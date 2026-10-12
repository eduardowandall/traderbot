"""As costuras do A7 e da A23: `Venue`, `Wallet`, as contas, `ExecutionResult`.

Nenhum comportamento muda: o `SpotVenue` só repassa ao provider da Jupiter,
e o resultado de uma execução grava no ledger o mesmo que o `SwapResult`.
"""

import ast
import inspect
from dataclasses import asdict
from decimal import Decimal

from factories import make_order, memory_gateway, mock_provider

from trader.execution.models.bucket import BucketAccount
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.venue import Venue, Wallet
from trader.execution.trade.accounts import perp as perp_module
from trader.execution.trade.accounts.base import BaseAccount
from trader.execution.trade.accounts.perp import PerpAccount
from trader.execution.trade.accounts.spot import SpotAccount
from trader.execution.trade.accounts.wallet import WalletBalances
from trader.execution.trade.trading_service import perps as perps_module
from trader.execution.trade.trading_service import service as service_module
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
    # o evento `intent_executed` guarda `asdict(result)`: os campos do swap, + perp
    assert list(asdict(RESULT)) == [
        "signature",
        "input_mint",
        "output_mint",
        "in_amount",
        "out_amount",
        "costs",
        "quote",
        "failed_signatures",
        "perp",  # A8: None no spot
        "venue_order",  # A12: o stop de uma perp
    ]


def _names(module) -> set[str]:
    """Os nomes que o módulo importa e os que ele usa (a árvore dele)."""
    tree = ast.parse(inspect.getsource(module))
    imported = {
        a.asname or a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for a in n.names
    }
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return imported | used


def test_the_perp_account_stands_on_the_base_not_on_spot():
    # A23: as duas contas dividem a `BaseAccount`; a de perp não é spot e
    # não conhece o `Venue` do swap (só a carteira e o `PerpVenue`)
    assert issubclass(PerpAccount, BaseAccount) and issubclass(SpotAccount, BaseAccount)
    assert not issubclass(PerpAccount, SpotAccount)
    names = _names(perp_module)
    assert "SpotAccount" not in names and "Venue" not in names


def test_the_wallet_reserve_is_the_wallets():
    # A23: `WalletBalances` lê um `Wallet` (o `SpotVenue` é um) e só o SOL
    # deixa a reserva de taxas
    wallet: Wallet = SpotVenue(mock_provider())
    balances = WalletBalances(wallet)
    assert balances.spendable(SOL, Decimal("1")) == Decimal("0.98")
    assert balances.spendable(SOL, Decimal("0.01")) == 0
    assert balances.spendable(USDC, Decimal("1")) == Decimal("1")


def test_the_service_keeps_no_perp_type_checks():
    # A23: o lado de perps é o `PerpDesk`; o bucket guarda a conta tipada
    for module in (service_module, perps_module):
        tree = ast.parse(inspect.getsource(module))
        checked = {
            ast.unparse(call.args[1])
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "isinstance"
        }
        assert "PerpAccount" not in checked
