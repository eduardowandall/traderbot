"""R1: depois de EXECUTED, nada pode perder o registro do trade."""

import logging
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import make_intent, memory_gateway, mock_provider

from trader.async_account import AsyncAccount
from trader.execution.fills import Fill
from trader.execution.gateway import open_entry_of
from trader.execution.orders import order_from_fill
from trader.execution.resolve import resolved_fill
from trader.models import SOLANA_MINTS, OrderSide, SwapResult
from trader.models.account_data import MintBalance
from trader.models.costs import TradeCosts
from trader.models.intent import IntentSide, IntentStatus, PolicyDecision

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 9, 1, 12, 0)
BUY = SwapResult("sig", USDC.mint, SOL.mint, USDC.ui_to_raw("150"), SOL.ui_to_raw("1"))


class TestOrderFromFill:
    def test_a_zero_onchain_delta_falls_back_to_the_quote(self):
        costs = TradeCosts("onchain", actual_in_amount=0, actual_out_amount=0)

        order = order_from_fill(Fill(BUY, costs), USDC, SOL, OrderSide.BUY, T0)

        assert order.quantity == Decimal("1")
        assert order.quote_amount == Decimal("150")

    def test_nothing_valid_still_never_raises(self, caplog):
        empty = SwapResult("sig", USDC.mint, SOL.mint, 0, 0)

        with caplog.at_level(logging.ERROR):
            order = order_from_fill(Fill(empty), USDC, SOL, OrderSide.BUY, T0)

        assert order.quantity == 0
        assert "Fill sem quantidade" in caplog.text

    def test_a_sell_keeps_the_pair_convention(self):
        sell = SwapResult("s", SOL.mint, USDC.mint, SOL.ui_to_raw("1"), 160_000_000)

        order = order_from_fill(Fill(sell), USDC, SOL, OrderSide.SELL, T0)

        assert (order.input_mint, order.output_mint) == (USDC.mint, SOL.mint)
        assert order.quantity == Decimal("1")
        assert order.price == Decimal("160")


def _account(gateway):
    provider = mock_provider()
    provider.get_account_balance = AsyncMock(
        return_value=[
            MintBalance(mint=USDC.pubkey, available=Decimal("1000")),
            MintBalance(mint=SOL.pubkey, available=Decimal("1")),
        ]
    )
    provider.buy = AsyncMock(return_value=BUY)
    provider.fetch_swap_costs = AsyncMock(return_value=None)
    return AsyncAccount(provider, USDC.pubkey, SOL.pubkey, gateway, account_id="t")


class TestAccountSurvivesALostWrite:
    async def test_a_failed_record_fill_keeps_the_position(self, caplog):
        gateway = memory_gateway()
        account = _account(gateway)
        gateway.record_fill = lambda *a, **k: (_ for _ in ()).throw(
            OSError("database is locked")
        )

        with caplog.at_level(logging.ERROR):
            order = await account.buy(Decimal("150"), Decimal("1"))

        assert account.book.position is not None
        assert "Fill executado mas não gravado" in caplog.text
        assert order.order_id in caplog.text  # a ordem inteira vai para o log

    async def test_a_restart_rebuilds_the_entry_without_the_order(self):
        gateway = memory_gateway()
        first = _account(gateway)
        gateway_record_fill = gateway.record_fill
        gateway.record_fill = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
        await first.buy(Decimal("150"), Decimal("1"))
        gateway.record_fill = gateway_record_fill

        restarted = _account(gateway)
        restarted.restore_from_ledger()

        position = restarted.book.position
        assert position is not None  # nada de comprar de novo
        assert position.entry_order.quantity == Decimal("1")
        assert position.entry_order.order_id == "sig"


class TestGatewayKeepsTheResult:
    async def test_a_failed_mark_executed_logs_the_swap(self, caplog, monkeypatch):
        gateway = memory_gateway()
        intent = make_intent()
        monkeypatch.setattr(
            gateway.ledger,
            "mark_executed",
            lambda *a: (_ for _ in ()).throw(OSError("database is locked")),
        )

        async def swap():
            return BUY

        with caplog.at_level(logging.ERROR), pytest.raises(OSError):
            await gateway.submit(intent, swap)

        assert "Swap executado mas não registrado" in caplog.text
        assert "sig" in caplog.text
        record = gateway.ledger.get(intent.intent_id)
        assert record and record.status == IntentStatus.EXECUTING  # fail closed


def _unconfirmed(ledger, side, **fields):
    intent = make_intent(side=side, **fields)
    ledger.record_intent(intent, PolicyDecision(True))
    ledger.mark_unconfirmed(intent.intent_id, "timeout", signature="tx-sig")
    record = ledger.get(intent.intent_id)
    assert record is not None
    return record


class TestResolvedExecuted:
    async def test_a_resolved_buy_reopens_the_position(self):
        ledger = memory_gateway().ledger
        record = _unconfirmed(
            ledger,
            IntentSide.BUY,
            spend_amount="150",
            quantity=Decimal("1"),
            price=Decimal("150"),
        )

        order, realized, pnl = await resolved_fill(record, None, T0)
        ledger.resolve(record.intent.intent_id, IntentStatus.EXECUTED, "explorer")
        ledger.attach_order(record.intent.intent_id, order, realized, pnl)

        entry = open_entry_of(ledger, record.intent.account)
        assert entry is not None and entry.quantity == Decimal("1")
        assert entry.costs is None  # estimada: sem a transação real

    async def test_a_resolved_sell_uses_the_chain_and_books_pnl(self):
        ledger = memory_gateway().ledger
        entry = order_from_fill(Fill(BUY), USDC, SOL, OrderSide.BUY, T0)
        record = _unconfirmed(
            ledger,
            IntentSide.SELL,
            spend_mint=SOL.mint,
            receive_mint=USDC.mint,
            spend_amount="1",
            quantity=Decimal("1"),
            price=Decimal("160"),
        )
        chain = TradeCosts(
            "onchain",
            fee_lamports=5000,
            actual_in_amount=SOL.ui_to_raw("1"),
            actual_out_amount=USDC.ui_to_raw("158"),
        )
        fetch = AsyncMock(return_value=chain)

        order, realized, pnl = await resolved_fill(record, entry, T0, fetch)

        fetch.assert_awaited_once()
        assert fetch.await_args and fetch.await_args.args[0].signature == "tx-sig"
        assert order.side == OrderSide.SELL
        assert order.quote_amount == Decimal("158")  # o valor real, não o pedido
        assert pnl is not None and pnl.gross_quote == Decimal("8")
        assert realized is not None
