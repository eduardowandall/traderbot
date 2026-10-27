"""A36: variantes de uma spec e a matriz variantes x janelas."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import FakeRange, make_spec

from trader.backtest.scenarios import Scenario, rank, run_matrix, summarize
from trader.backtest.variants import parse_vary, variants
from trader.shared.models import SOLANA_MINTS

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
T0 = datetime(2025, 10, 10, 12, 0, tzinfo=UTC)
COSTS = {
    "fee_bps": Decimal(30),
    "slippage_bps": Decimal(10),
    "network_fee_usd": Decimal("0.002"),
    "borrow_bps_hour": Decimal(1),
}
# compra perto do fundo de uma onda 94..106, sai com lucro ou perda
BASE = make_spec(
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


class TestParseVary:
    def test_values_are_json_literals_or_text(self):
        assert parse_vary(["exit.stop.pct=3,4.5", "entry.mode=any,all"]) == [
            ("exit.stop.pct", [3, 4.5]),
            ("entry.mode", ["any", "all"]),
        ]

    @pytest.mark.parametrize("bad", ["exit.stop.pct", "=3", "exit.stop.pct="])
    def test_needs_a_path_and_values(self, bad):
        with pytest.raises(ValueError, match="use CAMINHO=V1,V2"):
            parse_vary([bad])

    @pytest.mark.parametrize("axis", ["symbol=JUP-SOL", "timeframe=15_SECOND"])
    def test_fields_that_change_the_data_are_refused(self, axis):
        with pytest.raises(ValueError, match="mudam os dados"):
            variants(BASE, parse_vary([axis]))


class TestVariants:
    def test_the_product_with_the_base_first_and_no_repeats(self):
        axes = parse_vary(["exit.stop.pct=5,7", "exit.conditions.0.pct=3,6"])

        found = variants(BASE, axes)

        # (5, 3) é a própria base: 1 + 4 - 1
        assert [v.changes for v in found] == [
            {},
            {"exit.stop.pct": 5, "exit.conditions.0.pct": 6},
            {"exit.stop.pct": 7, "exit.conditions.0.pct": 3},
            {"exit.stop.pct": 7, "exit.conditions.0.pct": 6},
        ]
        assert len({v.spec.spec_id() for v in found}) == 4
        assert found[3].data["exit"]["stop"]["pct"] == 7
        assert BASE["exit"]["stop"]["pct"] == 5  # a base não muda

    def test_a_missing_path_is_a_typo(self):
        with pytest.raises(ValueError, match="não tem 'pcct'"):
            variants(BASE, parse_vary(["exit.stop.pcct=3"]))
        with pytest.raises(ValueError, match="não tem '4'"):
            variants(BASE, parse_vary(["exit.conditions.4.pct=3"]))

    def test_a_refused_value_names_the_path(self):
        with pytest.raises(ValueError, match="exit.stop.pct=-1"):
            variants(BASE, parse_vary(["exit.stop.pct=-1"]))


def _result(spec_id: str, median=None, worst=None) -> dict:
    if median is None:
        return {"spec_id": spec_id, "summary": {"windows": 1, "ran": 0}}
    summary = {"ran": 1, "median_return_pct": median, "worst_return_pct": worst}
    return {"spec_id": spec_id, "summary": summary}


def test_rank_by_median_then_worst_and_empty_last():
    results = [
        _result("none"),
        _result("a", 1.0, -9.0),
        _result("b", 2.0, -5.0),
        _result("c", 1.0, -2.0),
    ]
    assert [r["spec_id"] for r in rank(results)] == ["b", "c", "a", "none"]


def test_every_seed_of_a_window_counts_in_the_summary():
    def row(seed: str, ret: str) -> dict:
        result = {"return_pct": ret, "max_drawdown_pct": "1", "closed_trades": 1}
        return {
            "id": "w",
            "seed": seed,
            "tags": [],
            "vs_hold_pct": 0.0,
            "result": result,
        }

    summary = summarize([row("0", "-4"), row("1", "2"), row("2", "6")])

    assert summary["windows"] == 1 and summary["ran"] == 3
    assert summary["median_return_pct"] == 2
    assert (summary["worst_return_pct"], summary["worst_id"]) == (-4, "w#0")
    assert summary["profitable_pct"] == pytest.approx(200 / 3)


async def test_each_window_is_read_once_for_every_variant_and_seed():
    found = variants(BASE, parse_vary(["exit.conditions.0.pct=2,6"]))
    source = FakeRange({SOL: _wave})
    windows = [
        Scenario("a", T0, T0 + timedelta(hours=3)),
        Scenario(
            "held", T0 + timedelta(hours=4), T0 + timedelta(hours=7), ("holdout",)
        ),
    ]

    run = await run_matrix(
        source, found, windows, COSTS, ("0", "1"), now=T0 + timedelta(days=1)
    )

    assert len(source.asked) == 2  # uma leitura por janela
    assert run["ranking"][0] == run["best"]
    assert {v["spec_id"] for v in run["variants"]} == {v.spec.spec_id() for v in found}
    for variant in run["variants"]:
        assert [(r["id"], r["seed"]) for r in variant["windows"]] == [
            ("a", "0"),
            ("a", "1"),
            ("held", "0"),
            ("held", "1"),
        ]
        # o holdout nunca entra no que ranqueia; cada semente conta
        assert variant["summary"]["ran"] == 2
        assert variant["holdout"]["ran"] == 2
        assert variant["summary"]["worst_id"].startswith("a#")
        assert "trades" not in variant["windows"][0]["result"]
    medians = [v["summary"]["median_return_pct"] for v in run["variants"]]
    assert medians == sorted(medians, reverse=True)


async def test_a_window_that_cannot_be_read_is_an_error_for_every_variant():
    found = variants(BASE, parse_vary(["exit.conditions.0.pct=2"]))
    gap = {T0 + timedelta(minutes=m) for m in range(90)}
    source = FakeRange({SOL: _wave}, missing=gap)
    windows = [Scenario("hole", T0, T0 + timedelta(hours=3))]

    run = await run_matrix(source, found, windows, COSTS, now=T0 + timedelta(days=1))

    for variant in run["variants"]:
        [row] = variant["windows"]
        assert "a API não tem candles" in row["error"]
        assert variant["summary"]["ran"] == 0
