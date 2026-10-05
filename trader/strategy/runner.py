"""O strategy-runner (`main.py connect spec.json`): uma spec, sem chave.

Camada strategy-side: monta o bot de sempre (`AsyncWebsocketTradingBot`) com
um `RemoteTradeClient` para o trade-runner (`main.py serve <modo>`). Não
importa execução, venue nem risco (`tests/test_architecture.py`), então não
alcança a chave, a carteira nem o ledger; nem sabe o modo. Nem fala com a
Jupiter: preços e candles vêm do trade-runner (ops `price` e `candles`).
"""

import json
from pathlib import Path

from trader.shared.market import HubMarketData
from trader.shared.market.pair import market_for
from trader.shared.notification.notification_service import Notifier
from trader.shared.paths import connection_files
from trader.strategy.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.strategy.bot.config import BotConfig, OnTick
from trader.strategy.spec.strategy import SpecStrategy
from trader.strategy.trading_service.remote import RemoteCandles, RemoteTradeClient


def find_connection(path: Path | None = None) -> dict:
    """O arquivo de conexão do trade-runner: o dado, ou o único em `data_dir()`."""
    if path is None:
        found = connection_files()
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
    connection_file: Path | None,
    notifier: Notifier,
    on_tick: OnTick | None = None,
) -> AsyncWebsocketTradingBot:
    """O bot da spec, com ordens, preços e candles pelo trade-runner.

    Os preços vêm do hub do trade-runner (op `price`), um feed para todos os
    strategy-runners; os candles do aquecimento, do op `candles`. O `hello`
    leva só os termos da spec (`spec.terms()`), que o trade-runner confere.
    """
    connection = find_connection(connection_file)
    trader = RemoteTradeClient(
        connection["host"],
        int(connection["port"]),
        connection["token"],
        strategy.spec.terms(),
        # reiniciado, o trade-runner escreve porta e token novos: relê
        resolve=lambda: find_connection(connection_file),
    )
    market = market_for(
        strategy.spec.symbol, lambda: HubMarketData(trader.price, RemoteCandles(trader))
    )
    config = BotConfig(
        name=f"connect-{strategy.spec.name}",
        symbol=strategy.spec.symbol,
        strategy=strategy,
        market=market,
        trader=trader,
        notifier=notifier,
        on_tick=on_tick,
    )
    return AsyncWebsocketTradingBot(config)
