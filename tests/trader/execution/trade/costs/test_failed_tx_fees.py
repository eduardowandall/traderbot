"""Taxas de transações que a rede confirmou como falhas (B5).

A taxa foi paga mesmo sem troca: vira um evento `failed_tx_fee`, sai do PnL
do bucket em memória e volta igual depois de um reinício.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import SOL, USDC, memory_gateway
from solders.pubkey import Pubkey

from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.execution.models.account_data import MintBalance
from trader.execution.models.errors import (
    SwapFailedError,
    TransactionFailedOnChainError,
)
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentStatus, TxOutcome
from trader.execution.trade.gateway.account import SpotAccount
from trader.execution.trade.ledger.reports import FAILED_TX_FEE
from trader.execution.trade.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models.costs import BASE_FEE_LAMPORTS, TradeCosts

FEE = 15_000  # lamports por transação falha
SOL_USD = Decimal("200")
FEE_USD = Decimal(FEE) / 10**9 * SOL_USD  # 0.003


class FlakyExecutor:
    """Falha na rede `failures` vezes, depois executa a quote."""

    native_fee_reserve = Decimal("0.02")

    def __init__(self, failures: int, fee: int | None = FEE):
        self.failures = failures
        self.fee = fee
        self.calls = 0

    async def balances(self):
        return [
            MintBalance(Decimal("100"), Pubkey.from_string(USDC)),
            MintBalance(Decimal("1"), Pubkey.from_string(SOL)),
        ]

    async def execute(self, input_mint, output_mint, quote):
        self.calls += 1
        if self.calls <= self.failures:
            raise TransactionFailedOnChainError(
                "slippage", signature=f"fail-{self.calls}"
            )
        return ExecutionResult(
            f"ok-{self.calls}",
            input_mint,
            output_mint,
            int(quote.inAmount),
            int(quote.outAmount),
            costs=TradeCosts("onchain", fee_lamports=5000),
            quote=quote,
        )

    async def fetch_costs(self, result):
        return result.costs

    async def fetch_fee(self, signature):
        if self.fee is None:
            raise RuntimeError("rpc caiu")
        return self.fee

    async def outcome(self, sent):
        return TxOutcome.PENDING

    async def token_balance(self, mint):
        return Decimal("0")

    async def close_token_account(self, mint, announce):
        return None

    async def aclose(self):
        return None


class SolPrice:
    async def usd_prices(self, mints):
        return {SOL: SOL_USD}


def _provider(executor: FlakyExecutor) -> AsyncJupiterProvider:
    quotes = AsyncMock()
    quotes.get_quote = AsyncMock(
        return_value=JupiterQuoteResponse.single_route(
            USDC, 10_000_000, SOL, 100_000_000
        )
    )
    return AsyncJupiterProvider(executor, quotes)


def _account(provider, gateway) -> SpotAccount:
    account = SpotAccount(
        SpotVenue(provider),
        Pubkey.from_string(USDC),
        Pubkey.from_string(SOL),
        gateway=gateway,
        account_id="paper:strategy:x",
        prices=SolPrice(),
    )
    account.restore_from_ledger()
    return account


def _events(gateway):
    return gateway.ledger.conn.execute(
        "SELECT payload FROM events WHERE type = ?", (FAILED_TX_FEE,)
    ).fetchall()


class TestProvider:
    async def test_a_filled_swap_carries_the_failed_attempts(self, mock_sleep):
        result = await _provider(FlakyExecutor(failures=1)).buy(
            Pubkey.from_string(USDC), Pubkey.from_string(SOL), Decimal("10")
        )
        assert result.failed_signatures == ("fail-1",)

    async def test_exhausted_retries_carry_them_on_the_error(self, mock_sleep):
        with pytest.raises(SwapFailedError) as raised:
            await _provider(FlakyExecutor(failures=3)).buy(
                Pubkey.from_string(USDC), Pubkey.from_string(SOL), Decimal("10")
            )
        assert isinstance(raised.value, RuntimeError)  # quem já trata segue igual
        assert raised.value.failed_signatures == ("fail-1", "fail-2", "fail-3")

    async def test_an_unreadable_fee_counts_as_the_base_fee(self):
        provider = _provider(FlakyExecutor(failures=0, fee=None))
        assert await provider.fetch_failed_fees(["a", "b"]) == 2 * BASE_FEE_LAMPORTS


class TestBooking:
    async def test_a_fill_after_a_failed_attempt_books_its_fee(self, mock_sleep):
        gateway = memory_gateway()
        account = _account(_provider(FlakyExecutor(failures=1)), gateway)

        await account.buy(Decimal("100"), Decimal("0.1"))

        assert account.book.realized_usd == -FEE_USD
        assert len(_events(gateway)) == 1
        totals = gateway.ledger.pnl_totals("paper:strategy:x")
        assert (totals.failed_tx, totals.failed_fee_lamports) == (1, FEE)
        assert totals.net_usd == -FEE_USD

    async def test_a_swap_that_never_fills_still_books_the_fees(self, mock_sleep):
        gateway = memory_gateway()
        account = _account(_provider(FlakyExecutor(failures=3)), gateway)

        with pytest.raises(SwapFailedError):
            await account.buy(Decimal("100"), Decimal("0.1"))

        assert account.book.realized_usd == -3 * FEE_USD
        assert account.book.position is None
        record = gateway.ledger.list_intents(1)[0]
        assert record.status == IntentStatus.FAILED

        # um reinício restaura o mesmo PnL (o orçamento vê o custo)
        restored = _account(_provider(FlakyExecutor(failures=0)), gateway)
        assert restored.book.realized_usd == -3 * FEE_USD
        assert restored.book.failed_fee_sol == Decimal(3 * FEE) / 10**9

    async def test_windowed_totals_only_count_fees_in_the_window(self, mock_sleep):
        gateway = memory_gateway()
        account = _account(_provider(FlakyExecutor(failures=1)), gateway)
        await account.buy(Decimal("100"), Decimal("0.1"))

        later = datetime.now(UTC) + timedelta(hours=1)
        window = gateway.ledger.pnl_totals("paper:strategy:x", start=later)
        assert (window.trades, window.failed_tx) == (0, 0)
