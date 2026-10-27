"""A34: a história da API de candles, em pedaços e com cache, contra a real.

Conferido à mão em 2026-10-21: com `to` no limite de uma barra vêm exatamente
as 1000 barras antes dele; SOL tem 15 s e 1 min desde ~2024-10 (com buracos)
e 1 h desde ~2024-06. Se um destes falhar, a `datapi` mudou.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from live_helpers import invoke_json

from trader.execution.market import JupiterCandles
from trader.execution.market.history import CHUNK_BARS
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.indicators import bar_index
from trader.shared.models import SOLANA_MINTS, Interval
from trader.shared.paths import PROJECT_ROOT

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
EXAMPLE_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json")
# o crash de 2025-10-10, que A35 usa como cenário
CRASH = datetime(2025, 10, 10, 18, 0, tzinfo=UTC)


class CountingClient(AsyncJupiterClient):
    def __init__(self):
        super().__init__()
        self.requests = 0

    async def get_candles(self, *args, **kwargs):
        self.requests += 1
        return await super().get_candles(*args, **kwargs)


def test_an_aligned_to_returns_exactly_the_bars_before_it():
    span = CHUNK_BARS * 15
    end = datetime.fromtimestamp(int(CRASH.timestamp()) // span * span, UTC)

    async def read():
        client = AsyncJupiterClient()
        try:
            return await client.get_candles(SOL, Interval.SECOND_15, CHUNK_BARS, end)
        finally:
            await client.aclose()

    times = [c["time"] for c in asyncio.run(read())]

    assert max(times) == end.timestamp() - 15
    assert min(times) >= end.timestamp() - span


def test_a_window_across_chunks_is_contiguous_and_cached():
    span = CHUNK_BARS * 15
    boundary = datetime.fromtimestamp(int(CRASH.timestamp()) // span * span, UTC)
    start, end = boundary - timedelta(minutes=10), boundary + timedelta(minutes=10)

    async def read():
        client = CountingClient()
        data = JupiterCandles(client)
        try:
            first = await data.between(SOL, Interval.SECOND_15, start, end)
            second = await data.between(SOL, Interval.SECOND_15, start, end)
        finally:
            await data.aclose()
        return first, second, client.requests

    first, second, requests = asyncio.run(read())

    bars = [bar_index(c.timestamp, 15) for c in first]
    assert bars == sorted(set(bars))
    # os dois lados da emenda de pedaços (SOL negocia a cada 15 s)
    edge = int(boundary.timestamp()) // 15
    assert {edge - 1, edge} <= set(bars)
    assert len(bars) >= 75  # de 80 barras
    assert requests == 2  # um por pedaço; a segunda leitura veio do disco
    assert second == first


def test_the_history_floors():
    async def count(interval: Interval, end: datetime) -> int:
        client = AsyncJupiterClient()
        try:
            return len(await client.get_candles(SOL, interval, CHUNK_BARS, end))
        finally:
            await client.aclose()

    async def read():
        return [
            await count(Interval.SECOND_15, datetime(2025, 1, 9, tzinfo=UTC)),
            await count(Interval.HOUR_1, datetime(2024, 8, 6, tzinfo=UTC)),
            await count(Interval.HOUR_1, datetime(2024, 3, 1, tzinfo=UTC)),
        ]

    recent_15s, hour_2024_08, hour_2024_03 = asyncio.run(read())

    assert recent_15s > 900
    assert hour_2024_08 > 900
    assert hour_2024_03 == 0  # o piso: antes de ~2024-06 não há nada


def test_backtest_of_a_past_window():
    result = invoke_json(
        "backtest",
        EXAMPLE_SPEC,
        "--from",
        CRASH.isoformat(),
        "--to",
        (CRASH + timedelta(hours=12)).isoformat(),
        "--fee-bps",
        "30",
        "--network-fee-usd",
        "0.002",
    )

    assert result["bars"] == 720
    assert result["coverage"]["pct"] > 95
    assert result["window"]["from"] == CRASH.isoformat()
    assert result["start"].startswith("2025-10-10T18:00")
