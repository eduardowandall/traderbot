"""A28: o websocket de preços (`trench-stream.jup.ag`) do jeito que o hub o usa.

O hub perdia a conexão a cada 15-25 min ("no close frame received or sent").
A investigação (2026-10-13, quatro conexões lado a lado por 45 min) viu três
conexões com pings, de 721, 961 e 1141 s, caírem no mesmo instante, depois
de 16 s sem mensagem, com um reset local (WinError 64, código 1006), e
voltarem em 3 s por outra borda do CloudFront: o caminho de rede, não um
limite de idade do servidor (reassinar antes não evitaria nada). A conexão
sem pings nunca percebeu: ficou meio-aberta e muda por 25+ min. Estes testes
fixam o que o hub assume: o servidor responde aos pings do cliente (são eles
que transformam um caminho morto em reconexão) e exige `permessage-deflate`
(sem compressão, 426).
"""

import asyncio
import json

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from trader.execution.market.hub import WS_URL
from trader.shared.models import SOLANA_MINTS

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
HEADERS = {"Origin": "https://jup.ag"}


def test_the_server_streams_sol_and_answers_our_pings():
    async def read():
        async with websockets.connect(
            WS_URL, additional_headers=HEADERS, compression="deflate"
        ) as ws:
            await ws.send(json.dumps({"type": "subscribe:prices", "assets": [SOL]}))
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            pong = await ws.ping()
            latency = await asyncio.wait_for(pong, timeout=10)
            return message, latency

    message, latency = asyncio.run(read())

    assert message["type"] == "prices"
    assert any(item["assetId"] == SOL for item in message["data"])
    assert 0 < latency < 10


def test_the_server_refuses_a_connection_without_compression():
    async def connect():
        async with websockets.connect(
            WS_URL, additional_headers=HEADERS, compression=None
        ):
            pass

    with pytest.raises(InvalidStatus) as refused:
        asyncio.run(connect())
    assert refused.value.response.status_code == 426
