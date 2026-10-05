"""R3: leituras de saldo falham alto (nunca viram zero) e re-tentam o transitório."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from factories import memory_gateway, mock_provider
from solana.exceptions import SolanaRpcException

from trader.execution.gateway.account import AsyncAccount
from trader.execution.models.account_data import MintBalance
from trader.execution.venues.jupiter.async_rpc_client import (
    AsyncRPCClient,
    is_transient,
)
from trader.shared.models import SOLANA_MINTS

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
OWNER = SOL.pubkey  # qualquer pubkey serve


def _token_account(mint, raw):
    data = bytes(mint.pubkey) + bytes(32) + raw.to_bytes(8, "little") + bytes(93)
    return SimpleNamespace(account=SimpleNamespace(data=data))


def _wrapped(cause):
    """Como o solana-py embrulha: `SolanaRpcException` com o original em `__cause__`."""
    ex = SolanaRpcException.__new__(SolanaRpcException)
    Exception.__init__(ex, f"{type(cause)} raised in rpc")
    ex.error_msg = str(ex)
    ex.__cause__ = cause
    return ex


def _rpc(*token_account_responses):
    client = AsyncMock()
    client.is_connected = AsyncMock(return_value=True)
    client.get_token_accounts_by_owner = AsyncMock(
        side_effect=list(token_account_responses)
    )
    return AsyncRPCClient(client=client)


def _accounts(*items):
    return SimpleNamespace(value=list(items))


class TestTransient:
    def test_wrapped_timeouts_and_rate_limits_are_transient(self):
        request = httpx2.Request("POST", "https://rpc")
        limited = httpx2.HTTPStatusError(
            "429", request=request, response=httpx2.Response(429, request=request)
        )
        assert is_transient(_wrapped(httpx2.ReadTimeout("slow")))
        assert is_transient(_wrapped(limited))
        assert is_transient(httpx.ConnectError("down"))

    def test_real_errors_are_not(self):
        request = httpx2.Request("POST", "https://rpc")
        bad = httpx2.HTTPStatusError(
            "400", request=request, response=httpx2.Response(400, request=request)
        )
        assert not is_transient(_wrapped(bad))
        assert not is_transient(ValueError("x"))


class TestGetAccountBalance:
    async def test_sums_several_accounts_of_one_mint(self):
        rpc = _rpc(
            _accounts(_token_account(USDC, 5_000_000), _token_account(USDC, 2_000_000)),
            _accounts(),
        )

        balances = await rpc.get_account_balance(OWNER)

        assert balances == {USDC.pubkey: Decimal(7_000_000)}

    async def test_a_failed_read_raises_instead_of_returning_nothing(self, mock_sleep):
        error = _wrapped(ValueError("RPC recusou"))
        rpc = _rpc(error)

        with pytest.raises(SolanaRpcException):
            await rpc.get_account_balance(OWNER)

    async def test_a_transient_failure_is_retried(self, mock_sleep):
        # os dois programas são lidos juntos: a tentativa que falha gasta dois
        rpc = _rpc(
            _wrapped(httpx2.ReadTimeout("slow")),
            _accounts(),
            _accounts(_token_account(USDC, 1_000_000)),
            _accounts(),
        )

        balances = await rpc.get_account_balance(OWNER)

        assert balances == {USDC.pubkey: Decimal(1_000_000)}


class TestAccountNeverTradesOnAFailedRead:
    async def test_the_error_surfaces_and_is_not_cached(self):
        provider = mock_provider()
        good = [MintBalance(mint=USDC.pubkey, available=Decimal("100"))]
        provider.get_account_balance = AsyncMock(
            side_effect=[OSError("RPC fora"), good]
        )
        account = AsyncAccount(provider, USDC.pubkey, SOL.pubkey, memory_gateway())

        with pytest.raises(OSError):
            await account.get_balance(USDC.pubkey)

        assert await account.get_balance(USDC.pubkey) == Decimal("100")
