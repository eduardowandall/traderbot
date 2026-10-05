"""Inspeção da transação de swap antes de enviar (B7, só modo real)."""

from unittest.mock import AsyncMock

import pytest
from factories import bonk_quote, signs, simulation
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from trader.execution.trade.venues.jupiter.async_rpc_client import AsyncRPCClient
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.jupiter.tx_inspection import (
    ALLOWED_PROGRAMS,
    COMPUTE_BUDGET,
    JUPITER_V6,
    MAX_NATIVE_SPEND_LAMPORTS,
    TransactionInspectionError,
    WalletState,
    check_balances,
    check_programs,
    state_after,
    token_amount,
)
from trader.shared.models.mints import SOL_MINT

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _tx(*programs: str) -> VersionedTransaction:
    payer = Keypair()
    instructions = [Instruction(Pubkey.from_string(p), b"", []) for p in programs]
    message = MessageV0.try_compile(payer.pubkey(), instructions, [], Hash.default())
    return VersionedTransaction(message, [payer])


def _token_data(mint: str, amount: int) -> bytes:
    return bytes(Pubkey.from_string(mint)) + bytes(32) + amount.to_bytes(8, "little")


class TestPrograms:
    def test_a_jupiter_swap_passes(self):
        check_programs(_tx(COMPUTE_BUDGET, JUPITER_V6, *ALLOWED_PROGRAMS))

    def test_any_other_program_is_refused(self):
        stranger = str(Keypair().pubkey())
        with pytest.raises(TransactionInspectionError, match=stranger):
            check_programs(_tx(JUPITER_V6, stranger))


BEFORE = WalletState(
    lamports=1_000_000_000,
    tokens={"usdc-acc": (USDC, 100_000_000), "bonk-acc": (BONK, 5_000)},
)


def _after(lamports=999_990_000, usdc=50_000_000, bonk=5_000):
    return WalletState(lamports, {"usdc-acc": (USDC, usdc), "bonk-acc": (BONK, bonk)})


class TestBalances:
    def test_spending_the_quoted_input_passes(self):
        check_balances(BEFORE, _after(), USDC, in_amount=50_000_000)

    def test_spending_more_than_the_quote_is_refused(self):
        with pytest.raises(TransactionInspectionError, match="mais que o inAmount"):
            check_balances(BEFORE, _after(usdc=40_000_000), USDC, in_amount=50_000_000)

    def test_any_other_token_leaving_is_refused(self):
        with pytest.raises(
            TransactionInspectionError, match="não é o token de entrada"
        ):
            check_balances(BEFORE, _after(bonk=0), USDC, in_amount=50_000_000)

    def test_a_closed_account_counts_as_emptied(self):
        after = WalletState(999_990_000, {"usdc-acc": (USDC, 50_000_000)})
        with pytest.raises(TransactionInspectionError, match="bonk-acc"):
            check_balances(BEFORE, after, USDC, in_amount=50_000_000)

    def test_lamports_beyond_fees_are_refused_unless_sol_is_the_input(self):
        drained = _after(lamports=1_000_000_000 - MAX_NATIVE_SPEND_LAMPORTS - 1)
        with pytest.raises(TransactionInspectionError, match="lamports"):
            check_balances(BEFORE, drained, USDC, in_amount=50_000_000)
        spend = 200_000_000
        paid = _after(lamports=1_000_000_000 - spend - 10_000, usdc=100_000_000)
        check_balances(BEFORE, paid, SOL_MINT, in_amount=spend)


class TestSimulatedState:
    def test_accounts_come_back_in_the_requested_order(self):
        owner = Keypair().pubkey()
        before = WalletState(5, {str(Keypair().pubkey()): (USDC, 9)})
        assert before.addresses(owner)[0] == owner
        response = simulation(4, [_token_data(USDC, 7)]).value.accounts
        assert state_after(before, response).tokens == {
            next(iter(before.tokens)): (USDC, 7)
        }
        assert token_amount(b"short") == 0

    def test_an_rpc_that_ignores_the_accounts_is_refused(self):
        with pytest.raises(TransactionInspectionError, match="não devolveu"):
            state_after(BEFORE, None)


async def test_the_executor_refuses_a_draining_transaction_before_sending():
    quote = bonk_quote()  # 50 USDC -> 50 BONK
    keypair = Keypair()
    rpc = AsyncMock(spec=AsyncRPCClient)
    account = str(Keypair().pubkey())
    rpc.wallet_state = AsyncMock(
        return_value=WalletState(10**9, {account: (quote.inputMint, 80_000_000)})
    )
    # a simulação mostra 80 USDC saindo: mais que os 50 da quote
    rpc.simulate_transaction = AsyncMock(
        return_value=simulation(10**9, [_token_data(quote.inputMint, 0)])
    )
    signs(rpc)
    client = AsyncMock()
    client.get_swap_transaction = AsyncMock(return_value=_tx(JUPITER_V6))
    executor = OnChainExecutor(keypair, rpc, client, 100_000)

    with pytest.raises(TransactionInspectionError, match="mais que o inAmount"):
        await executor.execute(quote.inputMint, quote.outputMint, quote)

    rpc.send_transaction.assert_not_awaited()
    (call,) = rpc.simulate_transaction.await_args_list
    assert call.args[1] == [keypair.pubkey(), Pubkey.from_string(account)]
