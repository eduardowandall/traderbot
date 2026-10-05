import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from trader.shared.market.jupiter.async_jupiter_client import AsyncJupiterClient

MINT = "So11111111111111111111111111111111111111112"


def _price_msg(price, asset=MINT):
    return json.dumps({"type": "prices", "data": [{"assetId": asset, "price": price}]})


def _ws(*messages):
    ws = AsyncMock()
    ws.recv = AsyncMock(side_effect=list(messages))
    return ws


async def test_reads_price_as_exact_decimal():
    client = AsyncJupiterClient(client=AsyncMock(), websocket=_ws(_price_msg(0.1)))
    assert await client.get_price(MINT) == Decimal("0.1")


async def test_skips_non_price_messages_and_other_assets():
    ws = _ws(
        json.dumps({"type": "subscribed"}),
        _price_msg(5, asset="other-mint"),
        _price_msg(142.5),
    )
    client = AsyncJupiterClient(client=AsyncMock(), websocket=ws)
    assert await client.get_price(MINT) == Decimal("142.5")


@pytest.mark.parametrize(
    "closed",
    [
        ConnectionClosedOK(Close(1000, "bye"), None),
        ConnectionClosedError(Close(1011, "err"), None),
    ],
)
async def test_reconnects_after_any_close(closed, mock_sleep):
    old_ws = _ws(closed)
    new_ws = _ws(_price_msg(2))
    client = AsyncJupiterClient(client=AsyncMock(), websocket=old_ws)
    client._connect_price_ws = AsyncMock(return_value=new_ws)

    assert await client.get_price(MINT) == Decimal("2")
    client._connect_price_ws.assert_awaited_once_with(MINT)


async def test_gives_up_after_max_reconnects(mock_sleep):
    closed = ConnectionClosedOK(Close(1000, "bye"), None)
    client = AsyncJupiterClient(client=AsyncMock(), websocket=None)
    client._connect_price_ws = AsyncMock(side_effect=lambda mint: _ws(closed))

    with pytest.raises(ConnectionClosedOK):
        await client.get_price(MINT, max_reconnects=2)
    assert client._connect_price_ws.await_count == 3
    assert client.websocket is None
