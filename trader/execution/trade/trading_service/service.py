"""`TradeService`: executa ordens de estratégias, cada uma no seu bucket.

Camada de execução. Um bucket é uma conta lógica (`AsyncAccount`) com
`account_id = "<modo>:<nome>"` (o ledger já separa posição e PnL por conta)
e, opcionalmente, um orçamento em USD:

    disponível = min(saldo gastável, max(0, orçamento + min(0, PnL realizado)))

Ou seja, prejuízo realizado reduz o que o bucket ainda pode gastar; lucro não
aumenta. Os buckets dividem a carteira: um cache de saldo só
(`WalletBalances`), as ordens uma por vez (`asyncio.Lock`), e os orçamentos
abertos precisam caber na carteira. Ao abrir o primeiro bucket, as posições de
todos os buckets do ledger são conferidas contra a carteira; faltando token,
compras dele ficam bloqueadas. A política do gateway continua valendo para
cada ordem, por cima do bucket.

O modo (real/paper) é só do serviço: quem pede ordens nunca o conhece.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.errors import SwapRejectedError
from trader.execution.trade.gateway import (
    DuplicateIntentError,
    PolicyDeniedError,
    TradeGateway,
)
from trader.execution.trade.gateway.account import AsyncAccount
from trader.execution.trade.gateway.balances import WalletBalances
from trader.execution.trade.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.shared.models import Order, OrderSide
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    OrderRequest,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
# tolerância da reconciliação (taxas, arredondamento)
RECONCILE_TOLERANCE = Decimal("0.99")


@dataclass
class _Bucket:
    account: AsyncAccount
    budget_usd: Decimal | None
    status: BucketStatus = BucketStatus.ACTIVE
    # prejuízo realizado que encerra o bucket (o `max_loss_usd` da spec)
    max_loss_usd: Decimal | None = None
    # vendendo a sobra de um bucket encerrado: a venda passa por
    # `submit_order` de novo, que não pode tentar fechar outra vez
    closing: bool = False


def remaining_budget(budget_usd: Decimal, realized_usd: Decimal) -> Decimal:
    """Orçamento que sobra: prejuízo realizado consome, lucro não soma."""
    return max(ZERO, budget_usd + min(ZERO, realized_usd))


class TradeService:
    def __init__(
        self,
        provider: AsyncJupiterProvider,
        gateway: TradeGateway,
        mode: str | None = None,
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
        # preços USD para pares sem stablecoin/SOL; None no backtest e testes
        prices: PriceOracle | None = None,
    ):
        self.provider = provider
        self.prices = prices
        # no backtest, `TradeGateway.in_memory()`
        self.gateway = gateway
        self.mode = mode
        self.clock = clock
        self.wallet = WalletBalances(provider, clock)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        # tokens com menos na carteira do que as posições do ledger
        self._blocked: set[str] = set()
        self._reconciled = False

    def account_id(self, name: str) -> str:
        return f"{self.mode}:{name}" if self.mode else name

    async def open_bucket(
        self,
        name: str,
        input_mint: str,
        output_mint: str,
        budget_usd: Decimal | None = None,
        source: str = "strategy",
        max_loss_usd: Decimal | None = None,
    ) -> None:
        """Cria o bucket e restaura sua posição/PnL do ledger.

        Só lê saldos se há o que conferir: posições no ledger (reconciliação,
        uma vez por serviço) ou um orçamento (alocação).
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
            prices=self.prices,
            wallet=self.wallet,
        )
        bucket = _Bucket(account, budget_usd, max_loss_usd=max_loss_usd)
        self.gateway.open_account(account.account_id)  # antes do restore (ttl)
        account.restore_from_ledger()
        if not self._reconciled:
            await self._reconcile_wallet()
            self._reconciled = True
        await self._check_allocation(name, bucket)
        self._buckets[name] = bucket
        self._check_max_loss(name, bucket)  # já restaurado além do limite?

    async def _reconcile_wallet(self) -> None:
        """Por token: a soma das posições abertas do ledger cabe na carteira?

        Uma falta (tokens vendidos por fora, um fill mal registrado) vira um
        evento `reconcile_mismatch` e bloqueia compras do token neste processo;
        o dono confere a carteira. Vendas continuam, limitadas ao saldo.
        """
        prefix = f"{self.mode}:" if self.mode else ""
        for mint, expected in self.gateway.open_positions(prefix).items():
            balance = await self.wallet.get(mint)
            if balance >= expected * RECONCILE_TOLERANCE:
                continue
            self._blocked.add(mint)
            logger.warning(
                f"Carteira tem {balance} de {mint}, mas os buckets somam {expected}: "
                "compras bloqueadas"
            )
            self.gateway.add_event(
                "reconcile_mismatch",
                {"mint": mint, "expected": expected, "wallet_balance": balance},
            )

    async def _check_allocation(self, name: str, new: _Bucket) -> None:
        """Os orçamentos abertos (na mesma moeda) precisam caber na carteira.

        O que um bucket já comprou conta pelo custo: orçamento restante <=
        saldo livre + custo das posições abertas, somando todos os buckets.
        """
        if new.budget_usd is None:
            return
        quote = new.account.input_mint
        same = [
            b
            for b in (*self._buckets.values(), new)
            if b.account.input_mint == quote and b.budget_usd is not None
        ]
        allocated = sum((self._cap(b) or ZERO for b in same), ZERO)
        held = sum((_position_cost(b) for b in same), ZERO)
        free = await new.account.get_spendable_balance(quote)
        quote_usd = await self.quote_usd(quote)
        if quote_usd is None:
            raise ValueError(
                f"bucket {name}: sem preço USD de {quote} para conferir o orçamento"
            )
        if allocated > (free + held) * quote_usd:
            raise ValueError(
                f"bucket {name}: orçamentos de {allocated} USD passam do que a "
                f"carteira tem ({free} livres + {held} em posições, a {quote_usd} USD)"
            )

    def _check_max_loss(self, name: str, bucket: _Bucket) -> None:
        limit = bucket.max_loss_usd
        realized = bucket.account.book.realized_usd
        if limit is None or bucket.status != BucketStatus.ACTIVE or realized > -limit:
            return
        bucket.status = BucketStatus.RETIRING
        logger.warning(f"Bucket {name}: prejuízo {realized} atingiu o limite {limit}")
        self.gateway.add_event(
            "bucket_max_loss",
            {
                "account": bucket.account.account_id,
                "realized_usd": realized,
                "limit": limit,
            },
        )

    def retire(self, name: str, reason: str) -> None:
        """Encerra o bucket (sem compras); quem executa vende o que sobrou."""
        bucket = self._bucket(name)
        if bucket.status == BucketStatus.RETIRING:
            return
        bucket.status = BucketStatus.RETIRING
        logger.warning(f"Bucket {name} encerrado: {reason}")
        self.gateway.add_event(
            "bucket_retired", {"account": bucket.account.account_id, "reason": reason}
        )

    def _bucket(self, name: str) -> _Bucket:
        try:
            return self._buckets[name]
        except KeyError:
            raise ValueError(f"bucket {name} não está aberto") from None

    def _cap(self, bucket: _Bucket) -> Decimal | None:
        """O que o bucket ainda pode gastar; None sem orçamento (a carteira toda)."""
        if bucket.budget_usd is None:
            return None
        return remaining_budget(bucket.budget_usd, bucket.account.book.realized_usd)

    async def quote_usd(self, mint: Pubkey | str) -> Decimal | None:
        """USD por unidade de um token de cotação (1 em USDC/USDT)."""
        return (await usd_snapshot(self.prices, [str(mint)])).get(str(mint))

    async def _quote_cap(
        self, bucket: _Bucket
    ) -> tuple[Decimal | None, Decimal | None]:
        """(teto no token de cotação, quote_usd). Teto None: sem orçamento.

        O orçamento é em USD; sem o preço do token de cotação o teto é 0 (a
        compra não passa sem saber se cabe).
        """
        quote_usd = await self.quote_usd(bucket.account.input_mint)
        cap = self._cap(bucket)
        if cap is None:
            return None, quote_usd
        return (cap / quote_usd if quote_usd else ZERO), quote_usd

    async def get_bucket(self, name: str) -> BucketSnapshot:
        bucket = self._bucket(name)
        account = bucket.account
        available = await account.get_spendable_balance(account.input_mint)
        cap, quote_usd = await self._quote_cap(bucket)
        if cap is not None:
            available = min(available, cap)
        return BucketSnapshot(
            bucket=name,
            available=available,
            quote_usd=quote_usd,
            position=account.book.position,
            realized_usd=account.book.realized_usd,
            budget_usd=bucket.budget_usd,
            status=bucket.status,
            pnl_summary=account.book.summary(),
            opened_at=account.opened_at,
            last_exit_at=account.last_exit_at,
            last_exit_price=account.last_exit_price,
        )

    async def submit_order(self, name: str, request: OrderRequest) -> OrderReply:
        bucket = self._bucket(name)
        if request.side == OrderSide.BUY and bucket.status != BucketStatus.ACTIVE:
            return OrderReply.of_rejection(f"bucket {name} encerrado: sem compras")
        async with self._lock:
            reply = await self._place(bucket, request)
        self._check_max_loss(name, bucket)
        await self._close_if_retiring(name, bucket, request.price)
        return reply

    async def _close_if_retiring(
        self, name: str, bucket: _Bucket, price: Decimal
    ) -> None:
        """Um bucket encerrado com sobra aberta (venda parcial) vende o resto.

        Uma tentativa por ordem: se a venda falhar (reserva de SOL, tamanho
        mínimo, breaker, RPC), a próxima ordem tenta de novo.
        """
        if bucket.status == BucketStatus.ACTIVE or bucket.account.book.position is None:
            return
        if bucket.closing:
            return
        logger.warning(f"Bucket {name} encerrado com posição aberta: vendendo o resto")
        bucket.closing = True
        try:
            await self.close_bucket(name, price)
        finally:
            bucket.closing = False

    async def _place(self, bucket: _Bucket, request: OrderRequest) -> OrderReply:
        """Executa e classifica o desfecho; nenhuma exceção de execução escapa."""
        try:
            order = await self._execute(bucket, request)
        except (PolicyDeniedError, DuplicateIntentError) as ex:
            return OrderReply.of_denial(_reasons(ex))
        except (ValueError, SwapRejectedError) as ex:
            # nada foi executado: saldo, orçamento, impacto de preço...
            return OrderReply.of_rejection(str(ex))
        except Exception as ex:
            logger.error(f"Erro ao executar ordem de {bucket.account.account_id}: {ex}")
            return OrderReply.of_error(f"{type(ex).__name__}: {ex}")
        return OrderReply.of_fill(order)

    async def _execute(self, bucket: _Bucket, request: OrderRequest) -> Order:
        account = bucket.account
        if request.side == OrderSide.BUY and str(account.output_mint) in self._blocked:
            raise ValueError(
                f"compras de {account.output_mint} bloqueadas: a carteira tem menos "
                "do que as posições do ledger (reconcile_mismatch)"
            )
        if request.side == OrderSide.SELL:
            return await account.sell(
                request.price,
                request.quantity,
                request.rationale,
                request.idempotency_key,
            )
        cap, _ = await self._quote_cap(bucket)
        return await account.buy(
            request.price,
            request.quantity,
            request.rationale,
            request.idempotency_key,
            limit=cap,
        )

    async def close_bucket(self, name: str, price: Decimal) -> OrderReply | None:
        """Vende a posição aberta do bucket; None se não há posição."""
        position = self._bucket(name).account.book.position
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


def _position_cost(bucket: _Bucket) -> Decimal:
    position = bucket.account.book.position
    if position is None:
        return ZERO
    return position.entry_order.quote_amount or ZERO


def _reasons(ex: PolicyDeniedError | DuplicateIntentError) -> tuple[str, ...]:
    return ex.reasons if isinstance(ex, PolicyDeniedError) else (str(ex),)
