"""`PerpAccount`: a conta de um bucket de perp (A8, um `BucketAccount`).

O mesmo caminho do `SpotAccount` (intenção -> gateway -> ledger -> ordem ->
livro), com o `PerpVenue` no lugar do swap: a compra posta o colateral e abre
a posição, a venda fecha a posição inteira (vendas parciais são da A12). O
colateral é o token de cotação, lido da carteira como no spot; a posição fica
no venue, não na carteira, então não há conferência de saldo na venda.

O livro, o restore e o PnL são os do spot: a ordem de entrada guarda o
colateral em `quote_amount` e a de saída o que voltou, e o `perp` delas dá o
lado e o preço (`trader/shared/models/perp.py`).
"""

from dataclasses import asdict, replace
from decimal import Decimal

from trader.execution.models.intent import IntentSide, SentTx, TradeIntent
from trader.execution.models.perp import PerpTerms
from trader.execution.models.venue import Liquidation, PerpVenue, Venue
from trader.execution.trade.gateway.account import SpotAccount
from trader.execution.trade.gateway.fills import Fill, record_fill_safely
from trader.execution.trade.gateway.orders import perp_order_from_fill
from trader.shared.models import Order, OrderSide

LIQUIDATION = "liquidation"
# o venue fechou sozinho e algo voltou (a ordem de stop dele disparou)
VENUE_EXIT = "venue_exit"


