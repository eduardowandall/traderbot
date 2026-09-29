"""`TradeService`: executa ordens de estratégias, cada uma no seu bucket.

Camada de execução. Um bucket é uma conta lógica (`AsyncAccount`) com
`account_id = "<modo>:<nome>"` (o ledger já separa posição e PnL por conta)
e, opcionalmente, um orçamento em USD:

    disponível = min(saldo gastável, max(0, orçamento + min(0, PnL realizado)))

Ou seja, prejuízo realizado reduz o que o bucket ainda pode gastar; lucro não
aumenta. Como todos os buckets dividem a carteira, cada compra relê o saldo e
as ordens passam uma por vez (`asyncio.Lock`). A política do gateway continua
valendo para cada ordem, por cima do bucket.

Swaps manuais (`swap`) passam pelo mesmo lock e pelo mesmo caminho, no
bucket `manual` (ver `manual.py`).

O modo (real/dry/paper) é só do serviço: quem pede ordens nunca o conhece.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.async_account import AsyncAccount
from trader.execution import DuplicateIntentError, PolicyDeniedError, TradeGateway
from trader.models import Order, OrderSide
from trader.models.errors import SwapRejectedError
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.trading_service.manual import execute_swap
from trader.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    OrderRequest,
    SwapRequest,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
# bucket dos swaps manuais: conta "<modo>:manual", sem posição nem orçamento
MANUAL_BUCKET = "manual"


@dataclass
class _Bucket:
    account: AsyncAccount
    budget_usd: Decimal | None
    status: BucketStatus = BucketStatus.ACTIVE


def remaining_budget(budget_usd: Decimal, realized_usd: Decimal) -> Decimal:
    """Orçamento que sobra: prejuízo realizado consome, lucro não soma."""
    return max(ZERO, budget_usd + min(ZERO, realized_usd))


class TradeService:
    def __init__(
        self,
        provider: AsyncJupiterProvider,
        gateway: TradeGateway,
        mode: str | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.provider = provider
        # no backtest, `TradeGateway.in_memory()`
        self.gateway = gateway
        self.mode = mode
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def account_id(self, name: str) -> str:
        return f"{self.mode}:{name}" if self.mode else name

    async def open_bucket(
        self,
        name: str,
        input_mint: str,
        output_mint: str,
        budget_usd: Decimal | None = None,
        source: str = "strategy",
    ) -> None:
        """Cria o bucket e restaura sua posição/PnL do ledger.

        Não lê saldos: a única chamada à rede é a reconciliação.
        """
        if name in self._buckets:
            raise ValueError(f"bucket {name} já está aberto")
        account = AsyncAccount(
            self.provider,
            Pubkey.from_string(input_mint),
            Pubkey.from_string(output_mint),
            gateway=self.gateway,
            account_id=self.account_id(name),
            source=source,
            clock=self.clock,
        )
        bucket = _Bucket(account, budget_usd)
        if budget_usd is not None:
            account.spend_cap = lambda: self._cap(bucket)
        account.restore_from_ledger()
        if self.provider.balances_track_fills:
            # em dry run a carteira real não reflete as ordens simuladas
            await account.reconcile_position()
        self._buckets[name] = bucket

    def _bucket(self, name: str) -> _Bucket:
        try:
            return self._buckets[name]
        except KeyError:
            raise ValueError(f"bucket {name} não está aberto") from None

    def _cap(self, bucket: _Bucket) -> Decimal:
        assert bucket.budget_usd is not None
        return remaining_budget(
            bucket.budget_usd, bucket.account.get_total_realized_pnl()
        )

    async def get_bucket(self, name: str) -> BucketSnapshot:
        bucket = self._bucket(name)
        account = bucket.account
        available = await account.get_spendable_balance(account.input_mint)
        if bucket.budget_usd is not None:
            available = min(available, self._cap(bucket))
        return BucketSnapshot(
            bucket=name,
            available_usd=available,
            position=account.get_position(),
            realized_usd=account.get_total_realized_pnl(),
            budget_usd=bucket.budget_usd,
            status=bucket.status,
            pnl_summary=account.pnl_summary(),
        )

    async def submit_order(self, name: str, request: OrderRequest) -> OrderReply:
        account = self._bucket(name).account
        async with self._lock:
            return await _reply(
                account.account_id,
                lambda: account.place_order(
                    request.price,
                    request.side,
                    request.quantity,
                    rationale=request.rationale,
                    idempotency_key=request.idempotency_key,
                ),
            )

    async def swap(self, request: SwapRequest, source: str = "cli") -> OrderReply:
        """Swap manual no bucket `manual` (qualquer par, sem posição)."""
        account = self.account_id(MANUAL_BUCKET)
        async with self._lock:
            return await _reply(
                account,
                lambda: execute_swap(
                    self.gateway,
                    self.provider,
                    account,
                    request,
                    source,
                    self.clock(),
                ),
            )

    async def close_bucket(self, name: str, price: Decimal) -> OrderReply | None:
        """Vende a posição aberta do bucket; None se não há posição."""
        position = self._bucket(name).account.get_position()
        if position is None:
            return None
        request = OrderRequest(
            OrderSide.SELL,
            position.entry_order.quantity,
            price,
            rationale=f"bucket {name} encerrado",
        )
        return await self.submit_order(name, request)

    async def aclose(self) -> None:
        await self.provider.aclose()


async def _reply(
    account_id: str, execute: Callable[[], Awaitable[Order]]
) -> OrderReply:
    """Executa e classifica o desfecho; nenhuma exceção de execução escapa."""
    try:
        order = await execute()
    except (PolicyDeniedError, DuplicateIntentError) as ex:
        return OrderReply.of_denial(_reasons(ex))
    except (ValueError, SwapRejectedError) as ex:
        # nada foi executado: saldo, orçamento, impacto de preço...
        return OrderReply.of_rejection(str(ex))
    except Exception as ex:
        logger.error(f"Erro ao executar ordem de {account_id}: {ex}")
        return OrderReply.of_error(f"{type(ex).__name__}: {ex}")
    return OrderReply.of_fill(order)


def _reasons(ex: PolicyDeniedError | DuplicateIntentError) -> tuple[str, ...]:
    return ex.reasons if isinstance(ex, PolicyDeniedError) else (str(ex),)
