"""A quote conferida contra a Price API antes de executar (B7)."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.intent import TxOutcome
from trader.execution.models.mode import RunningMode
from trader.execution.trade.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.execution.wiring import build_provider
from trader.shared.models import SOLANA_MINTS, SwapResult

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")


class Recorder:
    native_fee_reserve = Decimal("0.02")

    def __init__(self):
        self.executed = []

    async def execute(self, input_mint, output_mint, quote):
        self.executed.append(quote)
        return SwapResult(
            "sig", input_mint, output_mint, int(quote.inAmount), int(quote.outAmount)
        )

    async def balances(self):
        return []

    async def fetch_costs(self, result):
        return None

    async def fetch_fee(self, signature):
        return None

    async def outcome(self, sent):
        return TxOutcome.PENDING

    async def token_balance(self, mint):
        return Decimal("0")

    async def close_token_account(self, mint, announce):
        return None

    async def aclose(self):
        return None


def _provider(out_sol: str, prices: dict | Exception):
    """10 USDC -> `out_sol` SOL, com a Price API respondendo `prices`."""
    quote = JupiterQuoteResponse.single_route(
        USDC.mint, 10_000_000, SOL.mint, SOL.ui_to_raw(Decimal(out_sol))
    )
    client = AsyncMock()
    client.get_quote = AsyncMock(
        side_effect=lambda i, o, amount, slippage: (
            quote
            if i == USDC.mint
            else JupiterQuoteResponse.single_route(
                SOL.mint, amount, USDC.mint, 9_000_000
            )
        )
    )
    if isinstance(prices, Exception):
        client.get_usd_prices = AsyncMock(side_effect=prices)
    else:
        client.get_usd_prices = AsyncMock(return_value=prices)
    executor = Recorder()
    provider = AsyncJupiterProvider(
        executor, client, max_quote_deviation_pct=Decimal(2)
    )
    return provider, executor


PRICES = {USDC.mint: Decimal(1), SOL.mint: Decimal(100)}


async def _buy(provider):
    return await provider.buy(USDC.pubkey, SOL.pubkey, Decimal(10))


@pytest.mark.parametrize("out_sol", ["0.1", "0.099", "0.0981", "0.2"])
async def test_a_quote_close_to_the_price_api_goes_through(out_sol):
    # 10 USDC valem 0.1 SOL: até 2% a menos passa (receber mais também)
    provider, executor = _provider(out_sol, PRICES)
    await _buy(provider)
    assert len(executor.executed) == 1


async def test_a_quote_more_than_two_percent_below_is_refused():
    provider, executor = _provider("0.0975", PRICES)  # 2.5% a menos
    with pytest.raises(SwapRejectedError, match="2.50% abaixo"):
        await _buy(provider)
    assert executor.executed == []


async def test_without_prices_a_buy_is_refused_and_a_sell_goes_on(caplog):
    provider, executor = _provider("0.1", OSError("Price API fora"))
    with pytest.raises(SwapRejectedError, match="sem preço independente"):
        await _buy(provider)

    await provider.sell(USDC.pubkey, SOL.pubkey, Decimal("0.1"))

    assert len(executor.executed) == 1  # só a venda
    assert "Quote não conferida" in caplog.text


async def test_off_by_default_and_on_in_the_wiring():
    provider, _ = _provider("0.05", PRICES)
    provider.max_quote_deviation_pct = None
    await _buy(provider)  # sem conferir
    assert build_provider(
        RunningMode.PAPER, 100_000
    ).max_quote_deviation_pct == Decimal(2)
