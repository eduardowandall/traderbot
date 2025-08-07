"""
Testes function-based para estratégias de trading.
Validam comportamento de compra e venda com mínimo de mocking.
"""

from datetime import datetime
from decimal import Decimal

from trader.models import Position, PositionType, TickerData
from trader.models.order import Order, OrderSide
from trader.trading_strategy import (
    SimpleMovingAverageStrategy,
)

order_counter = 1


def create_position(
    quantity: Decimal = Decimal("0.1"),
    entry_price: Decimal = Decimal("100.0"),
) -> Position:
    """Helper para criar posições de teste"""
    global order_counter
    order_counter += 1
    return Position(
        type=PositionType.LONG,
        entry_order=Order(
            order_id=f"test-order-{order_counter}",
            symbol="BTC-BRL",
            quantity=quantity,
            price=entry_price,
            side=OrderSide.BUY,
            time=datetime.now(),
        ),
        exit_order=None,
    )


def create_ticker(price: Decimal) -> TickerData:
    """Helper para criar objetos TickerData de teste"""
    return TickerData(
        buy=price - Decimal("1"),
        date=int(datetime.now().timestamp()),
        high=price + Decimal("5"),
        last=price,
        low=price - Decimal("5"),
        open=price,
        pair="BTC-BRL",
        sell=price + Decimal("1"),
        vol=Decimal("100"),
    )


def test_simple_moving_average_strategy_buy_signal():
    """Testa se SMA strategy compra quando SMA curta cruza acima da longa"""
    strategy = SimpleMovingAverageStrategy(short_period=3, long_period=5)

    # Preços que criam tendência de baixa seguida de alta
    prices = [
        Decimal("100"),
        Decimal("95"),
        Decimal("90"),
        Decimal("85"),
        Decimal("80"),  # SMA longa baixa
        Decimal("85"),
        Decimal("90"),
        Decimal("95"),  # SMA curta sobe
    ]

    position = create_position()
    balance = Decimal("1000")

    # Alimenta histórico de preços
    for price in prices:
        ticker = create_ticker(price)
        strategy.on_market_refresh(ticker, balance, position, [])

    # Deve comprar quando SMA curta > SMA longa
    assert strategy.should_buy(Decimal("95")) is True

    # SMA curta (3): (85+90+95)/3 = 90
    # SMA longa (5): (80+85+90+95)/4 = 87.5 (só tem 4 valores dos últimos 5)
    # Como 90 > 87.5, deve comprar


def test_simple_moving_average_strategy_sell_signal():
    """Testa se SMA strategy vende quando SMA curta cruza abaixo da longa"""
    strategy = SimpleMovingAverageStrategy(short_period=3, long_period=5)

    # Preços que criam tendência de alta seguida de baixa
    prices = [
        Decimal("80"),
        Decimal("85"),
        Decimal("90"),
        Decimal("95"),
        Decimal("100"),  # SMA longa alta
        Decimal("95"),
        Decimal("90"),
        Decimal("85"),  # SMA curta desce
    ]

    position = create_position()
    balance = Decimal("1000")

    # Alimenta histórico de preços
    for price in prices:
        ticker = create_ticker(price)
        strategy.on_market_refresh(ticker, balance, position, [])

    # Deve vender quando SMA curta < SMA longa
    assert strategy.should_sell(Decimal("85"), position) is True


def test_simple_moving_average_strategy_insufficient_data():
    """Testa se SMA strategy não opera sem dados suficientes"""
    strategy = SimpleMovingAverageStrategy(short_period=10, long_period=30)
    position = create_position()
    balance = Decimal("1000")

    # Apenas alguns preços (insuficientes para long_period)
    for price in [Decimal("100"), Decimal("101"), Decimal("102")]:
        ticker = create_ticker(price)
        strategy.on_market_refresh(ticker, balance, position, [])

    # Não deve comprar nem vender sem dados suficientes
    assert strategy.should_buy(Decimal("103")) is False
    assert strategy.should_sell(Decimal("103"), position) is False


def test_calculate_quantity_methods():
    """Testa se os métodos de cálculo de quantidade funcionam corretamente"""
    balance = Decimal("1000")
    price = Decimal("100")

    # SMA strategy usa 10% do saldo
    sma_strategy = SimpleMovingAverageStrategy()
    sma_quantity = sma_strategy.calculate_quantity(balance, price)
    expected_sma = (balance * Decimal("0.1")) / price  # 1.0
    assert Decimal(sma_quantity) == expected_sma
