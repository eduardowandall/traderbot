from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from solders.keypair import Keypair

from trader.models import SwapResult
from trader.providers import JupiterQuoteResponse
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_jupiter_svc import (
    AsyncJupiterProvider,
    SwapRejectedError,
)
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient


def _quote(price_impact_pct="0.001"):
    # priceImpactPct da Jupiter é uma fração (0.01 == 1%), não um percentual
    return JupiterQuoteResponse.single_route(
        "in", 1000, "out", 990, price_impact_pct=price_impact_pct
    )


def _provider(**kwargs):
    return AsyncJupiterProvider.on_chain(
        Keypair(),
        rpc_client=AsyncMock(spec=AsyncRPCClient),
        jupiter_client=AsyncMock(spec=AsyncJupiterClient),
        **kwargs,
    )


class TestPriceImpact:
    async def test_quote_within_limit_is_accepted(self):
        # max_price_impact_pct=1 -> 1%; 0.0099 de fração = 0.99%, dentro do limite
        provider = _provider(max_price_impact_pct=Decimal("1"))
        provider.jupiter_client.get_quote = AsyncMock(return_value=_quote("0.0099"))
        quote = await provider._get_quote_with_route("in", "out", 1000)
        assert quote.priceImpactPct == "0.0099"

    @pytest.mark.parametrize("impact", ["0.015", "-0.02"])
    async def test_quote_above_limit_is_rejected(self, impact):
        # 0.015/-0.02 de fração = 1.5%/2%, acima do limite de 1%
        provider = _provider(max_price_impact_pct=Decimal("1"))
        provider.jupiter_client.get_quote = AsyncMock(return_value=_quote(impact))
        with pytest.raises(SwapRejectedError):
            await provider._get_quote_with_route("in", "out", 1000)

    async def test_limit_can_be_disabled(self):
        provider = _provider(max_price_impact_pct=None)
        provider.jupiter_client.get_quote = AsyncMock(return_value=_quote("0.5"))
        await provider._get_quote_with_route("in", "out", 1000)

    async def test_rejection_is_not_retried(self):
        # 0.05 de fração = 5%, acima do limite de 1%
        provider = _provider(max_price_impact_pct=Decimal("1"))
        provider.jupiter_client.get_quote = AsyncMock(return_value=_quote("0.05"))
        with pytest.raises(SwapRejectedError):
            await provider.swap("in", "out", 1000)
        provider.jupiter_client.get_quote.assert_awaited_once()
        provider.executor.rpc_client.send_transaction.assert_not_awaited()  # type: ignore[attr-defined]


class TestSlippageCeiling:
    @pytest.mark.parametrize(
        ("requested", "ceiling", "expected"),
        [
            (50, 100, [50, 50, 75]),
            (90, 100, [90, 90, 100]),  # escalada limitada ao teto
            (100, 100, [100, 100, 100]),
            (300, 100, [300, 300, 300]),  # pedido acima do teto: sem escalada
        ],
    )
    def test_retry_slippages(self, requested, ceiling, expected):
        provider = _provider(max_slippage_bps=ceiling)
        assert provider._retry_slippages(requested) == expected


async def test_do_swap_returns_quote_amounts():
    provider = _provider()
    provider._get_quote_with_route = AsyncMock(return_value=_quote())
    provider.executor._get_swap_transaction = AsyncMock()
    provider.executor._get_signed_transaction = AsyncMock()
    resp = AsyncMock()
    resp.to_json = lambda: '{"result": "sig"}'
    provider.executor._send_transaction_and_wait_for_confirmation = AsyncMock(
        return_value=resp
    )

    result = await provider._do_swap("in", "out", 1000)

    assert result == SwapResult("sig", "in", "out", 1000, 990)


async def test_aclose_closes_all_clients_even_if_one_fails():
    provider = _provider()
    provider.jupiter_client.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    provider.executor.rpc_client.aclose = AsyncMock()

    await provider.aclose()

    provider.jupiter_client.aclose.assert_awaited_once()
    provider.executor.rpc_client.aclose.assert_awaited_once()
