"""A34: a história em pedaços de 1000 barras, com cache em disco."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from factories import FakeCandleApi

from trader.execution.market import JupiterCandles
from trader.execution.market.history import (
    CHUNK_BARS,
    CandleCache,
    Chunk,
    chunks,
)
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.indicators import to_utc
from trader.shared.models import Interval
from trader.shared.paths import candles_dir

MINUTE = Interval.MINUTE_1
SPAN = CHUNK_BARS * 60  # segundos de um pedaço de 1 min
K = 1_760_000_000 // SPAN  # um pedaço qualquer de 2025-10
T0 = datetime.fromtimestamp(K * SPAN, UTC)
LATER = T0 + timedelta(days=30)  # todos os pedaços de perto de T0 já fecharam


def _candles(client, now=LATER, root=None) -> JupiterCandles:
    return JupiterCandles(client, CandleCache(root), now=lambda: now)  # type: ignore[arg-type]


def test_chunks_are_aligned_to_the_epoch():
    got = chunks(MINUTE, T0 + timedelta(minutes=5), T0 + timedelta(seconds=SPAN + 1))

    assert [c.start for c in got] == [T0, T0 + timedelta(seconds=SPAN)]
    assert got[0].end == got[1].start
    assert chunks(MINUTE, T0, T0 + timedelta(seconds=SPAN)) == [Chunk(MINUTE, T0)]


def test_a_chunk_is_final_a_bar_and_a_minute_after_it_ends():
    chunk = Chunk(MINUTE, T0)

    assert not chunk.is_final(chunk.end + timedelta(seconds=119))
    assert chunk.is_final(chunk.end + timedelta(seconds=120))


async def test_between_asks_once_per_chunk_with_its_end():
    client = FakeCandleApi()
    data = _candles(client)
    start, end = T0 + timedelta(minutes=990), T0 + timedelta(minutes=1020)

    got = await data.between("mint", MINUTE, start, end)
    await data.aclose()

    assert [to_utc(c.timestamp) for c in got] == [
        start + timedelta(minutes=i) for i in range(30)
    ]
    assert sorted(call[3] for call in client.calls) == [  # type: ignore[type-var]
        T0 + timedelta(seconds=SPAN),
        T0 + timedelta(seconds=2 * SPAN),
    ]
    assert {call[2] for call in client.calls} == {CHUNK_BARS}
    assert got[0].volume == Decimal("12.25")
    assert client.closed


async def test_final_chunks_are_read_back_from_disk():
    start, end = T0, T0 + timedelta(minutes=10)
    first = await _candles(FakeCandleApi()).between("mint", MINUTE, start, end)

    again = FakeCandleApi()
    second = await _candles(again).between("mint", MINUTE, start, end)

    assert again.calls == []
    assert second == first
    assert (candles_dir() / "mint" / "1_MINUTE" / f"{T0:%Y%m%dT%H%M%S}.csv").exists()


async def test_the_open_chunk_is_never_cached():
    start, end = T0, T0 + timedelta(minutes=10)
    await _candles(FakeCandleApi(), now=end).between("mint", MINUTE, start, end)

    again = FakeCandleApi()
    await _candles(again, now=end).between("mint", MINUTE, start, end)

    assert len(again.calls) == 1


async def test_an_empty_chunk_is_asked_again():
    every_bar = {T0 + timedelta(minutes=i) for i in range(CHUNK_BARS)}
    start, end = T0, T0 + timedelta(minutes=10)
    assert (
        await _candles(FakeCandleApi(every_bar)).between("m", MINUTE, start, end) == []
    )

    again = FakeCandleApi()
    assert len(await _candles(again).between("m", MINUTE, start, end)) == 10
    assert len(again.calls) == 1


async def test_missing_bars_stay_missing():
    gap = {T0 + timedelta(minutes=3), T0 + timedelta(minutes=4)}
    data = _candles(FakeCandleApi(gap))

    got = await data.between("mint", MINUTE, T0, T0 + timedelta(minutes=6))

    assert [to_utc(c.timestamp).minute for c in got] == [
        (T0 + timedelta(minutes=i)).minute for i in (0, 1, 2, 5)
    ]


async def test_the_client_sends_the_end_as_to_in_ms():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"candles": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncJupiterClient(client=http)
    await client.get_candles("mint", MINUTE, 1000, end=T0)
    await http.aclose()

    assert seen[0].params["to"] == str(int(T0.timestamp() * 1000))
    assert seen[0].params["candles"] == "1000"
