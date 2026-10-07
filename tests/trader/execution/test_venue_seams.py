"""As costuras do A7: `Venue`, `SpotVenue`, `BucketAccount`, `ExecutionResult`.

Nenhum comportamento muda: o `SpotVenue` só repassa ao provider da Jupiter,
e o resultado de uma execução grava no ledger o mesmo que o `SwapResult`.
"""

from dataclasses import asdict
from decimal import Decimal

from factories import make_order, memory_gateway, mock_provider

from trader.execution.models.bucket import BucketAccount
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.venue import Venue
from trader.execution.trade.gateway.account import SpotAccount
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS

USDC = SOLANA_MINTS.get_by_symbol("USDC").pubkey
SOL = SOLANA_MINTS.get_by_symbol("SOL").pubkey
RESULT = ExecutionResult("sig", str(USDC), str(SOL), 1, 2)


async def test_spot_venue_passes_every_call_to_the_provider():
    provider = mock_provider()
    provider.buy.return_value = provider.sell.return_value = RESULT
    provider.send_outcome.return_value = TxOutcome.LANDED
    venue: Venue = SpotVenue(provider)

    assert venue.native_fee_reserve == Decimal("0.02")
    assert await venue.open(USDC, SOL, Decimal(5)) is RESULT
    provider.buy.assert_awaited_once_with(USDC, SOL, Decimal(5))
    assert await venue.close(USDC, SOL, Decimal("0.1")) is RESULT
    provider.sell.assert_awaited_once_with(USDC, SOL, quantity=Decimal("0.1"))
    await venue.balances()
    provider.get_account_balance.assert_awaited_once()
    await venue.token_balance(SOL)
    provider.token_balance.assert_awaited_once_with(SOL)
    await venue.fetch_costs(RESULT)
    provider.fetch_swap_costs.assert_awaited_once_with(RESULT)
    assert await venue.fetch_failed_fees(["a"]) == 0
    sent = SentTx("sig", str(USDC), str(SOL), 1, 2)
    assert await venue.send_outcome(sent) == TxOutcome.LANDED
    await venue.close_token_account(str(SOL), print)
    provider.close_token_account.assert_awaited_once_with(str(SOL), print)
    await venue.aclose()
    provider.aclose.assert_awaited_once()


def test_spot_account_commits_what_the_open_position_spent():
    gateway = memory_gateway()
    account: BucketAccount = SpotAccount(SpotVenue(mock_provider()), USDC, SOL, gateway)
    assert account.committed() == 0
    entry = make_order(quantity="0.1", price="100")
    account.book.open(entry)
    assert entry.quote_amount
    assert account.committed() == entry.quote_amount


def test_the_executed_payload_keeps_the_swap_result_fields():
    # o evento `intent_executed` guarda `asdict(result)`: sem mudança de formato
    assert list(asdict(RESULT)) == [
        "signature",
        "input_mint",
        "output_mint",
        "in_amount",
        "out_amount",
        "costs",
        "quote",
        "failed_signatures",
    ]
