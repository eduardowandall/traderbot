from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import memory_gateway, mock_provider

from trader.async_account import AsyncAccount
from trader.models import (
    SOLANA_MINTS,
    Order,
    OrderSide,
    Position,
    PositionType,
    SwapResult,
)
from trader.models.account_data import MintBalance


def _make_account(balances=None):
    mi, mo = SOLANA_MINTS.get_by_symbol("SOL"), SOLANA_MINTS.get_by_symbol("USDC")
    provider = mock_provider()
    if balances is None:
        balances = [
            MintBalance(mint=mi.pubkey, available=Decimal("1.0")),
            MintBalance(mint=mo.pubkey, available=Decimal("1.0")),
        ]
    provider.get_account_balance = AsyncMock(return_value=balances)
    return AsyncAccount(provider, mi.pubkey, mo.pubkey, memory_gateway()), mi, mo


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


async def test_can_buy_raises_when_long_position_exists():
    acc, mi, mo = _make_account()
    acc.book.position = _long_position(mi, mo)

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
    acc.book.position = _long_position(mi, mo)

    with pytest.raises(ValueError, match="Sem valor minimo"):
        await acc.can_sell()


async def test_can_sell_allows_with_long_position_and_sufficient_balance():
    acc, mi, mo = _make_account()
    acc.book.position = _long_position(mi, mo)

    await acc.can_sell()


USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")


def _usdc_sol_account(usdc="1000", sol="1", fill_ratio=Decimal("1")):
    """Conta que compra SOL com USDC; o provider "executa" pelo preço pedido.

    fill_ratio < 1 simula slippage: recebe-se menos do que o pedido.
    """
    provider = mock_provider()
    provider.get_account_balance = AsyncMock(
        return_value=[
            MintBalance(mint=USDC.pubkey, available=Decimal(usdc)),
            MintBalance(mint=SOL.pubkey, available=Decimal(sol)),
        ]
    )
    prices = {}

    async def buy(input_mint, output_mint, type_order, quantity, price):
        prices["last"] = price
        return SwapResult(
            "buy-sig",
            str(input_mint),
            str(output_mint),
            USDC.ui_to_raw(quantity * price),
            SOL.ui_to_raw(quantity * fill_ratio),
        )

    async def sell(input_mint, output_mint, type_order, quantity):
        return SwapResult(
            "sell-sig",
            str(output_mint),
            str(input_mint),
            SOL.ui_to_raw(quantity),
            USDC.ui_to_raw(quantity * prices.get("sell", Decimal("110"))),
        )

    provider.buy = AsyncMock(side_effect=buy)
    provider.sell = AsyncMock(side_effect=sell)
    return AsyncAccount(provider, USDC.pubkey, SOL.pubkey, memory_gateway())


async def test_buy_records_quote_fill():
    acc = _usdc_sol_account(fill_ratio=Decimal("0.99"))

    order = await acc.buy(Decimal("100"), Decimal("0.5"))

    assert order.order_id == "buy-sig"
    assert order.side == OrderSide.BUY
    assert order.input_mint == USDC.mint
    assert order.output_mint == SOL.mint
    # pediu 0.5 SOL por 50 USDC, recebeu 0.495 SOL
    assert order.quantity == Decimal("0.495")
    assert order.price == Decimal("50") / Decimal("0.495")
    assert order.requested_quantity == Decimal("0.5")
    assert order.requested_price == Decimal("100")

    assert acc.book.position
    assert acc.book.position.entry_order == order
    assert acc.book.position.exit_order is None


async def test_buy_is_capped_at_spendable_balance():
    acc = _usdc_sol_account(usdc="30")

    order = await acc.buy(Decimal("100"), Decimal("0.5"))

    acc.provider.buy.assert_awaited_once()  # type: ignore[attr-defined]
    assert acc.provider.buy.await_args.kwargs["quantity"] == Decimal("0.3")  # type: ignore[attr-defined]
    assert order.quantity == Decimal("0.3")


