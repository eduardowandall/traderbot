"""A35: o catálogo de cenários, a seleção, as janelas rolantes e o run."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from factories import FakeRange, candle, make_spec

from trader.backtest.scenarios import (
    CATALOG,
    TAG_CHECKS,
    Scenario,
    WindowStats,
    flags,
    hold_return_pct,
    load_catalog,
    rolling,
    run_scenarios,
    select,
    window_stats,
)
from trader.backtest.stress import parse_duration
from trader.backtest.ticks import Tick
from trader.shared.indicators import to_utc
from trader.shared.models import SOLANA_MINTS, Interval, TickerData
from trader.strategy.spec.models import StrategySpec

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
T0 = datetime(2025, 10, 10, 12, 0, tzinfo=UTC)
MINUTE = Interval.MINUTE_1
COSTS = {
    "fee_bps": Decimal(30),
    "slippage_bps": Decimal(10),
    "network_fee_usd": Decimal("0.002"),
    "borrow_bps_hour": Decimal(1),
}
# compra abaixo de 100, sai com 3% de lucro ou 5% de perda
SWING = {
    "entry": {"mode": "all", "conditions": [{"type": "price_below", "value": 100}]},
    "exit": {
        "mode": "any",
        "stop": {"type": "stop_loss", "pct": 5},
        "conditions": [{"type": "take_profit", "pct": 3}],
    },
}


def _wave(t: datetime) -> Decimal:
    """Sobe e desce entre 94 e 106 a cada 20 min."""
    step = int((t - T0).total_seconds() // 60) % 20
    return Decimal(94 + (step if step < 10 else 20 - step) * Decimal("1.2"))


def _catalog(tmp_path, *items) -> Path:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps({"scenarios": list(items)}), encoding="utf-8")
    return path


def _item(id_: str, hours: int = 2, start: datetime = T0, tags=("quiet",)) -> dict:
    end = start + timedelta(hours=hours)
    return {
        "id": id_,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tags": list(tags),
    }


class TestCatalog:
    def test_the_projects_catalog_loads_with_every_regime(self):
        catalog = load_catalog(CATALOG)

        tags = {tag for s in catalog for tag in s.tags}
        assert set(TAG_CHECKS) <= tags
        assert "holdout" in tags
        assert all(s.start.tzinfo is UTC and s.end > s.start for s in catalog)
        # um holdout por regime, para A36 nunca escolher nele
        for regime in ("bullish", "bearish", "choppy", "quiet", "black_swan"):
            assert any({regime, "holdout"} <= set(s.tags) for s in catalog)

    def test_refuses_a_repeated_id(self, tmp_path):
        with pytest.raises(ValueError, match="repetidos no catálogo: a"):
            load_catalog(_catalog(tmp_path, _item("a"), _item("a")))

    def test_refuses_a_time_without_zone(self, tmp_path):
        naive = {**_item("a"), "start": "2025-10-10T12:00:00"}
        with pytest.raises(ValueError, match="precisa de fuso"):
            load_catalog(_catalog(tmp_path, naive))

    def test_refuses_an_end_before_the_start(self, tmp_path):
        backwards = {**_item("a"), "end": "2025-10-10T11:00:00Z"}
        with pytest.raises(ValueError, match="não vem depois"):
            load_catalog(_catalog(tmp_path, backwards))


class TestSelect:
    CATALOG = [
        Scenario("a", T0, T0 + timedelta(hours=1), ("bearish",)),
        Scenario("b", T0, T0 + timedelta(hours=1), ("bullish", "holdout")),
        Scenario("c", T0, T0 + timedelta(hours=1), ("bearish",)),
    ]

    def test_by_tag_id_or_all_in_catalog_order(self):
        ids = [s.id for s in select(self.CATALOG, ["c", "tag:bearish"])]
        assert ids == ["a", "c"]
        assert [s.id for s in select(self.CATALOG, ["all"])] == ["a", "b", "c"]

    def test_an_unknown_selector_is_an_error(self):
        with pytest.raises(ValueError, match="nenhum cenário para 'tag:quiet'"):
            select(self.CATALOG, ["tag:quiet"])


class TestRolling:
    def test_durations(self):
        assert parse_duration("30m") == timedelta(minutes=30)
        assert parse_duration("12h") == timedelta(hours=12)
        assert parse_duration("7d") == timedelta(days=7)
        for bad in ("7", "0d", "1w", "d"):
            with pytest.raises(ValueError, match="duração inválida"):
                parse_duration(bad)

    def test_windows_that_fit_one_per_step(self):
        end = T0 + timedelta(days=3, hours=5)
        windows = rolling(T0, end, timedelta(days=1))

        assert [w.start for w in windows] == [T0 + timedelta(days=i) for i in range(3)]
        assert windows[0].id == "rolling-20251010T1200"
        assert windows[0].tags == ("rolling",)
        overlapping = rolling(T0, end, timedelta(days=1), timedelta(hours=12))
        assert len(overlapping) == 5

    def test_no_window_fits(self):
        with pytest.raises(ValueError, match="nenhuma janela"):
            rolling(T0, T0 + timedelta(hours=1), timedelta(days=1))


def _bars(*closes: float) -> list[TickerData]:
    return [
        candle(T0 + timedelta(minutes=i), Decimal(str(c))) for i, c in enumerate(closes)
    ]


class TestStats:
    def test_return_drawdown_and_range(self):
        stats = window_stats(_bars(100, 120, 90, 110), MINUTE)

        assert stats.return_pct == pytest.approx(10)
        assert stats.max_drawdown_pct == pytest.approx(25)  # 120 -> 90
        assert stats.range_pct == pytest.approx(120 / 90 * 100 - 100)

    def test_hold_is_in_usd_like_the_equity(self):
        # JUP-SOL: o par sobe 10% e o SOL cai 50%: segurar perde 45% em USD
        ticks = [
            Tick(T0, Decimal(1), Decimal(200)),
            Tick(T0, Decimal("1.1"), Decimal(100)),
        ]
        assert hold_return_pct(ticks) == pytest.approx(-45)
        assert hold_return_pct([Tick(T0, Decimal(2)), Tick(T0, Decimal(3))]) == 50

    def test_a_flat_window_has_no_volatility(self):
        assert window_stats(_bars(5, 5, 5), MINUTE).daily_vol_pct == 0

    def test_a_tag_the_pair_disagrees_with_is_flagged(self):
        rising = WindowStats(8.0, 2.0, 12.0, 3.0)

        assert flags(["bullish", "long", "holdout"], rising) == []
        [flag] = flags(["bearish"], rising)
        assert flag.startswith("[!] bearish: par +8.0%")

    def test_choppy_is_a_range_that_dwarfs_the_move(self):
        check = TAG_CHECKS["choppy"]
        assert check(WindowStats(4.2, 27, 43, 9))
        assert not check(WindowStats(4.2, 3, 8, 2))  # faixa pequena
        assert not check(WindowStats(-20, 25, 30, 9))  # uma queda, não vaivém


async def test_the_window_end_replaces_the_specs_expiry():
    # ttl_days 1 pararia no primeiro dia; numa janela de 3 dias, compra até o fim
    trough = {**SWING["entry"], "conditions": [{"type": "price_below", "value": 95}]}
    spec = StrategySpec.model_validate(
        make_spec(expires_at=None, ttl_days=1, **{**SWING, "entry": trough})
    )
    window = [Scenario("long", T0, T0 + timedelta(days=3))]
    source = FakeRange({SOL: _wave})

    run = await run_scenarios(source, spec, window, COSTS, now=T0 + timedelta(days=9))

    [row] = run["windows"]
    buys = [t for t in row["result"]["trades"] if t["side"] == "buy"]
    assert to_utc(buys[-1]["timestamp"]) > T0 + timedelta(days=2)
    assert row["result"]["spec_id"] == spec.spec_id()


async def test_each_window_runs_and_a_failed_one_does_not_stop_the_rest():
    spec = StrategySpec.model_validate(make_spec(**SWING))
    gap = {T0 + timedelta(days=1, minutes=m) for m in range(90)}  # > MAX_HOLE
    source = FakeRange({SOL: _wave}, missing=gap)
    scenarios = [
        Scenario("ok", T0, T0 + timedelta(hours=3), ("choppy",)),
        Scenario("hole", T0 + timedelta(days=1), T0 + timedelta(days=1, hours=3)),
    ]

    run = await run_scenarios(
        source, spec, scenarios, COSTS, now=T0 + timedelta(days=9)
    )

    ok, hole = run["windows"]
    assert run["spec_id"] == spec.spec_id() == ok["result"]["spec_id"]
    assert ok["result"]["closed_trades"] > 0
    assert ok["coverage"]["pct"] == 100.0
    assert ok["flags"] == []  # sobe e desce 12% sem sair do lugar: choppy
    assert ok["vs_hold_pct"] == pytest.approx(
        float(ok["result"]["return_pct"]) - ok["hold_pct"]
    )
    assert "a API não tem candles" in hole["error"]
    summary = run["summary"]
    assert (summary["windows"], summary["ran"]) == (2, 1)
    assert summary["worst_id"] == "ok"
    assert summary["trades"] == ok["result"]["closed_trades"]


async def test_a_bigger_max_hole_accepts_a_quiet_token():
    spec = StrategySpec.model_validate(make_spec(**SWING))
    gap = {T0 + timedelta(minutes=m) for m in range(30, 120)}
    source = FakeRange({SOL: _wave}, missing=gap)
    window = [Scenario("thin", T0, T0 + timedelta(hours=3))]
    now = T0 + timedelta(days=1)

    refused = await run_scenarios(source, spec, window, COSTS, now=now)
    accepted = await run_scenarios(
        source, spec, window, COSTS, now=now, max_hole=timedelta(hours=2)
    )

    assert "error" in refused["windows"][0]
    assert "error" not in accepted["windows"][0]
    assert accepted["windows"][0]["coverage"]["bars"] == 90
