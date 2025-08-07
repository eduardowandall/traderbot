from abc import ABC, abstractmethod
from decimal import Decimal

from trader.models.public_data import TickerData

from .models import OrderSide, OrderSignal, Position


class TradingStrategy(ABC):
    """Classe base para estratégias de trading"""

    @abstractmethod
    def on_market_refresh(
        self,
        ticker: TickerData,
        balance: Decimal,
        current_position: Position | None,
        position_history: list[Position],
    ) -> OrderSignal | None:
        pass


class SimpleMovingAverageStrategy(TradingStrategy):
    """Estratégia baseada em média móvel simples"""

    def __init__(self, short_period: int = 10, long_period: int = 30):
        self.short_period = short_period
        self.long_period = long_period
        self.price_history: list[Decimal] = []

    def _calculate_sma(self, period: int) -> Decimal:
        """Calcula média móvel simples"""
        if len(self.price_history) < period:
            return Decimal("0")
        return sum(self.price_history[-period:]) / Decimal(str(period))

    def should_buy(self, market_price: Decimal) -> bool:
        """Compra quando SMA curta cruza acima da SMA longa"""
        if len(self.price_history) < self.long_period:
            return False

        short_sma = self._calculate_sma(self.short_period)
        long_sma = self._calculate_sma(self.long_period)

        return short_sma > long_sma

    def should_sell(self, market_price: Decimal, position: Position) -> bool:
        """Vende quando SMA curta cruza abaixo da SMA longa"""
        if len(self.price_history) < self.long_period:
            return False

        short_sma = self._calculate_sma(self.short_period)
        long_sma = self._calculate_sma(self.long_period)

        return short_sma < long_sma

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        """Calcula quantidade baseada em 10% do saldo"""
        quantity = (balance * Decimal("0.1")) / price
        return quantity

    def on_market_refresh(
        self,
        ticker: TickerData,
        balance: Decimal,
        current_position: Position | None,
        position_history: list[Position],
    ) -> OrderSignal | None:
        self.price_history.append(ticker.last)
        if len(self.price_history) > self.long_period:
            self.price_history.pop(0)

        if not current_position:
            if self.should_buy(ticker.last):
                return OrderSignal(
                    OrderSide.BUY, self.calculate_quantity(balance, ticker.last)
                )
        else:
            if self.should_sell(ticker.last, current_position):
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )
        return None


class IterationStrategy(TradingStrategy):
    """
    Estratégia baseada em número de iterações.

    Compra na primeira oportunidade e vende após um número específico de iterações.
    Utiliza 80% do saldo disponível para cada operação de compra.

    Args:
        sell_on_iteration (int): Número de iterações para vender
    """

    def __init__(
        self,
        buy_on_iteration=2,
        sell_on_iteration=5,
    ):
        self.buy_on_iteration = int(buy_on_iteration)
        self.sell_on_iteration = int(sell_on_iteration)
        self.price_history: list[Decimal] = []

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        quantity = (balance * Decimal("0.8")) / price
        return quantity

    def on_market_refresh(
        self,
        ticker: TickerData,
        balance: Decimal,
        current_position: Position | None,
        position_history: list[Position],
    ) -> OrderSignal | None:
        self.price_history.append(ticker.last)
        if not current_position:
            if len(self.price_history) > self.buy_on_iteration:
                self.price_history = []
                return OrderSignal(
                    OrderSide.BUY,
                    self.calculate_quantity(balance, ticker.last),
                )
        else:
            if len(self.price_history) > self.sell_on_iteration:
                self.price_history = []
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )
        return None
