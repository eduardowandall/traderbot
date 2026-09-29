from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import memory_gateway

from trader.async_account import AsyncAccount
from trader.models import SOLANA_MINTS
from trader.paper import (
    InsufficientFundsError,
    SimulatedExecutor,
    SimulatedWallet,
    paper_provider,
    parse_balances,
)
from trader.providers import JupiterQuoteResponse
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_jupiter_svc import SwapRejectedError

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")


def _quote(in_mint, in_amount, out_mint, out_amount, impact="0.01"):
    return JupiterQuoteResponse.single_route(
        in_mint, in_amount, out_mint, out_amount, price_impact_pct=impact
    )


class TestParseBalances:
    def test_parses(self):
        assert parse_balances("USDC=100 SOL=0.5") == {
            "USDC": Decimal("100"),
            "SOL": Decimal("0.5"),
        }

    @pytest.mark.parametrize("spec", ["USDC", "FOO=1", "USDC=-1", "USDC=nan"])
    def test_rejects(self, spec):
        with pytest.raises(ValueError):
            parse_balances(spec)


class TestSimulatedWallet:
    def test_reset_and_balances(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("10"), "SOL": Decimal("0")})
        assert wallet.balance(USDC.mint) == Decimal("10")
        assert wallet.balances() == {USDC.mint: Decimal("10")}
        assert not wallet.is_empty

    def test_apply_swap_with_fee(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("10"), "SOL": Decimal("1")})
        wallet.apply_swap(
            USDC.mint, USDC.ui_to_raw("5"), SOL.mint, SOL.ui_to_raw("0.03"), 5000
        )
        assert wallet.balance(USDC.mint) == Decimal("5")
        assert wallet.balance(SOL.mint) == Decimal("1.029995")

    def test_fee_when_spending_sol(self):
        wallet = SimulatedWallet(initial={"SOL": Decimal("1")})
        wallet.apply_swap(SOL.mint, SOL.ui_to_raw("1") - 5000, USDC.mint, 1, 5000)
        assert wallet.raw_balance(SOL.mint) == 0

    def test_insufficient_funds_changes_nothing(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("1"), "SOL": Decimal("1")})
        with pytest.raises(InsufficientFundsError, match="USDC"):
            wallet.apply_swap(USDC.mint, USDC.ui_to_raw("2"), SOL.mint, 1)
        with pytest.raises(InsufficientFundsError, match="SOL"):
            wallet.apply_swap(USDC.mint, 1, SOL.mint, 1, SOL.ui_to_raw("2"))
        assert wallet.balance(USDC.mint) == Decimal("1")
        assert wallet.balance(SOL.mint) == Decimal("1")

    def test_insufficient_funds_is_not_retried(self):
        assert issubclass(InsufficientFundsError, SwapRejectedError)

    def test_persists(self, tmp_path):
        path = tmp_path / "wallet.json"
        wallet = SimulatedWallet(path, initial={"USDC": Decimal("10")})
        wallet.apply_swap(USDC.mint, USDC.ui_to_raw("4"), SOL.mint, 123)

        reloaded = SimulatedWallet(path)
        assert reloaded.balance(USDC.mint) == Decimal("6")
        assert reloaded.raw_balance(SOL.mint) == 123

    def test_existing_file_wins_over_initial(self, tmp_path):
        path = tmp_path / "wallet.json"
        SimulatedWallet(path, initial={"USDC": Decimal("10")})
        assert SimulatedWallet(path, initial={"USDC": Decimal("99")}).balance(
            USDC.mint
        ) == Decimal("10")


def _provider(wallet, quote):
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_quote = AsyncMock(return_value=quote)
    return paper_provider(wallet, jupiter_client=client)


class TestPaperProvider:
    async def test_swap_applies_real_quote_to_wallet(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        quote = _quote(USDC.mint, USDC.ui_to_raw("10"), SOL.mint, SOL.ui_to_raw("0.07"))
        provider = _provider(wallet, quote)

        result = await provider.swap_with_details(
            USDC.mint, SOL.mint, USDC.ui_to_raw("10")
        )

        assert result.signature.startswith("paper-")
        assert result.out_amount == SOL.ui_to_raw("0.07")
        assert wallet.balance(USDC.mint) == Decimal("90")
        assert wallet.balance(SOL.mint) == Decimal("1.069995")  # taxa de 5000 lamports

    async def test_insufficient_balance_is_rejected_once(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("1"), "SOL": Decimal("1")})
        quote = _quote(USDC.mint, USDC.ui_to_raw("10"), SOL.mint, 1)
        provider = _provider(wallet, quote)

        with pytest.raises(InsufficientFundsError):
            await provider.swap_with_details(USDC.mint, SOL.mint, USDC.ui_to_raw("10"))
        provider.jupiter_client.get_quote.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_price_impact_cap_still_applies(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        quote = _quote(USDC.mint, 1, SOL.mint, 1, impact="5")
        with pytest.raises(SwapRejectedError):
            await _provider(wallet, quote).swap_with_details(USDC.mint, SOL.mint, 1)
        assert wallet.balance(USDC.mint) == Decimal("100")

    async def test_balances_come_from_wallet(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("5"), "JUP": Decimal("2")})
        balances = await _provider(wallet, None).get_account_balance()
        assert {str(b.mint): b.available for b in balances} == {
            USDC.mint: Decimal("5"),
            JUP.mint: Decimal("2"),
        }

    async def test_never_touches_rpc_or_keys(self):
        # a execução simulada não tem RPC nem chave para usar, por construção
        provider = _provider(SimulatedWallet(), None)
        assert isinstance(provider.executor, SimulatedExecutor)
        for attr in ("rpc_client", "keypair", "pubkey"):
            assert not hasattr(provider.executor, attr)
        await provider.aclose()  # não falha

    async def test_full_round_trip_through_account(self):
        # compra e venda pelo AsyncAccount: em paper a venda funciona (em dry
        # falhava porque a carteira real não tinha o token)
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        client = AsyncMock(spec=AsyncJupiterClient)
        client.get_quote = AsyncMock(
            side_effect=[
                _quote(USDC.mint, USDC.ui_to_raw("10"), SOL.mint, SOL.ui_to_raw("0.1")),
                _quote(SOL.mint, SOL.ui_to_raw("0.1"), USDC.mint, USDC.ui_to_raw("11")),
            ]
        )
        provider = paper_provider(wallet, jupiter_client=client)
        account = AsyncAccount(provider, USDC.pubkey, SOL.pubkey, memory_gateway())

        await account.buy(Decimal("100"), Decimal("0.1"))
        await account.sell(Decimal("110"), Decimal("0.1"))

        assert account.current_position is None
        # bruto: 11 - 10 USDC; custos: 2 x 5000 lamports, convertidos com o
        # preço do SOL de cada perna (100 e 110 USD)
        assert account.total_gross_quote == Decimal("1")
        assert account.total_costs_sol == Decimal("0.00001")
        assert account.total_net_quote == Decimal("1") - Decimal("0.00105")
        assert account.get_total_realized_pnl() == Decimal("1") - Decimal("0.00105")
        assert account.incomplete_trades == 0
        assert wallet.balance(USDC.mint) == Decimal("101")
