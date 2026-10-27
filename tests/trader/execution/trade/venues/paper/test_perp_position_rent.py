"""A25: a conta de uma posição perp paga rent uma vez por mercado e lado.

A Jupiter nunca fecha a conta da posição e a reusa nas aberturas seguintes:
o paper (a carteira lembra as contas que criou) e o replay cobram o rent só
na primeira abertura, como `rent_lamports` dela, como o real.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

from factories import PerpOracle

from trader.backtest import Tick
from trader.backtest.replay import ReplayPerpsVenue, ReplayPrices, ReplayQuoteClient
from trader.execution.models.intent import send_hook
from trader.execution.models.perp import PerpTerms
from trader.execution.trade.venues.paper import SimulatedWallet
from trader.execution.trade.venues.paper.perps import (
    DEFAULT_PERP_POSITION_RENT_LAMPORTS,
    SimulatedPerpsVenue,
)
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
T0 = datetime(2026, 10, 12, 17, tzinfo=UTC)
SHORT3 = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
LONG3 = PerpTerms(SOL_MINT, Direction.LONG, Decimal(3))
RENT = 1_747_520
FEE = 5000  # a taxa base de cada envio do paper


def _venue(path) -> tuple[SimulatedPerpsVenue, SimulatedWallet]:
    wallet = SimulatedWallet(path, initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    return SimulatedPerpsVenue(wallet, PerpOracle("100"), clock=lambda: T0), wallet


def _rent(result) -> int:
    assert result.costs is not None
    return result.costs.rent_lamports


def test_the_default_is_the_measured_rent():
    assert DEFAULT_PERP_POSITION_RENT_LAMPORTS == RENT


async def test_only_the_first_open_of_a_market_and_side_pays_the_rent(tmp_path):
    venue, wallet = _venue(tmp_path / "w.json")
    first = await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "a")
    await venue.close_perp(USDC.mint, SHORT3, "b")
    again = await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "c")
    other_side = await venue.open_perp(USDC.mint, LONG3, Decimal(10), "d")

    assert [_rent(r) for r in (first, again, other_side)] == [RENT, 0, RENT]
    assert wallet.raw_balance(SOL_MINT) == 10**9 - 4 * FEE - 2 * RENT


async def test_the_wallet_file_remembers_the_accounts(tmp_path):
    path = tmp_path / "w.json"
    venue, _ = _venue(path)
    await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "a")
    await venue.close_perp(USDC.mint, SHORT3, "b")

    # outro processo (um `serve` reiniciado) lê o mesmo arquivo
    restarted = SimulatedPerpsVenue(
        SimulatedWallet(path), PerpOracle("100"), clock=lambda: T0
    )
    assert _rent(await restarted.open_perp(USDC.mint, SHORT3, Decimal(10), "c")) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["perp_accounts"] == [
        f"{SOL_MINT}:short"
    ]


def test_an_older_wallet_file_counts_its_open_positions_as_accounts(tmp_path):
    path = tmp_path / "w.json"
    SimulatedWallet(path, initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["perp_accounts"]
    data["perps"] = {f"{SOL_MINT}:short": {}}
    path.write_text(json.dumps(data), encoding="utf-8")

    wallet = SimulatedWallet(path)
    assert not wallet.needs_perp_account(f"{SOL_MINT}:short")
    assert wallet.needs_perp_account(f"{SOL_MINT}:long")


async def test_a_reset_forgets_the_accounts(tmp_path):
    venue, wallet = _venue(tmp_path / "w.json")
    await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "a")
    wallet.reset({"USDC": Decimal(100), "SOL": Decimal(1)})
    assert wallet.needs_perp_account(f"{SOL_MINT}:short")


async def test_a_resolved_open_keeps_its_rent(tmp_path):
    # A3: a abertura resolvida depois de um crash traz o rent do registro
    venue, _ = _venue(tmp_path / "w.json")
    logged = []
    token = send_hook.set(logged.append)
    try:
        await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "a")
        await venue.close_perp(USDC.mint, SHORT3, "b")
        await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "c")
    finally:
        send_hook.reset(token)

    first, _, again = logged
    assert _rent(await venue.resolve_send(first, SHORT3)) == RENT
    assert _rent(await venue.resolve_send(again, SHORT3)) == 0


async def test_the_replay_charges_it_on_the_first_open_only():
    client = ReplayQuoteClient(USDC, Decimal(0))
    client.tick = Tick(T0, Decimal(100))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100)})  # sem SOL
    venue = ReplayPerpsVenue(
        wallet, ReplayPrices(client, SOL, USDC), Decimal(0), Decimal(0)
    )

    first = await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "a")
    await venue.close_perp(USDC.mint, SHORT3, "b")
    again = await venue.open_perp(USDC.mint, SHORT3, Decimal(10), "c")

    assert [_rent(first), _rent(again)] == [RENT, 0]
    # fora da carteira, como a taxa de rede: o patrimônio o desconta
    assert venue.fees_usd == Decimal(RENT) / 10**9 * 100  # SOL a 100 no tick
