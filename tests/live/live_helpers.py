"""Ajudantes da suíte live (fora do conftest: há dois módulos `conftest`)."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from typer.testing import CliRunner

import main as main_module
from trader.execution.market import JupiterCandles
from trader.execution.market.hub import PriceHub
from trader.shared.market import HubMarketData

# ainda mais folgado que o padrão do paper: o bot aleatório opera a cada tick
LOOSE_PAPER_POLICY = """
[paper.limits]
max_trade_usd = 1000
max_daily_notional_usd = 100000
max_trades_per_hour = 100000
max_daily_loss_usd = 100000
"""


@asynccontextmanager
async def running_hub() -> AsyncIterator[PriceHub]:
    """Um `PriceHub` rodando (websocket + Price API), como o do `serve`."""
    hub = PriceHub()
    task = asyncio.create_task(hub.run())
    try:
        yield hub
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def hub_feed(hub: PriceHub) -> HubMarketData:
    """O feed de um bot sobre o hub (o de `connect`, sem o socket)."""

    async def price_of(mint: str):
        price, _ = await hub.price(mint)
        return price

    return HubMarketData(price_of, JupiterCandles())


def invoke(*argv: str):
    """Roda o CLI no processo; devolve o resultado do CliRunner."""
    return CliRunner().invoke(main_module.app, list(argv))


def invoke_json(*argv: str) -> dict:
    """Um comando com `--json` (o `backtest`), já decodificado."""
    result = invoke(*argv, "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["ok"], data
    return data
