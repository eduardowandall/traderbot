from decimal import Decimal

import pytest
from factories import SOL, USDC, make_intent, memory_gateway, mock_provider

from trader.execution import PolicyDeniedError
from trader.execution.fills import Fill, execute_trade
from trader.models.costs import TradeCosts
from trader.models.intent import IntentStatus
from trader.models.order import SwapResult
from trader.policy import Policy

RESULT = SwapResult("sig", USDC, SOL, in_amount=100, out_amount=7)


async def _swap():
    return RESULT


class TestFillAmounts:
    def test_unknown_costs_use_the_quote(self):
        assert Fill(RESULT).amounts() == (100, 7)

    def test_actual_amounts_win_when_known(self):
        costs = TradeCosts("onchain", actual_in_amount=99, actual_out_amount=6)
        assert Fill(RESULT, costs).amounts() == (99, 6)

    def test_each_side_falls_back_on_its_own(self):
        costs = TradeCosts("onchain", actual_out_amount=6)
        assert Fill(RESULT, costs).amounts() == (100, 6)


class TestExecuteTrade:
    async def test_costs_are_fetched_only_after_executed(self):
        gateway = memory_gateway()
        intent = make_intent()
        costs = TradeCosts("onchain", fee_lamports=5000)
        seen = []

        async def fetch(result):
            record = gateway.ledger.get(intent.intent_id)
            seen.append(record and record.status)
            return costs

        provider = mock_provider()
        provider.fetch_swap_costs.side_effect = fetch

        fill = await execute_trade(gateway, provider, intent, _swap)

        assert seen == [IntentStatus.EXECUTED]
        assert fill == Fill(RESULT, costs)

    async def test_non_cost_values_count_as_unknown(self):
        provider = mock_provider()  # fetch_swap_costs devolve um Mock

        fill = await execute_trade(memory_gateway(), provider, make_intent(), _swap)

        assert fill.costs is None

    async def test_a_denial_never_fetches_costs(self):
        gateway = memory_gateway(Policy(max_trade_usd=Decimal("1")))
        provider = mock_provider()

        with pytest.raises(PolicyDeniedError):
            await execute_trade(gateway, provider, make_intent(), _swap)

        provider.fetch_swap_costs.assert_not_awaited()


class TestInMemoryGateway:
    async def test_keeps_idempotency(self):
        gateway = memory_gateway()
        intent = make_intent(key="k")
        await execute_trade(gateway, mock_provider(), intent, _swap)

        with pytest.raises(Exception, match="duplicada"):
            await execute_trade(gateway, mock_provider(), make_intent(key="k"), _swap)
