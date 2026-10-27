"""A35: o catálogo de cenários contra os candles reais do SOL.

Os rótulos foram escolhidos no SOL (2026-10-21): cada um deve bater com o
que a janela mede a 1 h, e as janelas de um dia e de uma semana devem rodar a
15 s sem buraco.
"""

import asyncio
from contextlib import aclosing

from live_helpers import invoke_json

from trader.backtest.scenarios import flags, load_catalog, window_stats
from trader.backtest.window import holes
from trader.execution.market import JupiterCandles
from trader.shared.models import SOLANA_MINTS, Interval
from trader.shared.paths import PROJECT_ROOT

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
CATALOG = load_catalog()


def _read(interval: Interval, scenarios) -> list:
    async def read():
        async with aclosing(JupiterCandles()) as data:
            return await asyncio.gather(
                *(data.between(SOL, interval, s.start, s.end) for s in scenarios)
            )

    return asyncio.run(read())


def test_every_tag_agrees_with_sols_hourly_bars():
    series = _read(Interval.HOUR_1, CATALOG)

    disagree = {
        s.id: flags(s.tags, window_stats(candles, Interval.HOUR_1))
        for s, candles in zip(CATALOG, series, strict=True)
    }
    assert {k: v for k, v in disagree.items() if v} == {}


def test_short_and_weekly_windows_have_no_15s_hole():
    short = [s for s in CATALOG if "long" not in s.tags]
    series = _read(Interval.SECOND_15, short)

    found = {
        s.id: holes(candles, Interval.SECOND_15, s.start, s.end)
        for s, candles in zip(short, series, strict=True)
    }
    assert {k: v for k, v in found.items() if v} == {}


def test_variants_on_a_real_window():
    # A36: duas variantes do exemplo no crash de 2025-10-10, a 1 min
    spec = str(PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json")
    result = invoke_json(
        "backtest",
        spec,
        "--scenario",
        "crash-2025-10-10",
        "--vary",
        "exit.stop.pct=6",
        "--fee-bps",
        "30",
        "--network-fee-usd",
        "0.002",
    )

    assert len(result["variants"]) == 2
    assert result["best"] == result["ranking"][0] == result["variants"][0]["spec_id"]
    for variant in result["variants"]:
        [row] = variant["windows"]
        assert "error" not in row, row
        assert row["result"]["bars"] == 1440


def test_stresses_on_a_real_window():
    # A38: custos x2, um degrau e o slippage pelo volume no crash de 2025-10-10
    spec = str(PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json")
    result = invoke_json(
        "backtest",
        spec,
        "--scenario",
        "crash-2025-10-10",
        "--costs",
        "2x",
        "--shock",
        "-15%@50%",
        "--volume-slippage",
        "--fee-bps",
        "30",
        "--network-fee-usd",
        "0.002",
    )

    [variant] = result["variants"]
    base, doubled, shocked = variant["windows"]
    assert (base["stress"], doubled["stress"]) == ("base", "2x")
    # as três rodam; x2 nunca ganha da base; o degrau está no comprar e segurar
    assert "error" not in base and "error" not in shocked
    assert float(doubled["result"]["return_pct"]) <= float(base["result"]["return_pct"])
    assert shocked["hold_pct"] < base["hold_pct"]