class PerpAccount(SpotAccount):
    def __init__(self, venue: Venue, perps: PerpVenue, terms: PerpTerms, *args, **kw):
        super().__init__(venue, *args, **kw)
        self.perps = perps
        self.terms = terms
        self.post_trade = perps

    def _to_order(
        self,
        fill: Fill,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        return perp_order_from_fill(
            fill,
            self._quote,
            self._token,
            side,
            self.clock(),
            signal_price=price,
            usd=usd,
            requested_quantity=requested_quantity,
        )

    def _perp_intent(self, intent: TradeIntent) -> TradeIntent:
        """A intenção com os termos da perp; a exposição é o que a política vê."""
        notional = intent.notional_usd
        if intent.side == IntentSide.BUY and notional is not None:
            notional *= self.terms.leverage
        return replace(intent, perp=self.terms, notional_usd=notional)

    async def buy(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
        limit: Decimal | None = None,
    ) -> Order:
        """Posta `quantity * price` de colateral e abre a posição.

        O venue tem uma posição por mercado e lado na carteira: ocupada por
        outro bucket, a compra é recusada antes de virar intenção.
        """
        collateral = min(quantity * price, await self._buy_limit(limit))
        if await self.perps.has_position(self.terms):
            raise ValueError(
                f"a perp {self.terms.direction} deste mercado já está aberta por "
                "outro bucket (uma posição por mercado e lado)"
            )
        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.BUY,
            price,
            collateral / price,
            collateral,
            usd,
            idempotency_key=idempotency_key,
            rationale=rationale,
        )
        key = intent.idempotency_key
        order = await self._execute_order(
            self._perp_intent(intent),
            lambda: self.perps.open_perp(
                str(self.input_mint), self.terms, collateral, key
            ),
            OrderSide.BUY,
            price,
            usd,
        )
        self.logger.info(f"PERP ABERTA: {asdict(order)}", extra=asdict(order))
        record_fill_safely(self.gateway, intent.intent_id, order)
        self.book.open(order)
        await self._place_stop(order, key)
        return order

    async def _place_stop(self, order: Order, key: str) -> None:
        """A ordem de stop no venue (D7), já com a entrada no ledger.

        Se não dá para colocar, a posição fecha agora pelo caminho normal: uma
        perp real nunca fica aberta sem o stop do venue.
        """
        assert order.perp is not None  # `perp_order_from_fill`
        try:
            request = await self.perps.place_stop(
                self.terms, order.perp, f"{key}:stop", self._announce_stop
            )
        except Exception as ex:
            await self._stop_failed(order, key, ex)
            return
        if request is not None:
            self._event("perp_stop_placed", {"request": request})

    def _announce_stop(self, sent: SentTx) -> None:
        # gravado antes do envio (como os swaps, A3): um crash deixa o rastro
        self._event("perp_stop_sent", asdict(sent))

    async def _stop_failed(self, order: Order, key: str, ex: Exception) -> None:
        self.logger.error(f"{self.account_id}: o stop do venue falhou ({ex}); fechando")
        self._event("perp_stop_failed", {"error": f"{type(ex).__name__}: {ex}"})
        try:
            await self.sell(
                order.price,
                order.quantity,
                rationale="o stop do venue não pôde ser colocado (D7)",
                idempotency_key=f"{key}:stop-failed",
            )
        except Exception as close_ex:
            self.logger.error(
                f"{self.account_id}: fechar sem stop também falhou ({close_ex}): "
                "confira a posição na Jupiter"
            )

    def _event(self, kind: str, payload: dict) -> None:
        try:
            self.gateway.add_event(kind, {"account": self.account_id} | payload)
        except Exception as ex:
            self.logger.error(f"Evento {kind} não gravado: {ex}")

    async def sell(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        """Fecha a posição inteira (`quantity` não reduz: parcial é A12)."""
        entry = self._open_entry()
        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.SELL,
            price,
            entry.quantity,
            entry.quantity,
            usd,
            idempotency_key=idempotency_key
            or f"{self.account_id}:sell:{entry.order_id}:{entry.quantity}",
            rationale=rationale,
            closes_position=True,
        )
        key = intent.idempotency_key
        order = await self._execute_order(
            self._perp_intent(intent),
            lambda: self.perps.close_perp(str(self.input_mint), self.terms, key),
            OrderSide.SELL,
            price,
            usd,
        )
        self.logger.info(f"PERP FECHADA: {asdict(order)}", extra=asdict(order))
        order = self._settle(intent.intent_id, order)
        await self._check_stop_left()
        return order

    async def _check_stop_left(self) -> None:
        """Uma ordem de stop que ficou no venue depois de fechar: o dono a
        cancela na Jupiter (cancelar pelo bot é da A12). Nunca levanta."""
        try:
            left = await self.perps.stop_left(self.terms)
        except Exception as ex:
            self.logger.warning(f"Stop do venue não conferido: {ex}")
            return
        if left is not None:
            self.logger.error(
                f"{self.account_id}: a ordem de stop {left} ficou na Jupiter: "
                "cancele-a lá"
            )
            self._event("perp_stop_left", {"request": left})

    def _open_entry(self) -> Order:
        position = self.book.position
        if position is None:
            raise ValueError("Não é possível fechar a perp: sem posição aberta")
        return position.entry_order

    def _settle(self, intent_id: str, order: Order) -> Order:
        order, closed = self.book.settle_sell(order, may_keep_rest=False)
        record_fill_safely(
            self.gateway, intent_id, order, closed.realized_usd, closed.pnl
        )
        return order

    async def book_liquidation(self, liquidation: Liquidation) -> Order | None:
        """O venue fechou a posição sozinho: a saída entra no ledger sem a política.

        Uma liquidação (nada voltou): o colateral inteiro é prejuízo realizado
        (conta para `max_loss_usd`). Um stop do venue que disparou (A11b): o
        que voltou.
        None se não há posição aberta, ou se a liquidação já estava no ledger
        (uma tentativa anterior gravou e falhou depois: o livro relê o ledger).
        """
        entry = self.book.position and self.book.position.entry_order
        if entry is None:
            return None
        # o tamanho é o da entrada: o venue já não tem a posição para dizer
        result = replace(
            liquidation.result, in_amount=self._token.ui_to_raw(entry.quantity)
        )
        reason = LIQUIDATION if liquidation.liquidated else VENUE_EXIT
        usd = await self._usd_snapshot()
        intent = TradeIntent(
            source=reason,
            account=self.account_id,
            side=IntentSide.SELL,
            spend_mint=result.input_mint,
            receive_mint=result.output_mint,
            spend_amount=entry.quantity,
            notional_usd=None,
            # o preço da liquidação: o custo da ida e volta se mede até ele
            price=result.perp.price if result.perp else entry.price,
            quantity=entry.quantity,
            rationale=reason,
            closes_position=True,
            perp=self.terms,
            idempotency_key=f"{self.account_id}:{reason}:{entry.order_id}",
        )
        if not self.gateway.record_external(intent, result, reason):
            self.restore_from_ledger()
            return None
        order = self._to_order(
            Fill(result, result.costs), OrderSide.SELL, entry.quantity, entry.price, usd
        )
        return self._settle(intent.intent_id, order)
