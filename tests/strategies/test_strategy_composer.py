from datetime import datetime
from decimal import Decimal

from trader.models import OrderSide, OrderSignal, Position, PositionType
from trader.models.order import Order
from trader.trading_strategy import StrategyComposer, TradingStrategy


class FakeStrategy(TradingStrategy):
    def __init__(self, side: OrderSide):
        super().__init__()
        self.count = 0
        self.side = side

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        self.count += 1
        return OrderSignal(self.side, Decimal("10"))


def test_composer_call_refresh_for_all_when_buy():
    buy_strategy = FakeStrategy(side=OrderSide.BUY)
    sell_strategy = FakeStrategy(side=OrderSide.SELL)
    comp = StrategyComposer(
        buy_strategies=[buy_strategy], sell_strategies=[sell_strategy]
    )
    for _i in range(3):
        signal = comp.on_market_refresh(
            Decimal("10.0001"),
            Decimal("0.0001"),
            Decimal("100.00"),
            None,
        )
        assert signal
        assert signal.side == OrderSide.BUY
    assert buy_strategy.count == 3
    assert sell_strategy.count == 3


def test_composer_call_refresh_for_all_when_sell():
    buy_strategy = FakeStrategy(side=OrderSide.BUY)
    sell_strategy = FakeStrategy(side=OrderSide.SELL)
    comp = StrategyComposer(
        buy_strategies=[buy_strategy], sell_strategies=[sell_strategy]
    )
    for _i in range(3):
        signal = comp.on_market_refresh(
            Decimal("10.0001"),
            Decimal("0.0001"),
            Decimal("100.00"),
            Position(
                type=PositionType.LONG,
                entry_order=Order(
                    order_id="1",
                    input_mint="Sol1111",
                    output_mint="Efwr4t",
                    quantity=Decimal("10"),
                    price=Decimal("10.0001"),
                    side=OrderSide.BUY,
                    timestamp=datetime.now(),
                ),
                exit_order=None,
            ),
        )
        assert signal
        assert signal.side == OrderSide.SELL
    assert buy_strategy.count == 3
    assert sell_strategy.count == 3


def test_composer_check_signals_all_buy():
    buy_strategy = FakeStrategy(side=OrderSide.BUY)
    sell_strategy = FakeStrategy(side=OrderSide.SELL)
    comp = StrategyComposer(
        buy_strategies=[buy_strategy], sell_strategies=[sell_strategy]
    )
    result = comp._check_signals(
        [
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
        ],
        "all",
        OrderSide.BUY,
    )
    assert result is True

    result = comp._check_signals(
        [
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
        ],
        "all",
        OrderSide.BUY,
    )
    assert result is False


def test_composer_check_signals_any_buy():
    buy_strategy = FakeStrategy(side=OrderSide.BUY)
    sell_strategy = FakeStrategy(side=OrderSide.SELL)
    comp = StrategyComposer(
        buy_strategies=[buy_strategy], sell_strategies=[sell_strategy]
    )
    result = comp._check_signals(
        [
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
        ],
        "any",
        OrderSide.BUY,
    )
    assert result is True

    result = comp._check_signals(
        [
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
        ],
        "any",
        OrderSide.BUY,
    )
    assert result is False


def test_composer_check_signals_all_sell():
    buy_strategy = FakeStrategy(side=OrderSide.BUY)
    sell_strategy = FakeStrategy(side=OrderSide.SELL)
    comp = StrategyComposer(
        buy_strategies=[buy_strategy], sell_strategies=[sell_strategy]
    )
    result = comp._check_signals(
        [
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
        ],
        "all",
        OrderSide.SELL,
    )
    assert result is True

    result = comp._check_signals(
        [
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
        ],
        "all",
        OrderSide.SELL,
    )
    assert result is False


def test_composer_check_signals_any_sell():
    buy_strategy = FakeStrategy(side=OrderSide.BUY)
    sell_strategy = FakeStrategy(side=OrderSide.SELL)
    comp = StrategyComposer(
        buy_strategies=[buy_strategy], sell_strategies=[sell_strategy]
    )
    result = comp._check_signals(
        [
            OrderSignal(OrderSide.SELL, quantity=Decimal("10")),
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
        ],
        "any",
        OrderSide.SELL,
    )
    assert result is True

    result = comp._check_signals(
        [
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
            OrderSignal(OrderSide.BUY, quantity=Decimal("10")),
        ],
        "any",
        OrderSide.SELL,
    )
    assert result is False
