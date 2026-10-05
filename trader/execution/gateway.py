"""Gateway de execução: o único caminho de uma intenção até um swap.

Fluxo de `submit`:
1. idempotência: se a chave já executou (ou pode ter executado), não repete;
2. política: avalia a intenção com o estado do ledger;
3. registra a intenção (recusada ou em execução) no ledger;
4. executa e registra o resultado. Falha após o envio vira UNCONFIRMED, que
   bloqueia novos trades do modo até o dono mover (ou apagar) o ledger.

O circuit breaker conta só as falhas desde que este gateway foi criado:
reiniciar o processo rearma.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trader.ledger import AccountPnL, Ledger, ledger_path
from trader.ledger.reports import FAILED_TX_FEE
from trader.models.book import remainder_entry
from trader.models.costs import FailedTxFee, PnLResult
from trader.models.errors import SwapRejectedError, TransactionSubmittedError
from trader.models.intent import IntentRecord, IntentSide, TradeIntent
from trader.models.mints import SOLANA_MINTS
from trader.models.mode import RunningMode
from trader.models.order import Order, OrderSide, SwapResult, order_from_json
from trader.policy import Policy, evaluate, load_policy

logger = logging.getLogger(__name__)


class PolicyDeniedError(Exception):
    def __init__(self, intent: TradeIntent, reasons: tuple[str, ...]):
        self.intent = intent
        self.reasons = reasons
        super().__init__("Trade recusado pela política: " + "; ".join(reasons))


class DuplicateIntentError(Exception):
    """A chave de idempotência já foi usada por uma intenção que moveu fundos."""

    def __init__(self, existing: IntentRecord):
        self.existing = existing
        super().__init__(
            f"Intenção duplicada: chave {existing.intent.idempotency_key!r} já "
            f"usada por {existing.intent.intent_id} ({existing.status})"
        )


def _open_entry(legs: list[IntentRecord]) -> Order | None:
    """A entrada da posição aberta: a última compra menos as vendas depois dela.

    Sem a ordem gravada (o `record_fill` falhou, ou a intenção foi resolvida
    à mão), a entrada é reconstruída da própria linha: melhor uma posição com
    valores aproximados do que comprar de novo ou nunca conseguir vender.
    """
    if not legs or legs[0].intent.side != IntentSide.BUY:
        return None
    entry: Order | None = _order_of(legs[0])
    for sell in legs[1:]:
        if entry is None:
            return None
        entry = _after_sell(entry, sell)
    return entry


def _after_sell(entry: Order, sell: IntentRecord) -> Order | None:
    """O que sobra depois da venda, como a memória faz (uma venda por vez)."""
    if sell.order_json:
        order = order_from_json(sell.order_json)
        return None if order.closes_position else remainder_entry(entry, order.quantity)
    # `record_fill` falhou: vale o que a intenção sabia antes de executar
    if sell.intent.closes_position:
        return None
    sold = sell.intent.quantity or sell.intent.spend_amount
    return remainder_entry(entry, sold)


def _order_of(record: IntentRecord) -> Order:
    if record.order_json:
        return order_from_json(record.order_json)
    return _entry_from_record(record)


def _entry_from_record(record: IntentRecord) -> Order:
    intent = record.intent
    quote, token = SOLANA_MINTS[intent.spend_mint], SOLANA_MINTS[intent.receive_mint]
    quantity = (
        token.raw_to_ui(record.out_amount) if record.out_amount else intent.quantity
    ) or Decimal("0")
    spent = (
        quote.raw_to_ui(record.in_amount) if record.in_amount else intent.spend_amount
    )
    fill_price = spent / quantity if quantity else Decimal("0")
    logger.warning(
        f"Entrada {intent.intent_id} sem ordem gravada: reconstruída da intenção"
    )
    return Order(
        order_id=record.signature or intent.intent_id,
        input_mint=quote.mint,
        output_mint=token.mint,
        quantity=quantity,
        price=intent.price if intent.price is not None else fill_price,
        side=OrderSide.BUY,
        timestamp=record.updated_at,
        requested_quantity=intent.quantity,
        requested_price=intent.price,
        fill_price=fill_price,
        quote_amount=spent,
    )


@dataclass(frozen=True)
class AccountState:
    """O que o ledger sabe de uma conta: PnL realizado e a posição aberta."""

    realized_usd: Decimal
    totals: AccountPnL
    open_entry: Order | None = None  # ordem de compra da posição aberta
    entry_intent_id: str | None = None
    # para a estratégia retomar cooldown/rearme e a validade (`ttl_days`)
    opened_at: datetime | None = None  # abertura da conta (`bucket_opened`)
    last_exit_at: datetime | None = None  # última venda executada
    last_exit_price: Decimal | None = None  # preço dela (`below_last_exit`)


class TradeGateway:
    """Único caminho até o ledger para quem executa (conta, serviço, CLI)."""

    def __init__(
        self,
        ledger: Ledger,
        policy: Policy,
        real_mode: bool,
    ):
        self.ledger = ledger
        self.policy = policy
        self.real_mode = real_mode
        # o circuit breaker só conta falhas deste processo
        self.started_at = datetime.now(UTC)

    @classmethod
    def for_mode(cls, mode: str, policy: Policy | None = None) -> TradeGateway:
        """Ledger do modo + política do modo (`None` carrega `policy.toml`)."""
        return cls(
            ledger=Ledger(ledger_path(mode)),
            policy=load_policy(mode=str(mode)) if policy is None else policy,
            real_mode=mode == RunningMode.REAL,
        )

    @classmethod
    def in_memory(cls, policy: Policy | None = None) -> TradeGateway:
        """Gateway de replay: ledger em memória, modo não real.

        O backtest passa pelo mesmo caminho do ao vivo (idempotência, ciclo
        da intenção, eventos), mas sem os limites da política
        (`Policy.unlimited()`, veja lá o porquê).
        """
        return cls(
            ledger=Ledger(),
            policy=Policy.unlimited() if policy is None else policy,
            real_mode=False,
        )

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> TradeGateway:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- estado e registros (sem passar pela política) ----------------------

    def restore(self, account_id: str) -> AccountState:
        """PnL e posição aberta da conta, para reconstruir após reinício."""
        legs = self.ledger.legs_since_last_buy(account_id)
        entry = _open_entry(legs)
        opened_at, last_exit_at = self.ledger.account_times(account_id)
        totals = self.ledger.pnl_totals(account_id)
        return AccountState(
            realized_usd=totals.net_usd,
            totals=totals,
            open_entry=entry,
            entry_intent_id=legs[0].intent.intent_id if entry else None,
            opened_at=opened_at,
            last_exit_at=None if entry else last_exit_at,
            last_exit_price=None if entry else self.ledger.last_exit_price(account_id),
        )

    def open_positions(self, prefix: str = "") -> dict[str, Decimal]:
        """Tokens em posições abertas, somados por mint, nas contas do prefixo."""
        held: dict[str, Decimal] = {}
        for account in self.ledger.accounts(prefix):
            entry = _open_entry(self.ledger.legs_since_last_buy(account))
            if entry is not None:
                mint = entry.output_mint
                held[mint] = held.get(mint, Decimal("0")) + entry.quantity
        return held

    def open_account(self, account_id: str) -> None:
        """Marca a abertura da conta (a primeira vez que um bucket abre)."""
        self.ledger.mark_account_opened(account_id)

    def record_fill(
        self,
        intent_id: str,
        order: Order,
        realized_usd: Decimal | None = None,
        pnl: PnLResult | None = None,
    ) -> None:
        """Grava a ordem (e o PnL, em vendas) de uma intenção já EXECUTED.

        Fica separado de `mark_executed` de propósito: os custos são buscados
        entre os dois, e a intenção precisa estar EXECUTED antes disso.
        """
        self.ledger.attach_order(intent_id, order, realized_usd, pnl)

    def add_event(self, type_: str, payload: dict) -> None:
        self.ledger.add_event(type_, payload)

    def record_failed_fee(self, intent: TradeIntent, fee: FailedTxFee) -> None:
        """Taxas de tentativas que falharam na rede: custo do bucket, sem trade.

        Um evento (não uma coluna da intenção): vale para intenções que
        terminaram EXECUTED, FAILED ou REJECTED, e `pnl_totals` o desconta do PnL.
        """
        self.ledger.add_event(
            FAILED_TX_FEE,
            {
                "account": intent.account,
                "intent_id": intent.intent_id,
                "signatures": list(fee.signatures),
                "fee_lamports": fee.lamports,
                "fee_usd": fee.usd,
            },
            intent_id=intent.intent_id,
        )

    async def submit(
        self,
        intent: TradeIntent,
        execute: Callable[[], Awaitable[SwapResult]],
    ) -> SwapResult:
        self._authorize(intent)
        result = await self._execute(intent, execute)
        try:
            marked = self.ledger.mark_executed(intent.intent_id, result)
        except Exception:
            # o swap aconteceu: a assinatura e os valores precisam sobreviver
            # (a intenção fica EXECUTING e bloqueia novos trades)
            logger.error(
                f"Swap executado mas não registrado ({intent.intent_id}): {result}"
            )
            raise
        if not marked:
            # a intenção já tinha um desfecho final: não vira trade aqui
            raise RuntimeError(
                f"Intenção {intent.intent_id} já finalizada; swap {result.signature} "
                "não registrado como executado"
            )
        return result

    def _authorize(self, intent: TradeIntent) -> None:
        """Idempotência + política; registra a intenção (ou a recusa)."""
        existing, decision = self.ledger.authorize(
            intent,
            lambda state: evaluate(
                intent, self.policy, state, real_mode=self.real_mode
            ),
            failures_since=self.started_at,
        )
        if existing is not None:
            raise DuplicateIntentError(existing)
        assert decision is not None  # sem existente, sempre há decisão
        if not decision.allowed:
            logger.warning(f"Intenção {intent.intent_id} recusada: {decision.reasons}")
            raise PolicyDeniedError(intent, decision.reasons)

    async def _execute(
        self,
        intent: TradeIntent,
        execute: Callable[[], Awaitable[SwapResult]],
    ) -> SwapResult:
        """Executa e registra falhas; o sucesso é registrado por `submit`."""
        try:
            return await execute()
        except TransactionSubmittedError as ex:
            self.ledger.mark_unconfirmed(intent.intent_id, str(ex), ex.signature)
            raise
        except SwapRejectedError as ex:
            # recusa antes do envio (impacto de preço, saldo simulado): nada
            # quebrou, então não conta para o circuit breaker
            self.ledger.mark_rejected(intent.intent_id, str(ex))
            raise
        except Exception as ex:
            # erros antes do envio (quote, simulação, RPC) ou falha na rede
            self.ledger.mark_failed(intent.intent_id, f"{type(ex).__name__}: {ex}")
            raise
        except BaseException as ex:
            # cancelamento (Ctrl+C) no meio da execução: não dá para saber se a
            # transação chegou a ser enviada, então bloqueia até o dono conferir
            self.ledger.mark_unconfirmed(
                intent.intent_id, f"interrompida: {type(ex).__name__}"
            )
            raise
