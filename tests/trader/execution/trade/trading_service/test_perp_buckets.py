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
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
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
    exit_ = (await service.submit_order("short", _order(OrderSide.SELL, "104"))).order
    assert exit_ is not None and exit_.perp is not None
    back = exit_.quote_amount
    assert back is not None and Decimal("8.7") < back < Decimal("8.8")
    first_loss = account.book.realized_usd
    # duas pernas de 5000 lamports; o SOL (o mercado) estava a 100 e a 104
    fees_usd = Decimal("0.000005") * (100 + 104)
    assert first_loss == pytest.approx(back - 10 - fees_usd)

    # de novo, e o preço passa da liquidação
    paper.oracle.set("100")
    entry = (await service.submit_order("short", _order(OrderSide.BUY, "100"))).order
    assert entry is not None and entry.perp and entry.perp.liquidation_price
    paper.oracle.set(entry.perp.liquidation_price + 1)
    assert list(await service.check_liquidations()) == ["short"]
    assert await service.check_liquidations() == {}  # uma vez só
    assert account.book.position is None
    lost = first_loss - account.book.realized_usd
    assert lost == pytest.approx(Decimal(10) + Decimal("0.000005") * 100)
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
    reply = await restarted.submit_order("short", _order(OrderSide.SELL, "90"))
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
    await service.submit_order("short", _order(OrderSide.SELL, "100"))
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
        await service.check_liquidations()
    # nada no ledger: o venue ainda tem a posição, e a próxima varredura registra
    assert SimulatedWallet(paper.wallet_path).perps()
    monkeypatch.setattr(gateway, "record_external", record)
    assert list(await service.check_liquidations()) == ["short"]
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

    async def place_stop(self, terms, fill, key, announce):
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
    # o que voltou menos o que foi postado, e a taxa da entrada
    assert account.book.realized_usd == pytest.approx(
        Decimal(9) - 10 - Decimal("0.000005") * 100
    )
    assert events_of(paper.ledgers[-1], "intent_external")[0]["reason"] == "venue_exit"
