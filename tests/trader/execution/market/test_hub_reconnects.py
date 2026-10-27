"""A28: o websocket do hub cai sozinho; a reconexão de rotina é INFO, e só
uma sequência de conexões sem preço avisa (uma vez)."""

import asyncio
import logging
from decimal import Decimal

import pytest
from factories import PriceTable

from trader.execution.market import hub as hub_module
from trader.execution.market.hub import PriceHub
from trader.shared.models import SOLANA_MINTS

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint


class FlakyStream:
    """Cada conexão: `prices[i]` preços e depois cai (sem close frame)."""

    def __init__(self, prices: list[int]):
        self.prices = prices
        self.connections = 0

    def __call__(self, mints):
        return self._read()

    async def _read(self):
        n = self.connections
        self.connections += 1
        if n >= len(self.prices):
            await asyncio.Event().wait()  # fica de pé até o cancelamento
        for i in range(self.prices[n]):
            yield SOL, Decimal(100 + i)
        raise ConnectionError("no close frame received or sent")


async def _run(stream: FlakyStream) -> None:
    hub = PriceHub(PriceTable({SOL: Decimal(100)}), stream=stream)  # type: ignore[arg-type]
    await hub.price(SOL)  # o hub passa a acompanhar o SOL
    task = asyncio.ensure_future(hub._stream_forever())
    for _ in range(200):
        if stream.connections > len(stream.prices):
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert stream.connections == len(stream.prices) + 1


@pytest.fixture(autouse=True)
def no_reconnect_wait(monkeypatch):
    monkeypatch.setattr(hub_module, "RECONNECT_SECONDS", 0)


def _levels(caplog) -> list[tuple[int, str]]:
    return [
        (r.levelno, r.getMessage().split(" (")[0])
        for r in caplog.records
        if r.name == hub_module.__name__
    ]


async def test_a_drop_after_prices_reconnects_at_info(caplog):
    caplog.set_level(logging.DEBUG, logger=hub_module.__name__)
    await _run(FlakyStream([3, 2]))
    assert _levels(caplog) == [
        (logging.INFO, "Websocket de preços caiu"),
        (logging.INFO, "Websocket de preços caiu"),
    ]


async def test_connections_without_prices_warn_once_then_say_when_back(caplog):
    caplog.set_level(logging.DEBUG, logger=hub_module.__name__)
    # cai com preço, depois três tentativas sem nenhum (DNS), depois volta
    await _run(FlakyStream([2, 0, 0, 0, 1]))
    assert _levels(caplog) == [
        (logging.INFO, "Websocket de preços caiu"),
        (logging.WARNING, "Websocket de preços não conecta"),
        (logging.DEBUG, "Websocket de preços ainda não conecta"),
        (logging.DEBUG, "Websocket de preços ainda não conecta"),
        (logging.INFO, "Websocket de preços de volta"),
        (logging.INFO, "Websocket de preços caiu"),
    ]
