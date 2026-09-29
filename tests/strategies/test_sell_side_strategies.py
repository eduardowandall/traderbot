from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trader.models import OrderSide, OrderSignal, Position, PositionType
from trader.models.order import Order
from trader.trading_strategy import (
    StrategyComposer,
    TargetPercentStrategy,
    TradingStrategy,
    TrailingStopLossStrategy,
    WeightedMovingAverageStrategy,
)

ZERO = Decimal("0")


def _position(quantity="0.5", price="100"):
    return Position(
        PositionType.LONG,
        Order(
            "id",
            "in",
            "out",
            Decimal(quantity),
            Decimal(price),
            OrderSide.BUY,
            datetime.now(),
        ),
        exit_order=None,
    )


class AlwaysSide(TradingStrategy):
    def __init__(self, side):
        super().__init__()
        self.side = side

    def calculate_quantity(self, balance, price):
        return (balance * Decimal("0.8")) / price

    def on_market_refresh(self, price, spread, balance, current_position):
        return OrderSignal(self.side, Decimal("999"))


class TestComposerQuantity:
    def test_sell_closes_whole_position(self):
        composer = StrategyComposer(
            buy_strategies=[AlwaysSide(OrderSide.BUY)],
            sell_strategies=[AlwaysSide(OrderSide.SELL)],
        )
        position = _position(quantity="0.540541")
        # saldo do input_mint (USDC) não deve influenciar a venda
        signal = composer.on_market_refresh(
            Decimal("145"), None, Decimal("20"), position
        )
        assert signal == OrderSignal(OrderSide.SELL, Decimal("0.540541"))

    def test_buy_uses_first_buy_strategy_sizing(self):
        composer = StrategyComposer(
            buy_strategies=[AlwaysSide(OrderSide.BUY)],
            sell_strategies=[AlwaysSide(OrderSide.SELL)],
        )
        signal = composer.on_market_refresh(Decimal("100"), None, Decimal("50"), None)
        assert signal == OrderSignal(OrderSide.BUY, Decimal("0.4"))

    @pytest.mark.parametrize("kwargs", [{"sell_mode": "x"}, {"buy_mode": "some"}])
    def test_invalid_mode_raises_value_error(self, kwargs):
        with pytest.raises(ValueError):
            StrategyComposer(**kwargs)


class TestTrailingStopLoss:
    def test_signals_buy_when_flat(self):
        strategy = TrailingStopLossStrategy(stop_loss_percent="3")
        signal = strategy.on_market_refresh(Decimal("100"), None, Decimal("1"), None)
        assert signal and signal.side == OrderSide.BUY

    def test_peak_starts_at_entry_price(self):
        # a primeira observação já abaixo da entrada conta como queda desde a entrada
        strategy = TrailingStopLossStrategy(stop_loss_percent="3")
        signal = strategy.on_market_refresh(
            Decimal("96"), None, Decimal("0"), _position(price="100")
        )
        assert signal == OrderSignal(OrderSide.SELL, Decimal("0.5"))

    def test_trails_the_peak(self):
        strategy = TrailingStopLossStrategy(stop_loss_percent="3")
        position = _position(price="100")
        assert strategy.on_market_refresh(Decimal("110"), None, ZERO, position) is None
        assert (
            strategy.on_market_refresh(Decimal("107.5"), None, ZERO, position) is None
        )
        signal = strategy.on_market_refresh(Decimal("106.7"), None, ZERO, position)
        assert signal and signal.side == OrderSide.SELL

    def test_resets_peak_when_flat(self):
        strategy = TrailingStopLossStrategy(stop_loss_percent="3")
        strategy.on_market_refresh(Decimal("200"), None, ZERO, _position(price="100"))
        strategy.on_market_refresh(Decimal("50"), None, Decimal("1"), None)
        assert strategy.highest_price_after_target == Decimal("0")


class TestTargetPercent:
    def test_holds_below_target(self):
        strategy = TargetPercentStrategy(target_percent="5")
        assert (
            strategy.on_market_refresh(Decimal("104"), None, ZERO, _position()) is None
        )

    def test_sells_at_target(self):
        strategy = TargetPercentStrategy(target_percent="5")
        signal = strategy.on_market_refresh(Decimal("105"), None, ZERO, _position())
        assert signal == OrderSignal(OrderSide.SELL, Decimal("0.5"))


class TestWeightedMovingAverage:
    def test_wma_weights_recent_prices_more(self):
        strategy = WeightedMovingAverageStrategy()
        prices = [Decimal("1"), Decimal("2"), Decimal("3")]
        # (1*1 + 2*2 + 3*3) / 6
        assert strategy.weighted_moving_average(prices, 3) == Decimal(14) / Decimal(6)

    def _feed(self, strategy, prices):
        start = datetime(2026, 1, 1)
        for i, price in enumerate(prices):
            strategy.set_parameters(Decimal(price), start + timedelta(seconds=i * 60))

    def test_no_signal_during_warmup(self):
        strategy = WeightedMovingAverageStrategy(short_window=2, long_window=4)
        # 2 candles + o preço atual = 3 < long_window
        self._feed(strategy, ["10", "9"])
        assert (
            strategy.on_market_refresh(Decimal("7"), None, Decimal("100"), None) is None
        )

    def test_buys_when_short_below_long(self):
        strategy = WeightedMovingAverageStrategy(
            short_window=2, long_window=4, period=60
        )
        self._feed(strategy, ["10", "9", "8", "7"])
        signal = strategy.on_market_refresh(Decimal("7"), None, Decimal("100"), None)
        assert signal and signal.side == OrderSide.BUY
        assert signal.quantity == Decimal("80") / Decimal("7")

    def test_buys_when_short_above_long_if_configured(self):
        strategy = WeightedMovingAverageStrategy(
            short_window=2, long_window=4, buy_when_short_below=False, period=60
        )
        self._feed(strategy, ["10", "9", "8", "7"])
        assert (
            strategy.on_market_refresh(Decimal("7"), None, Decimal("100"), None) is None
        )

    def test_history_is_bounded(self):
        strategy = WeightedMovingAverageStrategy(short_window=2, long_window=4)
        self._feed(strategy, [str(p) for p in range(1, 20)])
        assert len(strategy.price_history) == 4
