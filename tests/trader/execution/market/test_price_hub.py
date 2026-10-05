"""O hub de preços (B7): um websocket para todos, a Price API para os parados."""

import asyncio
from decimal import Decimal

import pytest

from trader.execution.market.hub import PriceHub, StalePriceError
from trader.execution.market.prices import usd_snapshot
from trader.shared.market import HubMarketData
from trader.shared.models import SOLANA_MINTS
from trader.shared.trading_service.protocol import (
    REMOTE_ERRORS,
    PriceUnavailableError,
)

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
JUP = SOLANA_MINTS.get_by_symbol("JUP").mint
BONK = SOLANA_MINTS.get_by_symbol("BONK").mint


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PriceApi:
    def __init__(self, prices=None):
        self.prices = prices or {SOL: Decimal(150), JUP: Decimal("0.5")}
        self.calls: list[list[str]] = []
        self.closed = False

    async def get_usd_prices(self, mints):
        self.calls.append(list(mints))
        return {m: self.prices[m] for m in mints if m in self.prices}

    async def aclose(self):
        self.closed = True


class Stream:
    """Um websocket falso: cada assinatura lê da fila de mensagens."""

    def __init__(self):
        self.subscriptions: list[set[str]] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    def __call__(self, mints):
        self.subscriptions.append(set(mints))
        return self._read(set(mints))

    async def _read(self, mints):
        while True:
            mint, price = await self.messages.get()
            if mint in mints:
                yield mint, price


async def _settle():
    for _ in range(30):
        await asyncio.sleep(0)


async def test_the_first_price_comes_from_the_api_right_away():
    api, clock = PriceApi(), Clock()
    hub = PriceHub(api, stream=None, monotonic=clock)  # type: ignore[arg-type]

    assert await hub.price(SOL) == (Decimal(150), 0.0)
    assert api.calls == [[SOL]]
    clock.now += 3
    assert await hub.price(SOL) == (Decimal(150), 3.0)
    assert api.calls == [[SOL]]  # o segundo pedido não chama a API


async def test_one_subscription_for_every_mint_and_a_new_one_when_one_is_added():
    api, stream = PriceApi(), Stream()
    hub = PriceHub(api, stream=stream)  # type: ignore[arg-type]
    task = asyncio.create_task(hub.run())
    try:
        await hub.price(SOL)
        await _settle()
        await hub.price(JUP)
        await _settle()
        assert stream.subscriptions == [{SOL}, {SOL, JUP}]

        stream.messages.put_nowait((JUP, Decimal("0.6")))
        await _settle()
        assert (await hub.price(JUP))[0] == Decimal("0.6")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert api.closed


async def test_quiet_mints_are_polled_together_and_fresh_ones_left_alone():
    api, clock = (
        PriceApi({SOL: Decimal(150), JUP: Decimal("0.5"), BONK: Decimal(1)}),
        Clock(),
    )
    hub = PriceHub(api, stream=None, monotonic=clock, rest_after=5)  # type: ignore[arg-type]
    for mint in (SOL, JUP, BONK):
        await hub.price(mint)
    clock.now += 6
    hub._set(SOL, Decimal(151))  # o websocket trouxe SOL agora
    api.calls.clear()

    await hub.poll_stale()

    assert api.calls == [sorted([JUP, BONK])]  # uma chamada, só os parados


async def test_an_old_or_missing_price_is_never_served():
    api, clock = PriceApi(), Clock()
    hub = PriceHub(api, stream=None, monotonic=clock, max_age=30)  # type: ignore[arg-type]
    await hub.price(SOL)
    clock.now += 31
    with pytest.raises(StalePriceError, match="31s"):
        await hub.price(SOL)
    with pytest.raises(StalePriceError, match="sem preço"):
        await hub.price(BONK)  # a API não conhece


async def test_requests_during_the_first_poll_wait_for_it():
    # soak F10: logo após um reinício, vários `connect`s pedem SOL juntos; o
    # segundo pedido achava SOL acompanhado e ainda sem preço
    api, release = PriceApi(), asyncio.Event()
    answer = api.get_usd_prices

    async def slow(mints):
        await release.wait()
        return await answer(mints)

    api.get_usd_prices = slow  # type: ignore[method-assign]
    hub = PriceHub(api, stream=None)  # type: ignore[arg-type]
    first = asyncio.create_task(hub.price(SOL))
    await _settle()
    second = asyncio.create_task(hub.price(SOL))
    third = asyncio.create_task(hub.usd_prices([SOL]))
    await _settle()
    assert not second.done()  # espera o poll em andamento
    release.set()

    assert (await first)[0] == (await second)[0] == Decimal(150)
    assert await third == {SOL: Decimal(150)}
    assert api.calls == [[SOL]]  # um poll só
    assert not hub._first_polls


