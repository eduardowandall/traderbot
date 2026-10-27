"""A38: estresses (custos x N, choques) e o slippage pelo volume."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import FakeRange, candle, make_spec

from trader.backtest.replay import ReplayQuoteClient
from trader.backtest.scenarios import Scenario, is_fragile, run_matrix
from trader.backtest.stress import BASE, Shock, Stress, parse_shock, stresses
from trader.backtest.ticks import Tick
from trader.backtest.variants import variants
from trader.backtest.window import MAX_SLIPPAGE_SCALE, volume_scales
from trader.shared.models import SOLANA_MINTS, Interval

SOL = SOLANA_MINTS.get_by_symbol("SOL")
USDC = SOLANA_MINTS.get_by_symbol("USDC")
T0 = datetime(2025, 10, 10, 12, 0, tzinfo=UTC)
MINUTE = Interval.MINUTE_1
COSTS = {
    "fee_bps": Decimal(30),
    "slippage_bps": Decimal(10),
    "network_fee_usd": Decimal("0.002"),
    "borrow_bps_hour": Decimal(1),
}
BASE_SPEC = make_spec(
    entry={"mode": "all", "conditions": [{"type": "price_below", "value": 95}]},
    exit={
        "mode": "any",
        "stop": {"type": "stop_loss", "pct": 5},
        "conditions": [{"type": "take_profit", "pct": 3}],
    },
)


def _wave(t: datetime) -> Decimal:
    step = int((t - T0).total_seconds() // 60) % 20
    return Decimal(94 + (step if step < 10 else 20 - step) * Decimal("1.2"))


def _ticks(n: int = 5, price: int = 100) -> list[Tick]:
    return [Tick(T0 + timedelta(minutes=10 * i), Decimal(price)) for i in range(n)]


class TestParse:
    def test_costs_and_shocks_each_add_one_stress_after_the_base(self):
        found = stresses("base,2x,1.5x", ["-15%@50%", "-10%~1h"])

        assert [s.name for s in found] == [
            "base",
            "2x",
            "1.5x",
            "shock -15%@50%",
            "shock -10%~1h",
        ]
        assert found[1].costs == 2 and found[0] is BASE
        assert found[4].shock == Shock(Decimal(-10), Decimal("0.5"), timedelta(hours=1))

    def test_a_shock_position_defaults_to_the_middle(self):
        assert parse_shock("+5%") == Shock(Decimal(5), Decimal("0.5"))
        assert parse_shock("-20%@25%").at == Decimal("0.25")

    @pytest.mark.parametrize(
        "bad", ["-15", "-15%@150%", "-100%", "drop", "-5%~1w", "-10%~0h"]
    )
    def test_bad_shocks(self, bad):
        with pytest.raises(ValueError, match="--shock"):
            parse_shock(bad)

    @pytest.mark.parametrize("bad", ["2", "x", "-1x", "0x", "nanx", "infx"])
    def test_bad_costs(self, bad):
        with pytest.raises(ValueError, match="--costs"):
            stresses(bad, [])


class TestShock:
    def test_a_gap_moves_every_later_price(self):
        ticks = _ticks()  # 0, 10, ..., 40 min
        moved = Shock(Decimal(-15)).apply(ticks, T0, T0 + timedelta(minutes=40))

        assert [t.price for t in moved] == [100, 100, 85, 85, 85]
        assert [t.timestamp for t in moved] == [t.timestamp for t in ticks]

    def test_a_drop_recovers_in_a_straight_line(self):
        shock = Shock(Decimal(-10), Decimal(0), timedelta(minutes=20))
        moved = shock.apply(_ticks(), T0, T0 + timedelta(minutes=40))

        assert [t.price for t in moved] == [90, 95, 100, 100, 100]

    def test_naive_local_candle_times_are_compared_in_utc(self):
        naive = [candle(T0 + timedelta(minutes=10 * i), Decimal(100)) for i in range(5)]
        ticks = [Tick(c.timestamp, c.last) for c in naive]
        moved = Shock(Decimal(-15)).apply(ticks, T0, T0 + timedelta(minutes=40))
        assert [t.price for t in moved] == [100, 100, 85, 85, 85]


def test_a_cost_stress_multiplies_the_fee_and_the_slippage():
    doubled = Stress("2x", costs=Decimal(2)).scaled(COSTS)

    assert (doubled["fee_bps"], doubled["slippage_bps"]) == (60, 20)
    # a taxa de rede é fixa por transação; o empréstimo é de perp
    assert doubled["network_fee_usd"] == COSTS["network_fee_usd"]
    assert doubled["borrow_bps_hour"] == COSTS["borrow_bps_hour"]
    assert BASE.scaled(COSTS) is COSTS


async def test_each_stress_is_a_row_and_the_ranking_stays_on_the_base():
    found = variants(BASE_SPEC, [])
    windows = [Scenario("a", T0, T0 + timedelta(hours=3))]
    found_stresses = stresses("3x", ["-20%@50%"])

    run = await run_matrix(
        FakeRange({SOL.mint: _wave}),
        found,
        windows,
        COSTS,
        now=T0 + timedelta(days=1),
        stresses=found_stresses,
    )

    [variant] = run["variants"]
    assert [r["stress"] for r in variant["windows"]] == [
        "base",
        "3x",
        "shock -20%@50%",
    ]
    base, tripled, shocked = variant["windows"]
    assert variant["summary"]["ran"] == 1  # só a base ranqueia
    assert set(variant["stress"]) == {"3x", "shock -20%@50%"}
    assert float(tripled["result"]["return_pct"]) < float(base["result"]["return_pct"])
    assert shocked["hold_pct"] < base["hold_pct"] - 15  # o choque está nos ticks


def test_fragile_wins_on_the_base_and_loses_under_some_stress():
    win = {"ran": 1, "median_return_pct": 2.0}
    lose = {"ran": 1, "median_return_pct": -1.0}
    failed = {"ran": 0}

    assert is_fragile(win, {"2x": lose, "shock": win})
    assert not is_fragile(win, {"2x": win, "shock": failed})  # sem prova
    assert not is_fragile(lose, {"2x": lose})
    assert not is_fragile(win, {})


class TestVolumeSlippage:
    def test_scales_are_the_median_over_the_bar_clamped(self):
        bars = [candle(T0 + timedelta(minutes=i), Decimal(1)) for i in range(4)]
        for c, volume in zip(bars, [100, 50, 400, 1], strict=True):
            c.volume = Decimal(volume)
        # mediana 75: 100 e 400 pagam o dado; 50 paga 1.5x; 1 bateria no teto
        scales = volume_scales(bars, MINUTE)

        assert scales is not None
        assert [scales(c.timestamp) for c in bars] == [
            1,
            Decimal("1.5"),
            1,
            MAX_SLIPPAGE_SCALE,
        ]
        # uma barra sem candle (sem negócio) paga o máximo
        assert scales(T0 + timedelta(minutes=9)) == MAX_SLIPPAGE_SCALE

    def test_candles_without_volume_scale_nothing(self):
        assert volume_scales([candle(T0, Decimal(1))], MINUTE) is None

    async def test_the_quote_pays_fee_plus_scaled_slippage(self):
        amount = 100_000_000  # 100 USDC
        plain = ReplayQuoteClient(USDC, Decimal(30), Decimal(10))
        thin = ReplayQuoteClient(USDC, Decimal(30), Decimal(10), lambda ts: Decimal(3))
        plain.tick = thin.tick = Tick(T0, Decimal(100))

        # 30 + 10 bps contra 30 + 30 bps sobre 1 SOL
        got = await plain.get_quote(USDC.mint, SOL.mint, amount)
        assert int(got.outAmount) == 996_000_000
        got = await thin.get_quote(USDC.mint, SOL.mint, amount)
        assert int(got.outAmount) == 994_000_000
