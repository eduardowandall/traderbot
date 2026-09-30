"""Configuração de um bot (camada strategy-side).

O bot recebe tudo pronto: a estratégia, uma fonte de preços (`MarketData`) e
o `TradeClient` do bucket dele. Não recebe modo, chave, provider nem ledger:
isso fica do lado da execução (`trader/wiring.py` monta).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trader.market import MarketData
from trader.models import Interval, OrderSignal, Position, TickerData
from trader.trading_service.client import TradeClient


class Strategy(Protocol):
    """O que o bot e o backtester pedem de uma estratégia (`SpecStrategy`)."""

    def on_market_refresh(
        self, price: Decimal, balance: Decimal, current_position: Position | None
    ) -> OrderSignal | None: ...

    # timeframe e quantidade de candles para aquecer os indicadores
    def warmup(self) -> tuple[Interval, int]: ...

    def setup(self, ticker_history: Sequence[TickerData]) -> None: ...

    # estado que sobrevive a um reinício (cooldown, validade, última saída)
    def resume(
        self,
        last_exit_at: datetime | None,
        opened_at: datetime | None,
        last_exit_price: Decimal | None = None,
    ) -> None: ...

    # relógio e sorteios injetáveis: o backtest usa o tempo do tick e uma
    # semente fixa, para o resultado ser determinístico
    def set_clock(self, clock: Callable[[], datetime]) -> None: ...

    def seed(self, seed: int | str | None) -> None: ...


class Notifier(Protocol):
    # não bloqueia nem levanta: o envio corre fora do loop do bot
    def send_message(self, message: str) -> None: ...

    # espera (por tempo limitado) os envios pendentes
    async def aclose(self) -> None: ...


@dataclass
class BotConfig:
    name: str  # nome amigável (vai no nome do arquivo de log)
    symbol: str  # OUTPUT-INPUT, ex: SOL-USDC (compra SOL gastando USDC)
    strategy: Strategy
    market: MarketData
    trader: TradeClient
    notifier: Notifier
    # chamado a cada preço recebido (ex: `TickRecorder.record`)
    on_tick: Callable[[datetime, Decimal], None] | None = None
