from datetime import datetime
from decimal import Decimal

import pytest

from trader.models.order import Order, OrderSide
from trader.models.position import Position, PositionType
from trader.trading_strategy import TargetValueStrategy


class TestTargetValueBuy:
    def test_target_price_not_reached(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("1.0"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("100.0"),
        )
        strategy.last_price = Decimal("10.0001")
        order_signal = strategy.on_market_refresh(
            Decimal("9.9999"),
            Decimal("0"),
            Decimal("0"),
            None,
        )
        assert order_signal is None

    def test_target_price_reached(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("1.0"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("100.0"),
        )
        strategy.last_price = Decimal("9.9999")
        order_signal = strategy.on_market_refresh(
            Decimal("9.9999"),
            Decimal("0"),
            Decimal("0"),
            None,
        )
        assert order_signal
        assert order_signal.side == "buy"

    def test_target_price_reached_but_spread_too_high(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("1.0"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("0.1"),
        )
        strategy.last_price = Decimal("9.9999")
        order_signal = strategy.on_market_refresh(
            Decimal("9.9999"),
            Decimal("101"),
            Decimal("0"),
            None,
        )
        assert order_signal is None

    def test_target_price_reached_but_price_lower_than_last(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("1.0"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("1000.0"),
        )
        strategy.last_price = Decimal("9.9999")
        order_signal = strategy.on_market_refresh(
            Decimal("9.9998"),
            Decimal("0"),
            Decimal("0"),
            None,
        )
        assert order_signal is None


class TestTargetValueSell:
    @pytest.fixture(autouse=True)
    def mock_position(self):
        self.current_position = Position(
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
        )

    def test_target_profit_not_reached(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("1.0"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("100.0"),
        )
        strategy.last_price = Decimal("9.9999")
        order_signal = strategy.on_market_refresh(
            Decimal("9.9999"),
            Decimal("9.9999"),
            Decimal("100.00"),
            current_position=self.current_position,
        )
        assert order_signal is None

    def test_target_profit_reached_dont_sell(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("0.10"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("100.0"),
        )
        order_signal = strategy.on_market_refresh(
            Decimal("11.9999"),
            Decimal("11.9999"),
            Decimal("100.00"),
            current_position=self.current_position,
        )
        # quando o lucro alvo é atingido, não retorna ordem de venda, apenas marca a flag
        # só vai vender quando cair no stop loss
        assert order_signal is None
        assert strategy.target_profit_reached is True

    def test_target_profit_reached_dont_sell_updates_highest_price(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("0.10"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("100.0"),
        )
        order_signal = strategy.on_market_refresh(
            Decimal("11.9999"),
            Decimal("11.9999"),
            Decimal("100.00"),
            current_position=self.current_position,
        )
        assert order_signal is None
        assert strategy.target_profit_reached is True
        assert strategy.highest_price_after_target == Decimal("11.9999")

        order_signal = strategy.on_market_refresh(
            Decimal("12.9999"),
            Decimal("12.9999"),
            Decimal("100.00"),
            current_position=self.current_position,
        )
        assert order_signal is None
        assert strategy.target_profit_reached is True
        assert strategy.highest_price_after_target == Decimal("12.9999")

    def test_target_profit_reached_sell(self):
        strategy = TargetValueStrategy(
            target_buy_price=Decimal("10.0000"),
            target_profit_percent=Decimal("1.0"),
            stop_loss_percent=Decimal("1.0"),
            balance_percent=Decimal("10.0"),
            max_spread=Decimal("100.0"),
        )
        strategy.target_profit_reached = True
        strategy.highest_price_after_target = Decimal("12.5000")

        order_signal = strategy.on_market_refresh(
            Decimal("10.9999"),
            Decimal("10.9999"),
            Decimal("100.00"),
            current_position=self.current_position,
        )
        # valor atual é percentualmente menor do que o valor mais alto regitrado após atingir o lucro alvo
        assert order_signal
        assert order_signal.side == "sell"