async def test_sell_records_fill_and_realized_pnl():
    acc = _usdc_sol_account(sol="1")
    await acc.buy(Decimal("100"), Decimal("0.5"))
    entry = acc.book.position

    order = await acc.sell(Decimal("110"), Decimal("0.5"))

    assert order.order_id == "sell-sig"
    assert order.side == OrderSide.SELL
    assert order.quantity == Decimal("0.5")
    assert order.price == Decimal("110")
    assert acc.book.position is None
    assert entry and entry.exit_order == order
    assert acc.book.realized_usd == Decimal("5")


async def test_sell_is_capped_at_wallet_balance():
    # a compra recebeu menos que o pedido (slippage): vende só o que existe
    acc = _usdc_sol_account(sol="0.098")
    acc.book.position = _long_position(USDC, SOL)

    order = await acc.sell(Decimal("100.0"), Decimal("0.1"))

    assert order.quantity == Decimal("0.078")  # 0.098 - reserva de 0.02 SOL
    acc.provider.sell.assert_awaited_once_with(  # type: ignore[attr-defined]
        USDC.pubkey, SOL.pubkey, type_order="market", quantity=Decimal("0.078")
    )


async def test_sell_refuses_to_touch_sol_fee_reserve():
    acc = _usdc_sol_account(sol="0.015")
    acc.book.position = _long_position(USDC, SOL)

    with pytest.raises(ValueError, match="reservado para taxas"):
        await acc.sell(Decimal("100.0"), Decimal("0.1"))
    acc.provider.sell.assert_not_awaited()  # type: ignore[attr-defined]


async def test_buy_with_sol_keeps_fee_reserve():
    # par USDC-SOL: compra USDC gastando SOL
    provider = mock_provider()
    provider.get_account_balance = AsyncMock(
        return_value=[MintBalance(mint=SOL.pubkey, available=Decimal("1"))]
    )
    provider.buy = AsyncMock(
        return_value=SwapResult(
            "sig", SOL.mint, USDC.mint, SOL.ui_to_raw("0.98"), USDC.ui_to_raw("98")
        )
    )
    acc = AsyncAccount(provider, SOL.pubkey, USDC.pubkey, memory_gateway())

    # pede 100 USDC a 0.01 SOL/USDC = 1 SOL, mas só 0.98 pode ser gasto
    await acc.buy(Decimal("0.01"), Decimal("100"))

    assert provider.buy.await_args.kwargs["quantity"] == Decimal("98")


async def test_pnl_uses_usd_price_when_input_is_not_a_stablecoin():
    # caso real do log dry-run-random-USDC-SOL: comprou USDC gastando SOL.
    # o fill é 0.0086 SOL/USDC, mas o feed cota USDC a ~1 USD; misturar os dois
    # mostrava PNL de 11509%
    provider = mock_provider()
    provider.get_account_balance = AsyncMock(
        return_value=[MintBalance(mint=SOL.pubkey, available=Decimal("0.059077338"))]
    )
    provider.buy = AsyncMock(
        return_value=SwapResult("sig", SOL.mint, USDC.mint, 39077338, 4537732)
    )
    acc = AsyncAccount(provider, SOL.pubkey, USDC.pubkey, memory_gateway())
    market_price = Decimal("0.9997936921282948")

    order = await acc.buy(market_price, Decimal("0.059085"))

    assert order.quantity == Decimal("4.537732")
    assert order.price == market_price
    assert order.fill_price == Decimal("0.039077338") / Decimal("4.537732")
    assert acc.book.position
    pnl = acc.book.position.unrealized_pnl_percent(Decimal("0.999787081"))
    assert abs(pnl) < Decimal("0.01")


async def test_stablecoin_input_uses_real_fill_price():
    acc = _usdc_sol_account(fill_ratio=Decimal("0.99"))

    order = await acc.buy(Decimal("100"), Decimal("0.5"))

    # 50 USDC por 0.495 SOL: o slippage entra no preço de entrada
    assert order.price == order.fill_price == Decimal("50") / Decimal("0.495")
