"""R1: depois de EXECUTED, nada pode perder o registro do trade."""

import logging
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import make_intent, memory_gateway, mock_provider

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentStatus
from trader.execution.models.venue import MintBalance
from trader.execution.trade.accounts.spot import SpotAccount
from trader.execution.trade.gateway import PolicyDeniedError
from trader.execution.trade.gateway.fills import Fill
from trader.execution.trade.gateway.orders import order_from_fill
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.models.costs import QUOTE, TradeCosts

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 9, 1, 12, 0)
BUY = ExecutionResult(
    "sig", USDC.mint, SOL.mint, USDC.ui_to_raw("150"), SOL.ui_to_raw("1")
)


class TestOrderFromFill:
    def test_a_zero_onchain_delta_falls_back_to_the_quote(self):
        costs = TradeCosts("onchain", actual_in_amount=0, actual_out_amount=0)

        order = order_from_fill(Fill(BUY, costs), USDC, SOL, OrderSide.BUY, T0)

        assert order.quantity == Decimal("1")
        assert order.quote_amount == Decimal("150")

    def test_nothing_valid_still_never_raises(self, caplog):
        empty = ExecutionResult("sig", USDC.mint, SOL.mint, 0, 0)

        with caplog.at_level(logging.ERROR):
            order = order_from_fill(Fill(empty), USDC, SOL, OrderSide.BUY, T0)

        assert order.quantity == 0
        assert "Fill sem quantidade" in caplog.text

    def test_a_sell_keeps_the_pair_convention(self):
        sell = ExecutionResult(
            "s", SOL.mint, USDC.mint, SOL.ui_to_raw("1"), 160_000_000
        )

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
    provider.fetch_swap_costs = AsyncMock(return_value=TradeCosts(source=QUOTE))
    return SpotAccount(
        SpotVenue(provider), USDC.pubkey, SOL.pubkey, gateway, account_id="t"
    )


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

    async def test_a_failed_mark_executed_blocks_the_next_buy_at_once(
        self, monkeypatch
    ):
        # T1: antes, uma EXECUTING recente não bloqueava (só depois de 300s), e
        # o próximo tick comprava de novo
        gateway = memory_gateway()
        real_mark = gateway.ledger.mark_executed
        monkeypatch.setattr(
            gateway.ledger,
            "mark_executed",
            lambda *a: (_ for _ in ()).throw(OSError("database is locked")),
        )

        async def swap():
            return BUY

        with pytest.raises(OSError):
            await gateway.submit(make_intent(), swap)
        monkeypatch.setattr(gateway.ledger, "mark_executed", real_mark)

        with pytest.raises(PolicyDeniedError, match="sem desfecho"):
            await gateway.submit(make_intent(), swap)
        # outra conta (outro bot) segue operando
        assert await gateway.submit(make_intent(account="paper:JUP-USDC"), swap)
