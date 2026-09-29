"""Configuração de um bot (camada strategy-side).

O bot recebe tudo pronto: a estratégia, uma fonte de preços (`MarketData`) e
o `TradeClient` do bucket dele. Não recebe modo, chave, provider nem ledger:
isso fica do lado da execução (`trader/wiring.py` monta).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trader.market import MarketData
from trader.trading_service.client import TradeClient
from trader.trading_strategy import TradingStrategy


class Notifier(Protocol):
    # não bloqueia nem levanta: o envio corre fora do loop do bot
    def send_message(self, message: str) -> None: ...

    # espera (por tempo limitado) os envios pendentes
    async def aclose(self) -> None: ...


@dataclass
class BotConfig:
    name: str  # nome amigável (vai no nome do arquivo de log)
    symbol: str  # OUTPUT-INPUT, ex: SOL-USDC (compra SOL gastando USDC)
    strategy: TradingStrategy
    market: MarketData
    trader: TradeClient
    notifier: Notifier
    # chamado a cada preço recebido (ex: `TickRecorder.record`)
    on_tick: Callable[[datetime, Decimal], None] | None = None
