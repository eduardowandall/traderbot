"""Um bucket de perp vendido no paper, de ponta a ponta (A8, o "pronto quando").

Abre, segura, sai no stop, abre de novo e é liquidado (o preço passa da
liquidação), e o ledger e o PnL estão certos depois de reiniciar: o mesmo
arquivo de ledger e a mesma carteira simulada, num serviço novo.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import PerpOracle, events_of

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import (
    IntentSide,
    IntentStatus,
    SentTx,
    announce_send,
)
from trader.execution.models.perp import PerpTerms
from trader.execution.models.venue import Liquidation
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger import Ledger
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.paper.perps import SimulatedPerpsVenue
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.models.costs import ONCHAIN, TradeCosts
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
from trader.shared.spec.terms import AddCollateral
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
T0 = datetime(2026, 10, 7, 12, tzinfo=UTC)
SHORT3 = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
PAPER = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=1000,
    max_trades_per_hour_per_bucket=1000,
    perps_enabled=True,
)


class Paper:
    """Um `serve paper` de teste: carteira e ledger em arquivo, preço e relógio."""

    def __init__(self, tmp_path, policy=PAPER):
        self.tmp_path, self.policy = tmp_path, policy
        self.oracle, self.clock = PerpOracle("100"), [T0]
        self.wallet_path = tmp_path / "paper-wallet.json"
        SimulatedWallet(
            self.wallet_path, initial={"USDC": Decimal(100), "SOL": Decimal(1)}
        )
        self.ledgers: list[Ledger] = []

    def service(self, perps=True) -> TradeService:
        """Um serviço novo sobre o mesmo ledger e a mesma carteira (reinício)."""
        wallet = SimulatedWallet(self.wallet_path)
        ledger = Ledger(self.tmp_path / "ledger.sqlite3")
        self.ledgers.append(ledger)
        venue = None
        if perps:
            venue = SimulatedPerpsVenue(wallet, self.oracle, clock=self.now)
        return TradeService(
            SpotVenue(paper_provider(wallet, jupiter_client=object())),
            TradeGateway(ledger, self.policy, False),
            mode="paper",
            clock=self.now,
            prices=self.oracle,
            perps=venue,
        )

    def now(self) -> datetime:
        return self.clock[0]

    def close(self) -> None:
        for ledger in self.ledgers:
            ledger.close()


@pytest.fixture
def paper(tmp_path):
    paper = Paper(tmp_path)
    yield paper
    paper.close()


async def _open(service, terms=SHORT3, name="short"):
    await service.open_bucket(
        name,
        USDC,
        SOL_MINT,
        budget_usd=Decimal(30),
        max_loss_usd=Decimal(25),
        perp=terms,
    )


def _sell(price, quantity="1000"):
    """Uma venda; acima da posição, ela inteira (A12: menos é uma parte)."""
    return OrderRequest(OrderSide.SELL, Decimal(quantity), Decimal(price), "teste")


def _order(side, price, collateral="10"):
    price = Decimal(price)
    return OrderRequest(side, Decimal(collateral) / price, price, "teste")


async def test_a_short_opens_stops_out_and_is_liquidated(paper):
    service = paper.service()
    await _open(service)
    account = service._buckets["short"].account

    entry = (await service.submit_order("short", _order(OrderSide.BUY, "100"))).order
    assert entry is not None and entry.perp is not None
    assert entry.quote_amount == 10  # o colateral postado
    assert account.book.position and account.book.position.direction == Direction.SHORT
    assert entry.perp.liquidation_price and entry.perp.liquidation_price > 130

    # segura: o preço cai (bom para o vendido), depois sobe até o stop
    paper.oracle.set("95")
    assert account.book.position.unrealized_usd(Decimal(95)) > Decimal("1.4")
    paper.clock[0] += timedelta(hours=1)
    paper.oracle.set("104")
    exit_ = (await service.submit_order("short", _sell("104"))).order
    assert exit_ is not None and exit_.perp is not None
    back = exit_.quote_amount
    assert back is not None and Decimal("8.7") < back < Decimal("8.8")
    first_loss = account.book.realized_usd
    # duas pernas e o stop do venue (A12), 5000 lamports cada; o SOL (o
    # mercado) estava a 100 (a entrada e o stop) e a 104
    fees_usd = Decimal("0.000005") * (100 + 100 + 104)
    assert first_loss == pytest.approx(back - 10 - fees_usd)

    # de novo, e o preço passa da liquidação
    paper.oracle.set("100")
    entry = (await service.submit_order("short", _order(OrderSide.BUY, "100"))).order
    assert entry is not None and entry.perp and entry.perp.liquidation_price
    paper.oracle.set(entry.perp.liquidation_price + 1)
    assert list(await service.check_perps()) == ["short"]
    assert await service.check_perps() == {}  # uma vez só
    assert account.book.position is None
    lost = first_loss - account.book.realized_usd
    # o colateral, a taxa da entrada e a do stop do venue
    assert lost == pytest.approx(Decimal(10) + Decimal("0.000005") * 200)
    conn = paper.ledgers[0].conn
    events = [r["type"] for r in conn.execute("SELECT type FROM events")]
    assert "perp_liquidated" in events and "intent_external" in events

    # reinício: o mesmo PnL e nenhuma posição
    restarted = paper.service()
    await _open(restarted)
    again = restarted._buckets["short"].account
    assert again.book.position is None
    assert again.book.realized_usd == account.book.realized_usd
    totals = paper.ledgers[-1].pnl_totals(again.account_id)
    assert totals.closed == 2


async def test_an_open_short_is_restored_after_a_restart(paper):
    service = paper.service()
    await _open(service)
    await service.submit_order("short", _order(OrderSide.BUY, "100"))

    restarted = paper.service()
    await _open(restarted)
    position = restarted._buckets["short"].account.book.position
    assert position is not None and position.direction == Direction.SHORT
    paper.oracle.set("90")
    reply = await restarted.submit_order("short", _sell("90"))
    assert reply.filled and reply.order and reply.order.quote_amount
    assert reply.order.quote_amount > Decimal("12.9")  # +3 USD em 30 de tamanho


async def test_an_open_position_holds_its_market_and_side(paper):
    service = paper.service()
    await _open(service)
    await _open(service, name="other-short")  # abrir o bucket pode
    long3 = PerpTerms(SOL_MINT, Direction.LONG, Decimal(3))
    await _open(service, long3, name="long")
    assert (await service.submit_order("short", _order(OrderSide.BUY, "100"))).filled

    # o mesmo lado é recusado antes de virar intenção; o outro lado não
    reply = await service.submit_order("other-short", _order(OrderSide.BUY, "100"))
    assert reply.status == ReplyStatus.REJECTED
    assert "uma posição por mercado e lado" in reply.reasons[0]
    other = service.account_id("other-short")
    rows = service.gateway.ledger.conn.execute(
        "SELECT COUNT(*) FROM intents WHERE account = ?", (other,)
    ).fetchone()[0]
    assert rows == 0  # nenhuma intenção: nem a política nem o breaker veem
    assert (await service.submit_order("long", _order(OrderSide.BUY, "100"))).filled

    # fechada a posição (a v1 encerrada), o lado fica livre para a v2
    await service.submit_order("short", _sell("100"))
    assert (
        await service.submit_order("other-short", _order(OrderSide.BUY, "100"))
    ).filled


async def test_a_liquidation_that_fails_to_book_is_retried(paper, monkeypatch):
    service = paper.service()
    await _open(service)
    entry = (await service.submit_order("short", _order(OrderSide.BUY, "100"))).order
    assert entry is not None and entry.perp and entry.perp.liquidation_price
    paper.oracle.set(entry.perp.liquidation_price + 1)
    gateway = service.gateway
    record = gateway.record_external

    def locked(*args):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(gateway, "record_external", locked)
    with pytest.raises(RuntimeError):
        await service.check_perps()
    # nada no ledger: o venue ainda tem a posição, e a próxima varredura registra
    assert SimulatedWallet(paper.wallet_path).perps()
    monkeypatch.setattr(gateway, "record_external", record)
    assert list(await service.check_perps()) == ["short"]
    assert SimulatedWallet(paper.wallet_path).perps() == {}
    assert service._buckets["short"].account.book.position is None


async def test_a_mode_without_a_perp_venue_refuses_perp_buckets(paper):
    service = paper.service(perps=False)  # o real, até a A11
    with pytest.raises(ValueError, match="perps não disponíveis"):
        await _open(service)


async def test_the_policy_refuses_perp_entries_when_they_are_off(tmp_path):
    paper = Paper(tmp_path, Policy(max_trade_usd=Decimal(1000)))
    try:
        service = paper.service()
        await _open(service)
        reply = await service.submit_order("short", _order(OrderSide.BUY, "100"))
        assert reply.status == ReplyStatus.DENIED
        assert any("perps desligadas" in r for r in reply.reasons)
    finally:
        paper.close()


async def test_the_exposure_is_what_the_policy_limits(tmp_path):
    # 10 de colateral a 3x são 30 de exposição: acima de um limite de 25
    paper = Paper(tmp_path, Policy(max_trade_usd=Decimal(25), perps_enabled=True))
    try:
        service = paper.service()
        await _open(service)
        reply = await service.submit_order("short", _order(OrderSide.BUY, "100"))
        assert reply.status == ReplyStatus.DENIED
        assert any("acima do limite" in r for r in reply.reasons)
    finally:
        paper.close()


class StopFails(SimulatedPerpsVenue):
    """Um venue cuja ordem de stop não entra (D7: a posição fecha)."""

    async def place_stop(self, terms, fill, key):
        raise RuntimeError("stop recusado")


async def test_a_failed_venue_stop_closes_the_position(paper):
    service = paper.service()
    wallet = SimulatedWallet(paper.wallet_path)
    service.perps = StopFails(wallet, paper.oracle, clock=paper.now)
    await _open(service)

    reply = await service.submit_order("short", _order(OrderSide.BUY, "100"))

    assert reply.filled  # a entrada aconteceu e está no ledger
    account = service._buckets["short"].account
    assert account.book.position is None  # e foi fechada logo depois
    assert wallet.perps() == {}
    assert events_of(paper.ledgers[-1], "perp_stop_failed")


async def test_a_venue_exit_is_booked_with_what_came_back(paper):
    service = paper.service()
    await _open(service)
    await service.submit_order("short", _order(OrderSide.BUY, "100"))
    account = service._buckets["short"].account
    entry = account.book.position.entry_order
    # o stop do venue disparou: 9 USDC de volta, a posição sumiu do venue
    perp = replace(entry.perp, collateral_usd=Decimal(9), liquidated=False)
    result = ExecutionResult("venue-exit", SOL_MINT, USDC, 0, 9_000_000, perp=perp)

    order = await account.book_liquidation(Liquidation(SHORT3, result))

    assert order is not None and account.book.position is None
    # o que voltou menos o que foi postado, e as taxas da entrada e do stop
    assert account.book.realized_usd == pytest.approx(
        Decimal(9) - 10 - Decimal("0.000005") * 200
    )
    assert events_of(paper.ledgers[-1], "intent_external")[0]["reason"] == "venue_exit"


class StopSent(SimulatedPerpsVenue):
    """Um venue que envia a ordem de stop (como o real) e cobra a taxa dela."""

    async def place_stop(self, terms, fill, key):
        announce_send(SentTx("stop-sig", USDC, SOL_MINT, 0, 0))
        costs = TradeCosts(ONCHAIN, fee_lamports=10_000)
        return ExecutionResult(
            "stop-sig", USDC, SOL_MINT, 0, 0, costs=costs, venue_order="request"
        )


async def test_the_venue_stop_fee_is_a_cost_of_the_bucket(paper):
    # A11c F2: o envio do stop pagou taxa; ela entra no PnL, no orçamento e
    # nos relatórios, e um restore chega ao mesmo PnL
    service = paper.service()
    service.perps = StopSent(SimulatedWallet(paper.wallet_path), paper.oracle)
    await _open(service)

    reply = await service.submit_order("short", _order(OrderSide.BUY, "100"))

    assert reply.filled
    [fee] = events_of(paper.ledgers[-1], "perp_stop_fee")
    assert (fee["signature"], fee["fee_lamports"]) == ("stop-sig", 10_000)
    [placed] = events_of(paper.ledgers[-1], "perp_stop_placed")
    assert placed["request"] == "request"
    # o stop é uma intenção própria (A12): STOP, executada, com o envio gravado
    [stop] = [
        r for r in paper.ledgers[-1].list_intents() if r.intent.side == IntentSide.STOP
    ]
    assert stop.status == IntentStatus.EXECUTED and stop.signature == "stop-sig"
    assert paper.ledgers[-1].sends_of(stop.intent.intent_id)
    account = service._buckets["short"].account
    live = account.book.realized_usd
    assert live == -Decimal(fee["fee_usd"])  # nada fechado: só a taxa do stop
    totals = paper.ledgers[-1].pnl_totals(account.account_id)
    assert totals.net_usd == live and totals.fee_lamports >= 10_000
    account.restore_from_ledger()
    assert account.book.realized_usd == live


# --- A12 ----------------------------------------------------------------------


async def test_a_partial_close_realizes_its_share_and_survives_a_restart(paper):
    service = paper.service()
    await _open(service)
    await service.submit_order("short", _order(OrderSide.BUY, "100"))
    account = service._buckets["short"].account
    size = account.book.position.entry_order.quantity  # 0.3 SOL a 3x

    paper.oracle.set("90")
    reply = await service.submit_order("short", _sell("90", str(size / 2)))

    assert reply.filled and reply.order and not reply.order.closes_position
    rest = account.book.position.entry_order
    assert rest.quantity == size / 2 and account.book.position.partial_sells == 1
    assert rest.quote_amount == 5  # metade do colateral postado
    assert Decimal("1.4") < account.book.realized_usd < Decimal("1.5")
    restarted = paper.service()
    await _open(restarted)
    again = restarted._buckets["short"].account.book.position
    assert again.entry_order.quantity == rest.quantity and again.partial_sells == 1


def _topping(within="20", usd="2", times=1) -> PerpTerms:
    rule = AddCollateral(within_pct=Decimal(within), usd=Decimal(usd), max_times=times)
    return PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), top_up=rule)


async def test_collateral_is_added_near_liquidation_once_per_rule(paper):
    service = paper.service()
    terms = _topping()
    await _open(service, terms)
    entry = (await service.submit_order("short", _order(OrderSide.BUY, "100"))).order
    assert entry is not None and entry.perp and entry.perp.liquidation_price
    liquidation = entry.perp.liquidation_price
    account = service._buckets["short"].account

    # o stop do venue fica na metade até a liquidação (~116): a regra precisa
    # disparar antes dele
    await service.check_perps()  # a 100, ~25% da liquidação (~133): nada
    assert account.book.position.entry_order.quote_amount == 10
    paper.oracle.set("110")  # a ~17%: entra colateral
    await service.check_perps()
    entry = account.book.position.entry_order
    assert entry.quote_amount == 12  # postado: 10 + 2
    assert entry.perp and entry.perp.liquidation_price > liquidation
    await service.check_perps()  # `max_times` 1: não de novo
    assert account.book.position.entry_order.quote_amount == 12

    restarted = paper.service()
    await _open(restarted, terms)
    assert (
        restarted._buckets["short"].account.book.position.entry_order.quote_amount == 12
    )


async def test_added_collateral_stays_inside_the_budget(paper):
    service = paper.service()
    await service.open_bucket(
        "short",
        USDC,
        SOL_MINT,
        budget_usd=Decimal(11),
        max_loss_usd=Decimal(10),
        perp=_topping(usd="5"),
    )
    entry = (await service.submit_order("short", _order(OrderSide.BUY, "100"))).order
    assert entry is not None and entry.perp and entry.perp.liquidation_price
    paper.oracle.set("110")
    await service.check_perps()
    # 11 de orçamento, 10 já na posição e a taxa do stop realizada: só o resto
    account = service._buckets["short"].account
    realized = account.book.realized_usd  # a taxa do stop do venue
    assert account.book.position.entry_order.quote_amount == 11 + realized


async def test_the_venue_stop_closes_in_the_sweep(paper):
    service = paper.service()
    terms = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), stop_pct=Decimal(5))
    await _open(service, terms)
    await service.submit_order("short", _order(OrderSide.BUY, "100"))
    paper.oracle.set("106")  # passou do stop do venue (105)

    assert list(await service.check_perps()) == ["short"]

    assert service._buckets["short"].account.book.position is None
    [external] = events_of(paper.ledgers[-1], "intent_external")
    assert external["reason"] == "venue_exit"


async def test_one_read_of_the_venue_blocks_a_market_the_ledger_does_not_know(paper):
    # A12: uma posição aberta à mão (no venue, não no ledger)
    wallet = SimulatedWallet(paper.wallet_path)
    stray = SimulatedPerpsVenue(wallet, paper.oracle, clock=paper.now)
    await stray.open_perp(USDC, SHORT3, Decimal(10), "by-hand")
    service = paper.service()
    await _open(service, SHORT3, name="short")
    await _open(service, PerpTerms(SOL_MINT, Direction.LONG, Decimal(3)), name="long")

    [found] = events_of(paper.ledgers[-1], "perp_mismatch")
    assert (found["direction"], found["kind"]) == ("short", "unknown_to_ledger")
    reply = await service.submit_order("short", _order(OrderSide.BUY, "100"))
    assert reply.status == ReplyStatus.REJECTED
    assert "perp_mismatch" in reply.reasons[0]
    assert (await service.submit_order("long", _order(OrderSide.BUY, "100"))).filled


class StopStays(SimulatedPerpsVenue):
    """Um venue em que o stop sobra depois de fechar (o real, sem o keeper)."""

    cancel_error: Exception | None = None

    async def stop_left(self, terms, order):
        return True

    async def cancel_stop(self, terms, order, key):
        if self.cancel_error is not None:
            raise self.cancel_error
        return ExecutionResult("cancel-sig", USDC, SOL_MINT, 0, 0, venue_order=order)


@pytest.mark.parametrize("fails", [False, True])
async def test_a_leftover_venue_stop_is_cancelled_by_the_bot(paper, fails):
    service = paper.service()
    venue = StopStays(SimulatedWallet(paper.wallet_path), paper.oracle, clock=paper.now)
    venue.cancel_error = RuntimeError("cancel recusado") if fails else None
    service.perps = venue
    await _open(service)
    await service.submit_order("short", _order(OrderSide.BUY, "100"))

    await service.submit_order("short", _sell("100"))

    ledger = paper.ledgers[-1]
    sides = [r.intent.side for r in ledger.list_intents()]
    # o stop e o cancelamento, cada um uma intenção do seu lado (A20)
    assert sides.count(IntentSide.STOP) == sides.count(IntentSide.CANCEL) == 1
    left = events_of(ledger, "perp_stop_left")
    assert bool(left) is fails  # só quando o cancelamento falha, o dono cancela


async def test_a_perp_open_killed_mid_send_is_resolved_from_its_logged_send(
    paper, monkeypatch
):
    # A12: a intenção ficou EXECUTING com o envio gravado; a varredura resolve
    service = paper.service()
    await _open(service)
    ledger = service.gateway.ledger

    def killed(*args):
        raise RuntimeError("processo morto")

    monkeypatch.setattr(ledger, "mark_executed", killed)
    with pytest.raises(RuntimeError):
        await service._buckets["short"].account.buy(
            Decimal(100), Decimal("0.1"), limit=Decimal(30)
        )
    monkeypatch.undo()
    [stuck] = ledger.active_intents()
    assert ledger.sends_of(stuck.intent.intent_id)[0].perp is not None

    restarted = paper.service()
    await _open(restarted)
    await restarted.resolve_intents()

    record = paper.ledgers[-1].get(stuck.intent.intent_id)
    assert record is not None and record.status == IntentStatus.EXECUTED
    position = restarted._buckets["short"].account.book.position
    assert position is not None and position.entry_order.quote_amount == 10
    assert position.direction == Direction.SHORT


class StaleOracle(SimulatedPerpsVenue):
    """Um venue cujo oráculo parou (o real, com a Doves sem atualizar)."""

    async def check_fresh(self, terms):
        from trader.execution.market.perps.feed import StaleOracleError

        raise StaleOracleError("oráculo da Jupiter Perps parado há 90 s")


async def test_a_stale_oracle_refuses_the_buy_before_any_intent(paper):
    # A20: a recusa vem antes da intenção: nenhuma linha no ledger a cada tick
    service = paper.service()
    service.perps = StaleOracle(SimulatedWallet(paper.wallet_path), paper.oracle)
    await _open(service)

    reply = await service.submit_order("short", _order(OrderSide.BUY, "100"))

    assert reply.status == ReplyStatus.REJECTED and "parado" in reply.reasons[0]
    account = service.account_id("short")
    rows = service.gateway.ledger.conn.execute(
        "SELECT COUNT(*) FROM intents WHERE account = ?", (account,)
    ).fetchone()[0]
    assert rows == 0
