"""Configuração de um bot (camada strategy-side).

O bot recebe tudo pronto: a estratégia, uma fonte de preços (`MarketData`) e
o `TradeClient` do bucket dele. Não recebe modo, chave, provider nem ledger:
isso fica do lado da execução (`trader/execution/wiring.py` monta).
"""

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from trader.shared.market import MarketData
from trader.shared.models import Interval, OrderSignal, Position, TickerData
from trader.shared.notification.notification_service import Notifier
from trader.strategy.trading_service.client import TradeClient


class Strategy(Protocol):
    """O que o bot e o backtester pedem de uma estratégia (`SpecStrategy`)."""

    # preço e saldo no token de cotação do par (USD em USDC/USDT);
    # `quote_usd` converte valores em USD (ex: `fixed_usd`)
    def on_market_refresh(
        self,
        price: Decimal,
        balance: Decimal,
        current_position: Position | None,
        quote_usd: Decimal | None,
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


@dataclass
class BotConfig:
    name: str  # nome amigável (vai no nome do arquivo de log)
    symbol: str  # OUTPUT-INPUT, ex: SOL-USDC (compra SOL gastando USDC)
    strategy: Strategy
    market: MarketData
    trader: TradeClient
    notifier: Notifier
    # chamado a cada preço recebido (ex: `TickRecorder.record`): horário,
    # preço em cotação e, num par sem stablecoin, o USD da cotação
    on_tick: Callable[[datetime, Decimal, Decimal | None], None] | None = None
    # tarefas que rodam junto com o loop e param com ele (ex: o relatório
    # diário do `run`); o bot não sabe o que fazem
    background: Sequence[Callable[[], Coroutine[Any, Any, None]]] = ()
