from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from trader.async_account import AsyncAccount
from trader.models import SOLANA_MINTS, Order, OrderSide, Position, PositionType
from trader.models.account_data import MintBalance
from trader.providers import AsyncJupiterProvider


def _make_account(balances=None):
    mi, mo = SOLANA_MINTS.get_by_symbol("SOL"), SOLANA_MINTS.get_by_symbol("USDC")
    provider = AsyncMock(spec=AsyncJupiterProvider)
    if balances is None:
        balances = [
            MintBalance(mint=mi.pubkey, available=Decimal("1.0")),
            MintBalance(mint=mo.pubkey, available=Decimal("1.0")),
        ]
    provider.get_account_balance = AsyncMock(return_value=balances)
    return AsyncAccount(provider, mi.pubkey, mo.pubkey), mi, mo


def _long_position(mi, mo):
    return Position(
        PositionType.LONG,
        Order(
            "",
            mi.mint,
            mo.mint,
            Decimal("0.1"),
            Decimal("110.0"),
            OrderSide.BUY,
            datetime.now(),
        ),
        exit_order=None,
    )


async def test_buy():
    mi, mo = SOLANA_MINTS.get_by_symbol("SOL"), SOLANA_MINTS.get_by_symbol("USDC")
    acc = AsyncAccount(AsyncMock(spec=AsyncJupiterProvider), mi.pubkey, mo.pubkey)

    with patch.object(AsyncAccount, "can_buy", return_value=True):
        order = await acc.buy(Decimal("100.00"), Decimal("0.1"))

    assert order.input_mint == mi.mint
    assert order.output_mint == mo.mint
    assert order.quantity == Decimal("0.1")
    assert order.price == Decimal("100.0")
    assert order.side == OrderSide.BUY

    assert acc.current_position
    assert acc.current_position.entry_order == order
    assert acc.current_position.exit_order is None


async def test_sell():
    mi, mo = SOLANA_MINTS.get_by_symbol("SOL"), SOLANA_MINTS.get_by_symbol("USDC")
    acc = AsyncAccount(AsyncMock(spec=AsyncJupiterProvider), mi.pubkey, mo.pubkey)
    position = Position(
        PositionType.LONG,
        Order(
            "",
            mi.mint,
            mo.mint,
            Decimal("0.1"),
            Decimal("110.0"),
            OrderSide.BUY,
            datetime.now(),
        ),
        exit_order=None,
    )
    acc.current_position = position
    with patch.object(AsyncAccount, "can_sell", return_value=True):
        order = await acc.sell(Decimal("100.00"), Decimal("0.1"))

    assert order.input_mint == mi.mint
    assert order.output_mint == mo.mint
    assert order.quantity == Decimal("0.1")
    assert order.price == Decimal("100.0")
    assert order.side == OrderSide.SELL

    assert position.exit_order == order
    assert acc.current_position is None


async def test_can_buy_raises_when_long_position_exists():
    acc, mi, mo = _make_account()
    acc.current_position = _long_position(mi, mo)

    with pytest.raises(ValueError, match="Já existe posicão"):
        await acc.can_buy()


async def test_can_buy_raises_when_balance_below_minimum():
    mi, mo = SOLANA_MINTS.get_by_symbol("SOL"), SOLANA_MINTS.get_by_symbol("USDC")
    acc, _, _ = _make_account(
        [
            MintBalance(mint=mi.pubkey, available=Decimal("0.005")),
            MintBalance(mint=mo.pubkey, available=Decimal("1.0")),
        ]
    )

    with pytest.raises(ValueError, match="Sem valor minimo"):
        await acc.can_buy()


async def test_can_buy_allows_without_position_and_sufficient_balance():
    acc, _, _ = _make_account()

    await acc.can_buy()


async def test_can_sell_raises_when_no_position():
    acc, _, _ = _make_account()

    with pytest.raises(ValueError, match="Sem posicão de compra"):
        await acc.can_sell()


async def test_can_sell_raises_when_balance_below_minimum():
    mi, mo = SOLANA_MINTS.get_by_symbol("SOL"), SOLANA_MINTS.get_by_symbol("USDC")
    acc, _, _ = _make_account(
        [
            MintBalance(mint=mi.pubkey, available=Decimal("1.0")),
            MintBalance(mint=mo.pubkey, available=Decimal("0.000001")),
        ]
    )
    acc.current_position = _long_position(mi, mo)

    with pytest.raises(ValueError, match="Sem valor minimo"):
        await acc.can_sell()


async def test_can_sell_allows_with_long_position_and_sufficient_balance():
    acc, mi, mo = _make_account()
    acc.current_position = _long_position(mi, mo)

    await acc.can_sell()