async def test_a_waiter_giving_up_does_not_cancel_the_first_poll():
    api, release = PriceApi(), asyncio.Event()
    answer = api.get_usd_prices

    async def slow(mints):
        await release.wait()
        return await answer(mints)

    api.get_usd_prices = slow  # type: ignore[method-assign]
    hub = PriceHub(api, stream=None)  # type: ignore[arg-type]
    first = asyncio.create_task(hub.price(SOL))
    await _settle()
    second = asyncio.create_task(hub.price(SOL))
    await _settle()
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    release.set()
    assert (await first)[0] == Decimal(150)


async def test_the_api_failing_only_warns(caplog):
    api = PriceApi()

    async def down(mints):
        raise OSError("429")

    api.get_usd_prices = down  # type: ignore[method-assign]
    hub = PriceHub(api, stream=None)  # type: ignore[arg-type]
    with pytest.raises(StalePriceError):
        await hub.price(SOL)
    assert "Price API sem resposta" in caplog.text


async def test_hub_market_data_takes_prices_from_the_hub_and_candles_elsewhere():
    class Candles:
        closed = False

        async def get_candles(self, mint, interval, qty):
            return ["candle"]

        async def aclose(self):
            Candles.closed = True

    async def price_of(mint):
        return Decimal(7)

    feed = HubMarketData(price_of, Candles())  # type: ignore[arg-type]
    assert await feed.get_price(SOL) == Decimal(7)
    assert await feed.get_candles(SOL, None, 1) == ["candle"]  # type: ignore[arg-type]
    await feed.aclose()
    assert Candles.closed


async def test_the_hub_feed_paces_the_bot_at_one_price_per_interval(monkeypatch):
    clock = Clock()
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr("trader.execution.market.hub.asyncio.sleep", fake_sleep)

    async def price_of(mint):
        return Decimal(1)

    feed = HubMarketData(price_of, None, interval=1.0, monotonic=clock)  # type: ignore[arg-type]
    await feed.get_price(SOL)  # o primeiro sai na hora
    clock.now += 0.25
    await feed.get_price(SOL)
    clock.now += 3
    await feed.get_price(SOL)  # o bot demorou: sem espera
    assert slept == [0.75]


class TestHubAsOracle:
    """B10 C6: o hub é o `PriceOracle` do processo."""

    async def test_new_mints_come_in_one_call_and_fresh_ones_make_none(self):
        api, clock = PriceApi(), Clock()
        hub = PriceHub(api, stream=None, monotonic=clock)  # type: ignore[arg-type]

        prices = await hub.usd_prices([SOL, JUP])
        clock.now += 20  # o loop do hub manteria fresco; aqui só envelhece
        again = await hub.usd_prices([SOL])

        assert prices == {SOL: Decimal(150), JUP: Decimal("0.5")}
        assert again == {SOL: Decimal(150)}
        assert api.calls == [sorted([SOL, JUP])]

    async def test_an_old_price_gets_one_more_try_and_a_missing_one_is_left_out(
        self,
    ):
        api, clock = PriceApi(), Clock()
        hub = PriceHub(api, stream=None, monotonic=clock, max_age=30)  # type: ignore[arg-type]
        await hub.usd_prices([SOL, BONK])
        clock.now += 31
        api.prices[SOL] = Decimal(160)

        assert await hub.usd_prices([SOL, BONK]) == {SOL: Decimal(160)}
        # BONK (sem preço) e SOL (velho) foram de novo à API
        assert sorted(api.calls[-1]) == sorted([SOL, BONK])

    async def test_a_snapshot_through_the_hub_prices_stables_at_one(self):
        usdc = SOLANA_MINTS.get_by_symbol("USDC").mint
        api = PriceApi()
        hub = PriceHub(api, stream=None)  # type: ignore[arg-type]

        snapshot = await usd_snapshot(hub, [usdc, SOL])

        assert snapshot == {usdc: Decimal(1), SOL: Decimal(150)}
        assert api.calls == [[SOL]]


def test_stale_price_kind_maps_to_price_unavailable():
    # o runner manda `type(ex).__name__`: renomear a exceção quebraria o mapa
    assert REMOTE_ERRORS[StalePriceError.__name__] is PriceUnavailableError
