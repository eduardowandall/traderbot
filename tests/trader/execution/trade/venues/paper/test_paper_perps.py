"""O motor de perps do paper (A8): taxas, empréstimo, liquidação, a carteira."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import PerpOracle

from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.perp import PerpTerms
from trader.execution.trade.venues.paper import SimulatedWallet
from trader.execution.trade.venues.paper.perps import SimulatedPerpsVenue
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.perp import (
    MAINTENANCE_RATE,
    PerpFill,
    liquidation_price,
    perp_from_dict,
    perp_to_dict,
)

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
T0 = datetime(2026, 10, 7, 12, tzinfo=UTC)
SHORT3 = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
LONG3 = PerpTerms(SOL_MINT, Direction.LONG, Decimal(3))


def _venue(tmp_path, price="100"):
    wallet = SimulatedWallet(
        tmp_path / "w.json", initial={"USDC": Decimal(100), "SOL": Decimal(1)}
    )
    oracle, clock = PerpOracle(price), [T0]
    venue = SimulatedPerpsVenue(wallet, oracle, clock=lambda: clock[0])
    return venue, wallet, oracle, clock


async def test_open_posts_collateral_and_keeps_one_position_per_side(tmp_path):
    venue, wallet, _, _ = _venue(tmp_path)
    result = await venue.open_perp(USDC, SHORT3, Decimal(10), "k")

    fill = result.perp
    assert fill is not None and fill.size_usd == 30
    # 0.06% de 30 + o impacto (1 bps a cada 10 mil USD: quase nada aqui)
    assert fill.fees_usd == Decimal("0.018") + Decimal("0.000009")
    assert fill.collateral_usd == 10 - fill.fees_usd
    assert wallet.balance(USDC) == 90
    assert wallet.balance(SOL_MINT) == Decimal(1) - Decimal("0.000005")
    assert (result.input_mint, result.output_mint) == (USDC, SOL_MINT)
    assert SOLANA_MINTS[SOL_MINT].raw_to_ui(result.out_amount) == Decimal("0.3")
    with pytest.raises(SwapRejectedError, match="já existe"):
        await venue.open_perp(USDC, SHORT3, Decimal(10), "k")
    await venue.open_perp(USDC, LONG3, Decimal(10), "k")  # o outro lado pode
    assert set(wallet.perps()) == {f"{SOL_MINT}:short", f"{SOL_MINT}:long"}


async def test_close_returns_collateral_plus_pnl_minus_borrow_and_fees(tmp_path):
    venue, wallet, oracle, clock = _venue(tmp_path)
    opened = (await venue.open_perp(USDC, SHORT3, Decimal(10), "k")).perp
    assert opened is not None
    oracle.set("95")  # vendido: -5% no preço = +1.5 USD em 30 de tamanho
    clock[0] += timedelta(hours=2)

    result = await venue.close_perp(USDC, SHORT3, "k")

    fill = result.perp
    assert fill is not None and not fill.liquidated
    assert fill.borrow_usd == Decimal("0.006")  # 30 x 1 bps x 2 h
    expected = opened.collateral_usd + Decimal("1.5") - fill.borrow_usd - fill.fees_usd
    assert fill.collateral_usd == expected
    assert wallet.balance(USDC) == 90 + expected.quantize(Decimal("0.000001"))
    assert (result.input_mint, result.output_mint) == (SOL_MINT, USDC)
    assert wallet.perps() == {}
    with pytest.raises(SwapRejectedError, match="nenhuma posição"):
        await venue.close_perp(USDC, SHORT3, "k")


async def test_liquidation_takes_the_whole_collateral(tmp_path):
    venue, wallet, oracle, _ = _venue(tmp_path)
    opened = (await venue.open_perp(USDC, SHORT3, Decimal(10), "k")).perp
    assert opened is not None and opened.liquidation_price is not None
    oracle.set(opened.liquidation_price - Decimal("0.01"))
    assert (await venue.sweep([SHORT3])).exits == []

    oracle.set(opened.liquidation_price + Decimal("0.01"))
    assert (await venue.sweep([LONG3])).exits == []  # só as posições pedidas
    [liquidation] = (await venue.sweep([SHORT3])).exits

    assert liquidation.terms == SHORT3
    assert liquidation.result.perp and liquidation.result.perp.liquidated
    assert liquidation.result.out_amount == 0
    assert wallet.balance(USDC) == 90  # nada volta
    # a posição fica até quem perguntou registrar a liquidação
    assert set(wallet.perps()) == {f"{SOL_MINT}:short"}
    await venue.acknowledge(SHORT3)
    assert wallet.perps() == {}
    await venue.acknowledge(SHORT3)  # de novo: nada a esquecer


async def test_no_price_refuses_the_open(tmp_path):
    venue, wallet, oracle, _ = _venue(tmp_path)
    oracle.prices.clear()
    with pytest.raises(SwapRejectedError, match="sem preço"):
        await venue.open_perp(USDC, SHORT3, Decimal(10), "k")
    assert wallet.balance(USDC) == 100


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_at_the_liquidation_price_the_equity_is_the_maintenance_margin(direction):
    fill = PerpFill(
        direction=direction,
        leverage=Decimal(3),
        price=Decimal(100),
        size_usd=Decimal(30),
        collateral_usd=Decimal("9.98"),
        fees_usd=Decimal(0),
        borrow_bps_hour=Decimal(0),
    )
    price = liquidation_price(fill)
    assert fill.equity(price) == pytest.approx(fill.size_usd * MAINTENANCE_RATE)
    # 3x: a liquidação fica perto de 33% contra (menos as taxas e a margem)
    move = (price - 100) * direction.sign
    assert Decimal(-34) < move < Decimal(-32)


def test_a_fill_round_trips_through_its_dict():
    fill = PerpFill(
        Direction.SHORT,
        Decimal(3),
        Decimal(100),
        Decimal(30),
        Decimal(10),
        Decimal("0.018"),
        Decimal(1),
        liquidation_price=Decimal(133),
        liquidated=True,
    )
    assert perp_from_dict(perp_to_dict(fill)) == fill


class Rates:
    """A taxa de empréstimo da Jupiter (A10) de mentira: `None` falha."""

    def __init__(self, *rates: Decimal | None):
        self.rates = list(rates)
        self.reads = 0
        self.mints: list[str] = []
        self.closed = False

    async def borrow_bps_hour(self, mint: str) -> Decimal:
        self.reads += 1
        self.mints.append(mint)
        rate = self.rates.pop(0)
        if rate is None:
            raise RuntimeError("RPC falhou")
        return rate

    async def aclose(self) -> None:
        self.closed = True


def _live_venue(tmp_path, rates: Rates):
    venue, wallet, oracle, clock = _venue(tmp_path)
    venue.borrow_rates = rates
    return venue, clock


async def _rate(venue) -> Decimal:
    fill = (await venue.open_perp(USDC, SHORT3, Decimal(10), "k")).perp
    assert fill is not None
    await venue.close_perp(USDC, SHORT3, "k")
    return fill.borrow_bps_hour


async def test_paper_opens_at_the_live_rate_read_at_most_every_few_minutes(tmp_path):
    rates = Rates(Decimal("0.17"), Decimal("0.2"))
    venue, clock = _live_venue(tmp_path, rates)
    assert await _rate(venue) == Decimal("0.17")
    clock[0] += timedelta(minutes=1)
    assert await _rate(venue) == Decimal("0.17")  # ainda vale: sem ler
    clock[0] += timedelta(minutes=5)
    assert await _rate(venue) == Decimal("0.2")
    assert rates.reads == 2
    await venue.aclose()
    assert rates.closed


async def test_a_failed_read_keeps_the_last_good_rate_or_the_default(tmp_path):
    venue, clock = _live_venue(tmp_path, Rates(None, Decimal("0.17"), None))
    assert await _rate(venue) == Decimal(1)  # nunca leu: a padrão
    clock[0] += timedelta(minutes=10)
    assert await _rate(venue) == Decimal("0.17")
    clock[0] += timedelta(minutes=10)
    assert await _rate(venue) == Decimal("0.17")  # falhou: a última boa


async def test_each_side_borrows_from_its_collateral_custody(tmp_path):
    # A18: um vendido toma USDC emprestado; um comprado, o próprio SOL
    rates = Rates(Decimal("0.6"), Decimal("0.2"))
    venue, _ = _live_venue(tmp_path, rates)
    short = (await venue.open_perp(USDC, SHORT3, Decimal(10), "s")).perp
    long = (await venue.open_perp(USDC, LONG3, Decimal(10), "l")).perp
    assert rates.mints == [USDC, SOL_MINT]
    assert short is not None and short.borrow_bps_hour == Decimal("0.6")
    assert long is not None and long.borrow_bps_hour == Decimal("0.2")


# --- A12: o stop do venue, partes, colateral, os envios gravados ---------------


async def _short_with_stop(tmp_path):
    venue, wallet, oracle, clock = _venue(tmp_path)
    terms = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), stop_pct=Decimal(5))
    opened = await venue.open_perp(USDC, terms, Decimal(10), "k")
    assert opened.perp is not None
    placed = await venue.place_stop(terms, opened.perp, "k:stop")
    return venue, wallet, oracle, terms, placed


async def test_the_venue_stop_fires_in_the_sweep_and_pays_back(tmp_path):
    venue, wallet, oracle, terms, placed = await _short_with_stop(tmp_path)
    stop = Decimal(wallet.perps()[f"{SOL_MINT}:short"]["stop"])
    assert placed.venue_order and stop == 105
    # o envio do stop pagou a taxa de rede, como no real
    assert wallet.balance(SOL_MINT) == Decimal(1) - 2 * Decimal("0.000005")

    oracle.set("104")
    assert (await venue.sweep([terms])).exits == []  # antes do stop: nada
    oracle.set("106")
    [exit_] = (await venue.sweep([terms])).exits
    assert not exit_.liquidated and exit_.result.perp is not None
    back = exit_.result.perp.collateral_usd
    assert Decimal("8") < back < Decimal("8.3")  # -6% em 30 de tamanho

    before = wallet.balance(USDC)
    await venue.acknowledge(terms)
    assert wallet.perps() == {}
    assert wallet.balance(USDC) == before + back.quantize(Decimal("0.000001"))


async def test_a_partial_close_keeps_the_rest_open(tmp_path):
    venue, wallet, oracle, clock = _venue(tmp_path)
    opened = (await venue.open_perp(USDC, SHORT3, Decimal(10), "k")).perp
    assert opened is not None

    half = await venue.close_perp(USDC, SHORT3, "c", Decimal("0.5"))

    assert half.perp is not None and half.perp.size_usd == 15
    assert SOLANA_MINTS[SOL_MINT].raw_to_ui(half.in_amount) == Decimal("0.15")
    rest = perp_from_dict(wallet.perps()[f"{SOL_MINT}:short"]["fill"])
    assert rest is not None and rest.size_usd == 15
    assert rest.collateral_usd == opened.collateral_usd / 2
    whole = await venue.close_perp(USDC, SHORT3, "c2")
    assert whole.perp is not None and whole.perp.size_usd == 15
    assert wallet.perps() == {}


async def test_added_collateral_moves_the_liquidation_away(tmp_path):
    venue, wallet, _, _ = _venue(tmp_path)
    opened = (await venue.open_perp(USDC, SHORT3, Decimal(10), "k")).perp
    assert opened is not None and opened.liquidation_price is not None
    held = (await venue.sweep([SHORT3])).held.get(SHORT3)
    assert held is not None and held.liquidation_price == opened.liquidation_price

    added = await venue.add_collateral(USDC, SHORT3, Decimal(5), "a")

    grown = added.perp
    assert grown is not None and grown.size_usd == 30
    assert grown.collateral_usd == opened.collateral_usd + 5
    assert (
        grown.liquidation_price and grown.liquidation_price > opened.liquidation_price
    )
    assert (added.in_amount, added.out_amount) == (5_000_000, 0)
    assert wallet.balance(USDC) == 85
    assert await venue.open_markets() == [(SOL_MINT, Direction.SHORT)]


async def test_each_send_is_logged_with_its_fill_and_resolves_from_it(tmp_path):
    from trader.execution.models.intent import PerpSendKind, send_hook

    venue, _, _, _ = _venue(tmp_path)
    logged = []
    token = send_hook.set(logged.append)
    try:
        opened = await venue.open_perp(USDC, SHORT3, Decimal(10), "k")
    finally:
        send_hook.reset(token)

    [sent] = logged
    assert sent.signature == opened.signature and sent.perp is not None
    assert sent.perp.kind == PerpSendKind.OPEN
    again = await venue.resolve_send(sent, SHORT3)
    assert again is not None and again.perp == opened.perp
    assert (again.in_amount, again.out_amount) == (opened.in_amount, opened.out_amount)
