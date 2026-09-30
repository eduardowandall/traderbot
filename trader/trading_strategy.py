"""Base das estratégias: sinais a partir de preço, saldo e posição.

As estratégias são specs (`trader/strategy_spec/`); esta é só a interface que
o bot e o backtester usam.
"""

import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from trader.models.public_data import TickerData

from .models import OrderSignal, Position


class TradingStrategy(ABC):
    """Classe base para estratégias de trading"""

    # relógio e gerador aleatório injetáveis: no replay/backtest usam o tempo
    # dos ticks e uma semente fixa, para o resultado ser determinístico.
    # Definidos por instância em __init__ (subclasses devem chamá-lo).
    clock: Callable[[], datetime]
    rng: random.Random

    def __init__(self):
        self.logger = logging.getLogger(self.__module__)
        self.clock = datetime.now
        self.rng = random.Random()

    def set_clock(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    def seed(self, seed: int | str | None) -> None:
        self.rng = random.Random(seed)

    @abstractmethod
    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        pass

    def setup(self, ticker_history: Sequence[TickerData]):
        return
