"""`PerpAccount`: a conta de um bucket de perp (A8, um `BucketAccount`).

O mesmo caminho do `SpotAccount` (intenção -> gateway -> ledger -> ordem ->
livro), com o `PerpVenue` no lugar do swap: a compra posta o colateral e abre
a posição, a venda fecha a posição inteira ou uma parte dela (A12). O
colateral é o token de cotação, lido da carteira como no spot; a posição fica
no venue, não na carteira, então não há conferência de saldo na venda.

O livro, o restore e o PnL são os do spot: a ordem de entrada guarda o
colateral em `quote_amount` e a de saída o que voltou, e o `perp` delas dá o
lado e o preço (`trader/shared/models/perp.py`). Colateral a mais (A12) é uma
intenção `ADD` que faz a entrada crescer (`PositionBook.add`).

A ordem de stop no venue (D7) é uma intenção própria (`{chave}:stop`, lado
`STOP`, A12): passa pelo gateway como um trade (idempotência, o envio gravado
antes, a resolução), sem mover dinheiro. A que sobra depois de fechar é
cancelada do mesmo jeito (`{chave}:cancel`).
"""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from decimal import Decimal

from trader.execution.models.book import DUST_FRACTION
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentSide, TradeIntent
from trader.execution.models.perp import PerpTerms
from trader.execution.models.venue import Liquidation, PerpVenue, Venue
from trader.execution.trade.accounts.spot import SpotAccount
from trader.execution.trade.gateway.fills import (
    Fill,
    execute_trade,
    record_fill_safely,
    record_venue_order,
)
from trader.execution.trade.gateway.orders import perp_order_from_fill
from trader.execution.trade.ledger import events
from trader.shared.models import Order, OrderSide
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.perp import PerpFill

