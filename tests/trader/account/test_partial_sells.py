"""R5: vendas ficam dentro da posição; parciais mantêm o resto aberto."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from factories import memory_gateway, mock_provider

from trader.async_account import AsyncAccount
from trader.models import SOLANA_MINTS, Order, OrderSide, SwapResult
from trader.models.account_data import MintBalance
from trader.models.book import remainder_entry
from trader.models.costs import TradeCosts

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")


def _account(gateway, sol_balance="10"):
    """Compra SOL com USDC a 100; a carteira tem `sol_balance` SOL."""
    provider = mock_provider()  # reserva de 0.02 SOL para taxas
    wallet = {"usdc": Decimal("1000"), "sol": Decimal(sol_balance)}
    provider.get_account_balance = AsyncMock(
        side_effect=lambda: [
            MintBalance(mint=USDC.pubkey, available=wallet["usdc"]),
            MintBalance(mint=SOL.pubkey, available=wallet["sol"]),
        ]
    )

    async def buy(input_mint, output_mint, type_order, quantity, price):
        return SwapResult(
            "buy-sig",
            USDC.mint,
            SOL.mint,
            USDC.ui_to_raw(quantity * price),
            SOL.ui_to_raw(quantity),
        )

    async def sell(input_mint, output_mint, type_order, quantity):
        return SwapResult(
            f"sell-{quantity}",
            SOL.mint,
            USDC.mint,
            SOL.ui_to_raw(quantity),
            USDC.ui_to_raw(quantity * 110),
        )

    provider.buy = AsyncMock(side_effect=buy)
    provider.sell = AsyncMock(side_effect=sell)
    provider.fetch_swap_costs = AsyncMock(return_value=None)
    account = AsyncAccount(provider, USDC.pubkey, SOL.pubkey, gateway, account_id="t")
    return account, wallet


async def test_a_sell_never_exceeds_the_position():
    account, _ = _account(memory_gateway())
    await account.buy(Decimal("100"), Decimal("0.1"))

    await account.sell(Decimal("110"), Decimal("10"))  # pede o saldo inteiro

    assert account.provider.sell.await_args.kwargs["quantity"] == Decimal("0.1")  # type: ignore[attr-defined]
    assert account.book.position is None


async def test_a_partial_sell_keeps_the_rest_and_survives_a_restart():
    gateway = memory_gateway()
    account, _ = _account(gateway)
    await account.buy(Decimal("100"), Decimal("0.1"))

    order = await account.sell(Decimal("110"), Decimal("0.04"))

    assert not order.closes_position
    position = account.book.position
    assert position and position.entry_order.quantity == Decimal("0.06")
    assert position.entry_order.quote_amount == Decimal("6")  # custo proporcional
    assert account.book.realized_usd == Decimal("0.4")  # só a fração vendida

    restarted, _ = _account(gateway)
    restarted.restore_from_ledger()
    restored = restarted.book.position
    assert restored and restored.entry_order.quantity == Decimal("0.06")

    last = await restarted.sell(Decimal("110"), Decimal("0.06"))  # chave nova
    assert last.closes_position
    assert restarted.book.position is None
    fresh, _ = _account(gateway)
    fresh.restore_from_ledger()
    assert fresh.book.position is None


async def test_an_unsellable_rest_closes_and_is_recorded():
    gateway = memory_gateway()
    account, wallet = _account(gateway, sol_balance="0")
    await account.buy(Decimal("100"), Decimal("0.1"))
    wallet["sol"] = Decimal("0.1")  # a reserva de 0.02 SOL não pode ser vendida
    account.balances = None

    order = await account.sell(Decimal("110"), Decimal("0.1"))

    assert order.quantity == Decimal("0.08")
    assert order.closes_position
    assert account.book.position is None
    events = gateway.ledger.conn.execute(
        "SELECT payload FROM events WHERE type = 'position_leftover'"
    ).fetchall()
    assert len(events) == 1 and '"quantity": "0.02"' in events[0]["payload"]


def test_the_rest_scales_costs_and_ignores_dust():
    entry = Order(
        "e",
        USDC.mint,
        SOL.mint,
        Decimal("1"),
        Decimal("100"),
        OrderSide.BUY,
        datetime(2026, 9, 1),
        quote_amount=Decimal("100"),
        costs=TradeCosts("onchain", fee_lamports=10_000, rent_lamports=2_000_000),
    )

    rest = remainder_entry(entry, Decimal("0.25"))

    assert rest and rest.quantity == Decimal("0.75")
    assert rest.quote_amount == Decimal("75")
    assert rest.costs and rest.costs.fee_lamports == 7_500
    assert remainder_entry(entry, Decimal("0.995")) is None  # poeira
