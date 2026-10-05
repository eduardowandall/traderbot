"""O strategy-runner (`main.py connect spec.json`): uma spec, sem chave.

Camada strategy-side: monta o bot de sempre (`AsyncWebsocketTradingBot`) com
um `RemoteTradeClient` para o trade-runner (`main.py serve <modo>`). Não
importa execução, venue nem risco (`tests/test_architecture.py`), então não
alcança a chave, a carteira nem o ledger; nem sabe o modo.
"""

import json
from collections.abc import Callable
from pathlib import Path

from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig, Notifier
from trader.market import MarketData
from trader.market.hub import HubMarketData
from trader.market.pair import market_for
from trader.paths import data_dir
from trader.strategy_spec.strategy import SpecStrategy
from trader.trading_service.remote import RemoteTradeClient


def find_connection(path: Path | None = None) -> dict:
    """O arquivo de conexão do trade-runner: o dado, ou o único em `data_dir()`."""
    if path is None:
        found = sorted(data_dir().glob("trader-*.json"))
        if len(found) != 1:
            names = ", ".join(p.name for p in found) or "nenhum"
            raise ValueError(
                f"trade-runners encontrados: {names}; rode `main.py serve <modo>` "
                "ou indique um com --trader"
            )
        path = found[0]
    return json.loads(Path(path).read_text(encoding="utf-8"))


def strategy_bot(
    strategy: SpecStrategy,
    spec_json: dict,
    connection_file: Path | None,
    candles: Callable[[], MarketData],
    notifier: Notifier,
) -> AsyncWebsocketTradingBot:
    """O bot da spec, com ordens e preços pelo trade-runner.

    Os preços vêm do hub do trade-runner (op `price`), um feed para todos os
    strategy-runners; `candles` só serve o aquecimento.
    """
    connection = find_connection(connection_file)
    trader = RemoteTradeClient(
        connection["host"],
        int(connection["port"]),
        connection["token"],
        spec_json,
        # reiniciado, o trade-runner escreve porta e token novos: relê
        resolve=lambda: find_connection(connection_file),
    )
    market = market_for(
        strategy.spec.symbol, lambda: HubMarketData(trader.price, candles())
    )
    config = BotConfig(
        name=f"connect-{strategy.spec.name}",
        symbol=strategy.spec.symbol,
        strategy=strategy,
        market=market,
        trader=trader,
        notifier=notifier,
    )
    return AsyncWebsocketTradingBot(config)