LIQUIDATION = "liquidation"
# o venue fechou sozinho e algo voltou (a ordem de stop dele disparou)
VENUE_EXIT = "venue_exit"
ONE = Decimal(1)


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

    # --- abrir ------------------------------------------------------------------

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
        # antes da intenção: um oráculo parado não deixa linha no ledger (A20)
        await self.perps.check_fresh(self.terms)
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
        await self._place_stop(order, key, usd.get(SOL_MINT))
        return order

    # --- a ordem de stop no venue (D7) ------------------------------------------

    async def _place_stop(
        self, order: Order, key: str, sol_usd: Decimal | None
    ) -> None:
        """A ordem de stop no venue, já com a entrada no ledger.

        Se não dá para colocar, a posição fecha agora pelo caminho normal: uma
        perp real nunca fica aberta sem o stop do venue.
        """
        fill = order.perp
        assert fill is not None  # `perp_order_from_fill`
        try:
            await self._venue_order(
                IntentSide.STOP,
                f"{key}:stop",
                "o stop do venue (D7)",
                lambda k: self.perps.place_stop(self.terms, fill, k),
                sol_usd,
            )
        except Exception as ex:
            await self._stop_failed(order, key, ex)  # fechar vem antes (D7)

    async def _venue_order(
        self,
        side: IntentSide,
        key: str,
        rationale: str,
        send: Callable[[str], Awaitable[ExecutionResult]],
        sol_usd: Decimal | None,
    ) -> None:
        """Uma ordem no venue pelo gateway (A12): uma intenção `STOP` (colocar)
        ou `CANCEL` (A20), sem dinheiro; a taxa do envio é custo do bucket, e
        um stop colocado deixa o endereço no ledger."""
        intent = TradeIntent(
            source=self.source,
            account=self.account_id,
            side=side,
            spend_mint=str(self.input_mint),
            receive_mint=str(self.output_mint),
            spend_amount=Decimal(0),
            rationale=rationale,
            perp=self.terms,
            idempotency_key=key,
        )
        fill = await execute_trade(
            self.gateway,
            self.perps,
            intent,
            lambda: send(key),
            sol_usd=sol_usd,
            on_failed_fee=self._charge_failed_fee,
        )
        placed = fill.result.venue_order if side == IntentSide.STOP else None
        usd = record_venue_order(self.gateway, intent, fill, sol_usd, placed)
        self.book.charge_cost(usd)

    async def _stop_failed(self, order: Order, key: str, ex: Exception) -> None:
        self.logger.error(f"{self.account_id}: o stop do venue falhou ({ex}); fechando")
        self._event(events.PERP_STOP_FAILED, {"error": f"{type(ex).__name__}: {ex}"})
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

    # --- fechar -----------------------------------------------------------------

    async def sell(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        """Fecha a posição, ou a parte `quantity` dela (A12).

        Uma parte tão grande que o resto seria poeira fecha tudo.
        """
        entry = self._open_entry()
        fraction = min(quantity / entry.quantity, ONE) if entry.quantity else ONE
        whole = fraction >= ONE - DUST_FRACTION
        fraction = ONE if whole else fraction
        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.SELL,
            price,
            entry.quantity * fraction,
            entry.quantity * fraction,
            usd,
            idempotency_key=idempotency_key
            or f"{self.account_id}:sell:{entry.order_id}:{entry.quantity}",
            rationale=rationale,
            closes_position=whole,
        )
        key = intent.idempotency_key
        order = await self._execute_order(
            self._perp_intent(intent),
            lambda: self.perps.close_perp(
                str(self.input_mint), self.terms, key, fraction
            ),
            OrderSide.SELL,
            price,
            usd,
        )
        self.logger.info(f"PERP FECHADA: {asdict(order)}", extra=asdict(order))
        order = self._settle(intent.intent_id, order, may_keep_rest=not whole)
        if whole:
            await self._cancel_stop_left(key, usd.get(SOL_MINT))
        return order

    async def _cancel_stop_left(self, key: str, sol_usd: Decimal | None) -> None:
        """A ordem de stop que ficou no venue depois de fechar: cancelada aqui
        (A12); se não dá, o dono a cancela na Jupiter. Nunca levanta."""
        placed = self.gateway.ledger.last_event(
            events.PERP_STOP_PLACED, self.account_id
        )
        order: str | None = (placed or {}).get("request")
        if not order:
            return
        try:
            if not await self.perps.stop_left(self.terms, order):
                return
            await self._venue_order(
                IntentSide.CANCEL,
                f"{key}:cancel",
                "cancelar o stop do venue que sobrou",
                lambda k: self.perps.cancel_stop(self.terms, order, k),
                sol_usd,
            )
        except Exception as ex:
            self.logger.error(
                f"{self.account_id}: a ordem de stop {order} ficou na Jupiter e "
                f"não foi cancelada ({ex}): cancele-a lá"
            )
            self._event(events.PERP_STOP_LEFT, {"request": order})

    def _open_entry(self) -> Order:
        position = self.book.position
        if position is None:
            raise ValueError("Não é possível fechar a perp: sem posição aberta")
        return position.entry_order

    def _settle(self, intent_id: str, order: Order, may_keep_rest: bool) -> Order:
        order, closed = self.book.settle_sell(order, may_keep_rest=may_keep_rest)
        record_fill_safely(
            self.gateway, intent_id, order, closed.realized_usd, closed.pnl
        )
        return order

    # --- colateral a mais (A12) -------------------------------------------------

    async def top_up(
        self,
        price: Decimal,
        held: PerpFill,
        limit: Callable[[], Awaitable[Decimal | None]],
    ) -> Order | None:
        """`market.add_collateral` da spec: perto da liquidação, colateral a mais.

        `price`: o preço do mercado agora (USD); `held`: a posição como o
        venue a tem (a varredura leu, A20); `limit`: o que o orçamento do
        bucket ainda permite (None: só a carteira), pedido só quando o
        colateral vai mesmo entrar. None se nada foi feito.
        """
        rule, position = self.terms.top_up, self.book.position
        if rule is None or position is None or held.liquidation_price is None:
            return None
        done = position.top_ups
        distance = abs(price / held.liquidation_price - 1) * 100
        if done >= rule.max_times or distance > rule.within_pct:
            return None
        entry = position.entry_order
        return await self.add_collateral(
            price,
            rule.usd,
            f"{self.account_id}:add:{entry.order_id}:{done}",
            await limit(),
        )

    async def add_collateral(
        self, price: Decimal, collateral: Decimal, key: str, limit: Decimal | None
    ) -> Order:
        """Põe `collateral` (no token de cotação) na posição aberta.

        Gasta como uma compra: a carteira e o orçamento limitam (o que a
        posição já usa conta), e a política vê o valor posto.
        """
        self._open_entry()
        spendable = self._spendable_input(await self.wallet.fresh(self.input_mint))
        room = spendable if limit is None else min(spendable, limit - self.committed())
        collateral = min(collateral, room)
        if collateral <= 0:
            raise ValueError("sem orçamento para colateral a mais")
        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.ADD,
            price,
            Decimal(0),
            collateral,
            usd,
            idempotency_key=key,
            rationale="colateral a mais: perto da liquidação (add_collateral)",
        )
        order = await self._execute_order(
            self._perp_intent(intent),
            lambda: self.perps.add_collateral(
                str(self.input_mint), self.terms, collateral, key
            ),
            OrderSide.BUY,
            price,
            usd,
        )
        self.logger.info(f"PERP COLATERAL: {asdict(order)}", extra=asdict(order))
        record_fill_safely(self.gateway, intent.intent_id, order)
        self.book.add(order)
        return order

    # --- saídas que o venue fez -------------------------------------------------

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
        return self._settle(intent.intent_id, order, may_keep_rest=False)
