"""A37: as células de `run_matrix` em processos dão o mesmo que no processo."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import FakeCandleApi, make_spec

from trader.backtest.scenarios import Scenario, run_matrix
from trader.backtest.variants import parse_vary, variants
from trader.execution.market import JupiterCandles
from trader.execution.market.history import CandleCache

T0 = datetime(2025, 10, 10, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=30)  # os pedaços já fecharam: vão para o cache
COSTS = {
    "fee_bps": Decimal(30),
    "slippage_bps": Decimal(10),
    "network_fee_usd": Decimal("0.002"),
    "borrow_bps_hour": Decimal(1),
}
# a API falsa oscila entre 99 e 106.5 a cada 7 min
BASE = make_spec(
    entry={"mode": "all", "conditions": [{"type": "price_below", "value": 102}]},
    exit={
        "mode": "any",
        "stop": {"type": "stop_loss", "pct": 5},
        "conditions": [{"type": "take_profit", "pct": 3}],
    },
)


class NoNetwork:
    async def get_candles(self, *args, **kwargs):
        raise AssertionError("tudo devia vir do cache")

    async def aclose(self):
        pass


async def test_parallel_cells_equal_the_serial_ones():
    found = variants(BASE, parse_vary(["exit.conditions.0.pct=2,5"]))
    windows = [
        Scenario("a", T0, T0 + timedelta(hours=3)),
        Scenario("b", T0 + timedelta(hours=4), T0 + timedelta(hours=7), ("holdout",)),
    ]
    filled = JupiterCandles(FakeCandleApi(), CandleCache(), now=lambda: LATER)  # type: ignore[arg-type]
    serial = await run_matrix(filled, found, windows, COSTS, now=LATER)

    cached = JupiterCandles(NoNetwork(), CandleCache(), now=lambda: LATER)  # type: ignore[arg-type]
    parallel = await run_matrix(cached, found, windows, COSTS, now=LATER, jobs=2)

    assert parallel == serial
    assert sum(v["summary"]["trades"] for v in serial["variants"]) > 0


async def test_a_window_still_forming_runs_in_the_parent():
    # o pedaço do fim de uma janela de agora não vai para o cache: um worker
    # leria outros candles; ela roda no processo, a antiga nos workers
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    found = variants(BASE, parse_vary(["exit.conditions.0.pct=2"]))
    windows = [
        Scenario("old", T0, T0 + timedelta(hours=3)),
        Scenario("now", now - timedelta(hours=3), now),
    ]
    api = FakeCandleApi()
    source = JupiterCandles(api, CandleCache(), now=lambda: now)  # type: ignore[arg-type]
    serial = await run_matrix(source, found, windows, COSTS, now=now)

    parallel = await run_matrix(source, found, windows, COSTS, now=now, jobs=2)

    assert parallel == serial
