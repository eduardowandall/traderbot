"""Resolve intenções sem desfecho (A3): EXECUTING ou UNCONFIRMED.

Um processo morto (ou interrompido) no meio de um swap deixa a intenção
ativa, e ela bloqueia o modo (`policy._unresolved`). Todo envio é gravado
antes de acontecer (`intent_sent`, via `send_hook`), então dá para perguntar
à rede (ou à carteira simulada) o que houve com cada um:

- um envio LANDED: a intenção vira EXECUTED, com custos, ordem e (vendas) o
  PnL, como `SpotAccount` faria;
- todos FAILED ou EXPIRED: FAILED, com a taxa dos FAILED registrada;
- nenhum envio, numa intenção que grava envios: FAILED (nunca foi enviada);
- algum PENDING, ou uma intenção de uma versão sem o registro: continua
  bloqueando, e o log diz o que conferir (uma vez por intenção).

Uma perp (A12) passa pelo mesmo caminho até o envio que entrou; depois o
`PerpVenue` diz o que o keeper fez com ele (`resolve_send`): ainda nada
(continua bloqueando), executado (a perna vira ordem como na conta) ou
recusado (REJECTED, com a taxa e o rent do pedido como custo do bucket).

Quem chama garante que nada do processo está em execução (o lock de ordens
do `TradeService`). Cada resolução grava um evento `intent_resolved`.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.book import PositionBook
from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import (
    IntentRecord,
    IntentSide,
    IntentStatus,
    SentTx,
    TradeIntent,
    TxOutcome,
)
from trader.execution.models.venue import PerpVenue, PostTrade, Venue
from trader.execution.trade.gateway.fills import (
    FailedFees,
    record_fill_safely,
    record_leftover,
    record_venue_order,
    settle,
)
from trader.execution.trade.gateway.gateway import TradeGateway
from trader.execution.trade.gateway.orders import (
    order_from_fill,
    perp_order_from_fill,
    priced_mints,
)
from trader.execution.trade.ledger.events import (
    INTENT_RESOLVED,
)
from trader.shared.models import SOLANA_MINTS, Order, OrderSide
from trader.shared.models.mints import SOL_MINT

logger = logging.getLogger(__name__)


@dataclass
class IntentResolver:
    gateway: TradeGateway
    venue: Venue
    prices: PriceOracle | None = None
    # as perps do modo (A12); None: uma intenção de perp continua bloqueando
    perps: PerpVenue | None = None
    # intenções já avisadas como pendentes neste processo
    _warned: set[str] = field(default_factory=set)

    async def run(self) -> set[str]:
        """Tenta resolver cada intenção ativa; devolve as contas resolvidas."""
        accounts = set()
        for record in self.gateway.ledger.active_intents():
            try:
                if await self._resolve(record):
                    accounts.add(record.intent.account)
            except Exception as ex:
                logger.error(f"Intenção {record.intent.intent_id} não resolvida: {ex}")
        return accounts

    async def _resolve(self, record: IntentRecord) -> bool:
        if record.intent.perp is not None and self.perps is None:
            self._warn(record, "é uma perp sem venue neste modo: confira o venue")
            return False
        sends = self.gateway.ledger.sends_of(record.intent.intent_id)
        if not sends:
            return self._unsent(record)
        outcomes = [(s, await self.venue.send_outcome(s)) for s in sends]
        failed = [s.signature for s, o in outcomes if o == TxOutcome.FAILED]
        landed = [s for s, o in outcomes if o == TxOutcome.LANDED]
        if landed:
            return await self._landed(record, landed[-1], failed)
        pending = [s.signature for s, o in outcomes if o == TxOutcome.PENDING]
        if pending:
            self._warn(record, f"a transação {pending[-1]} ainda está pendente")
            return False
        self._mark_failed(record, "nenhum envio entrou na rede (falhou ou expirou)")
        await self._fees(record.intent, {}).book(self._unbooked(record, failed))
        return True

    async def _landed(
        self, record: IntentRecord, sent: SentTx, failed: Sequence[str]
    ) -> bool:
        if record.intent.perp is not None:
            return await self._perp_landed(record, sent, failed)
        await self._executed(record, sent, failed)
        return True

    async def _perp_landed(
        self, record: IntentRecord, sent: SentTx, failed: Sequence[str]
    ) -> bool:
        """Um pedido de perp entrou na rede: o que o keeper fez com ele."""
        assert self.perps is not None and record.intent.perp is not None
        try:
            result = await self.perps.resolve_send(sent, record.intent.perp)
        except SwapRejectedError as ex:
            self.gateway.ledger.mark_rejected(
                record.intent.intent_id, f"resolvida: {ex}"
            )
            self._note(record, IntentStatus.REJECTED, sent.signature, str(ex))
            await self._fees(record.intent, {}, self.perps).book(
                self._unbooked(record, [*failed, sent.signature])
            )
            return True
        if result is None:
            self._warn(record, f"o keeper ainda não executou {sent.signature}")
            return False
        await PerpResolution(self, record, result, failed).run()
        return True

    def _unsent(self, record: IntentRecord) -> bool:
        if not self.gateway.ledger.has_send_log(record.intent.intent_id):
            self._warn(
                record,
                "é de uma versão sem registro de envios: confira a carteira na "
                "blockchain e mova (ou apague) o ledger do modo",
            )
            return False
        # o envio seria gravado antes: sem registro, nada foi enviado
        self._mark_failed(record, "nunca enviada (interrompida antes do envio)")
        return True

    def _mark_failed(self, record: IntentRecord, reason: str) -> None:
        self.gateway.ledger.mark_failed(record.intent.intent_id, f"resolvida: {reason}")
        self._note(record, IntentStatus.FAILED, None, reason)

    async def _executed(
        self, record: IntentRecord, sent: SentTx, failed: Sequence[str]
    ) -> None:
        """EXECUTED com o envio que entrou; depois disso nada levanta."""
        intent = record.intent
        quote, token = intent.pair()
        # antes de marcar: preços (do oráculo agora) e a entrada que a venda fecha
        usd = await usd_snapshot(self.prices, priced_mints(quote, token))
        entry = None
        if intent.side == IntentSide.SELL:
            entry = self.gateway.restore(intent.account).open_entry
        result = sent.to_result()
        if not self.gateway.ledger.mark_executed(intent.intent_id, result):
            return
        self._note(
            record, IntentStatus.EXECUTED, sent.signature, "a transação entrou na rede"
        )
        fill = await settle(
            self.venue,
            result,
            self._fees(intent, usd),
            self._unbooked(record, failed),
        )
        order = order_from_fill(
            fill,
            quote,
            token,
            OrderSide(intent.side),
            intent.created_at,
            signal_price=intent.price,
            usd=usd,
            requested_quantity=intent.quantity,
        )
        self._record_fill(record, order, entry)

    def _record_fill(
        self, record: IntentRecord, order: Order, entry: Order | None
    ) -> None:
        """Como `SpotAccount`: a ordem e, numa venda, o PnL e a sobra."""
        intent = record.intent
        if entry is None:
            # uma compra; ou uma venda sem entrada no ledger (fica sem PnL)
            record_fill_safely(self.gateway, intent.intent_id, order)
            return
        book = PositionBook(SOLANA_MINTS[order.input_mint].symbol)
        book.open(entry)
        order, closed = book.settle_sell(order, not intent.closes_position)
        record_fill_safely(
            self.gateway, intent.intent_id, order, closed.realized_usd, closed.pnl
        )
        if order.closes_position:
            record_leftover(self.gateway, intent.account, entry, order)

    def _fees(
        self,
        intent: TradeIntent,
        usd: Mapping[str, Decimal],
        venue: PostTrade | None = None,
    ) -> FailedFees:
        return FailedFees(
            self.gateway, venue or self.venue, intent, usd.get(SOL_MINT), None
        )

    def _unbooked(self, record: IntentRecord, failed: Sequence[str]) -> list[str]:
        """Os envios falhos cuja taxa ainda não foi registrada."""
        booked = self.gateway.ledger.booked_fee_signatures(record.intent.intent_id)
        return [s for s in failed if s not in booked]

    def _note(
        self,
        record: IntentRecord,
        outcome: IntentStatus,
        signature: str | None,
        reason: str,
    ) -> None:
        intent = record.intent
        logger.warning(
            f"Intenção {intent.intent_id} ({intent.account}, {record.status}) "
            f"resolvida: {outcome}, {reason}"
        )
        self.gateway.ledger.add_event(
            INTENT_RESOLVED,
            {
                "account": intent.account,
                "from": str(record.status),
                "outcome": str(outcome),
                "signature": signature,
                "reason": reason,
            },
            intent_id=intent.intent_id,
        )

    def _warn(self, record: IntentRecord, what: str) -> None:
        intent_id = record.intent.intent_id
        if intent_id in self._warned:
            return
        self._warned.add(intent_id)
        logger.warning(
            f"Intenção {intent_id} ({record.intent.account}, {record.status}) "
            f"sem desfecho: {what}; o modo segue bloqueado (nova tentativa a "
            "cada varredura)"
        )


@dataclass
class PerpResolution:
    """Uma perna de perp que o keeper executou, registrada como a conta faria.

    Compra: a entrada; colateral a mais: a ordem (o restore soma); venda: o
    PnL contra a entrada do ledger; uma ordem no venue (o stop): o endereço
    dele, sem ordem.
    """

    resolver: IntentResolver
    record: IntentRecord
    result: ExecutionResult
    failed: Sequence[str]

    async def run(self) -> None:
        resolver, intent = self.resolver, self.record.intent
        assert resolver.perps is not None
        quote, token = intent.pair()
        usd = await usd_snapshot(resolver.prices, priced_mints(quote, token))
        entry = None
        if intent.side == IntentSide.SELL:
            entry = resolver.gateway.restore(intent.account).open_entry
        if not resolver.gateway.ledger.mark_executed(intent.intent_id, self.result):
            return
        resolver._note(
            self.record,
            IntentStatus.EXECUTED,
            self.result.signature,
            "o keeper executou o pedido",
        )
        fill = await settle(
            resolver.perps,
            self.result,
            resolver._fees(intent, usd, resolver.perps),
            resolver._unbooked(self.record, self.failed),
        )
        if not intent.side.moves_position:
            # o livro desconta a taxa no restore (o serviço relê o bucket)
            stop = intent.side == IntentSide.STOP
            placed = self.result.venue_order if stop else None
            gateway = resolver.gateway
            record_venue_order(gateway, intent, fill, usd.get(SOL_MINT), placed)
            return
        side = OrderSide.SELL if intent.side == IntentSide.SELL else OrderSide.BUY
        order = perp_order_from_fill(
            fill,
            quote,
            token,
            side,
            intent.created_at,
            signal_price=self._signal_price(),
            usd=usd,
            requested_quantity=intent.quantity or Decimal(0),
        )
        resolver._record_fill(self.record, order, entry)

    def _signal_price(self) -> Decimal:
        price = self.record.intent.price
        if price is None and self.result.perp is not None:
            price = self.result.perp.price
        return price or Decimal(0)
