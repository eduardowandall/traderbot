import dataclasses
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import memory_gateway

from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.execution.market.jupiter.quote import JupiterQuoteResponse
from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.trade.accounts.spot import SpotAccount
from trader.execution.trade.venues.paper import (
    InsufficientFundsError,
    SimulatedExecutor,
    SimulatedWallet,
    paper_provider,
)
from trader.execution.trade.venues.paper import wallet as wallet_module
from trader.execution.trade.venues.paper.executor import DEFAULT_FEE_LAMPORTS
from trader.execution.trade.venues.paper.wallet import APPLIED_LOG_SIZE
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")


def _quote(in_mint, in_amount, out_mint, out_amount, impact="0.01"):
    return JupiterQuoteResponse.single_route(
        in_mint, in_amount, out_mint, out_amount, price_impact_pct=impact
    )


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

    async def test_the_priority_fee_cap_is_charged_on_every_leg(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        quote = _quote(USDC.mint, USDC.ui_to_raw("10"), SOL.mint, SOL.ui_to_raw("0.07"))
        client = AsyncMock(spec=AsyncJupiterClient)
        client.get_quote = AsyncMock(return_value=quote)
        provider = paper_provider(
            wallet, jupiter_client=client, priority_fee_lamports=100_000
        )

        result = await provider.swap_with_details(
            USDC.mint, SOL.mint, USDC.ui_to_raw("10")
        )

        # como on-chain: `fee_lamports` é o total, a parte priority à parte
        assert result.costs is not None
        assert result.costs.fee_lamports == 105_000
        assert result.costs.priority_fee_lamports == 100_000
        assert wallet.balance(SOL.mint) == Decimal("1.069895")

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
        # compra e venda pelo SpotAccount: em paper a venda funciona (em dry
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
        account = SpotAccount(
            SpotVenue(provider), USDC.pubkey, SOL.pubkey, memory_gateway()
        )

        await account.buy(Decimal("100"), Decimal("0.1"))
        await account.sell(Decimal("110"), Decimal("0.1"))

        assert account.book.position is None
        # bruto: 11 - 10 USDC; custos: 2 x 5000 lamports, convertidos com o
        # preço do SOL de cada perna (100 e 110 USD)
        assert account.book.gross_quote == Decimal("1")
        assert account.book.costs_sol == Decimal("0.00001")
        assert account.book.net_quote == Decimal("1") - Decimal("0.00105")
        assert account.book.realized_usd == Decimal("1") - Decimal("0.00105")
        assert account.book.incomplete == 0
        assert wallet.balance(USDC.mint) == Decimal("101")


class TestPaperSlippage:
    def _quote(self, threshold: int):
        quote = JupiterQuoteResponse.single_route(
            USDC.mint, 10_000_000, SOL.mint, 1_000_000
        )
        return dataclasses.replace(quote, otherAmountThreshold=str(threshold))

    @pytest.mark.parametrize(
        ("threshold", "filled"),
        [
            (950_000, 999_000),  # 10 bps abaixo da quote
            (999_900, 999_900),  # nunca abaixo do mínimo da quote
            (1_000_000, 1_000_000),  # sem tolerância: o fill é a quote
        ],
    )
    async def test_the_fill_is_below_the_quote_but_not_below_its_minimum(
        self, threshold, filled
    ):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        executor = SimulatedExecutor(wallet)
        result = await executor.execute(USDC.mint, SOL.mint, self._quote(threshold))

        assert result.out_amount == filled
        assert result.costs is not None
        assert result.costs.actual_out_amount == filled
        assert wallet.raw_balance(SOL.mint) == 10**9 + filled - DEFAULT_FEE_LAMPORTS


class TestAppliedLog:
    """A3: a carteira paper lembra os swaps aplicados, para resolver intenções."""

    async def test_an_applied_swap_landed_and_its_costs_can_be_read_back(
        self, tmp_path
    ):
        wallet = SimulatedWallet(
            tmp_path / "w.json", initial={"USDC": Decimal("100"), "SOL": Decimal("1")}
        )
        executor = SimulatedExecutor(wallet)
        result = await executor.execute(
            USDC.mint, SOL.mint, _quote(USDC.mint, 10_000_000, SOL.mint, 1_000_000)
        )
        sent = SentTx(result.signature, USDC.mint, SOL.mint, 10_000_000, 1_000_000)

        # outro processo (o `serve` reiniciado) lê o mesmo arquivo
        again = SimulatedExecutor(SimulatedWallet(tmp_path / "w.json"))
        assert await again.outcome(sent) == TxOutcome.LANDED
        bare = dataclasses.replace(result, costs=None)
        assert await again.fetch_costs(bare) == result.costs
        missing = dataclasses.replace(sent, signature="paper-outro")
        assert await again.outcome(missing) == TxOutcome.EXPIRED

    def test_the_log_keeps_only_the_last_swaps(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100")})
        for i in range(APPLIED_LOG_SIZE + 5):
            wallet.apply_swap(USDC.mint, 1, SOL.mint, 1, signature=f"s{i}")
        assert wallet.applied("s4") is None
        assert wallet.applied(f"s{APPLIED_LOG_SIZE + 4}") is not None
        assert wallet.trimmed_at is not None

    async def test_a_send_that_may_have_been_trimmed_is_pending_not_expired(
        self, monkeypatch, tmp_path
    ):
        # aplicado e cortado do registro: dizer EXPIRED perderia a posição
        monkeypatch.setattr(wallet_module, "APPLIED_LOG_SIZE", 2)
        clock = iter(range(100, 200))
        monkeypatch.setattr(wallet_module.time, "time", lambda: next(clock))
        wallet = SimulatedWallet(tmp_path / "w.json", initial={"USDC": Decimal("100")})
        executor = SimulatedExecutor(wallet)
        old = SentTx("s0", USDC.mint, SOL.mint, 1, 1, sent_at=100)
        for i in range(4):
            wallet.apply_swap(USDC.mint, 1, SOL.mint, 1, signature=f"s{i}")

        again = SimulatedExecutor(SimulatedWallet(tmp_path / "w.json"))
        assert await again.outcome(old) == TxOutcome.PENDING
        # enviado depois do último corte e fora do registro: nunca aplicado
        late = SentTx("nunca", USDC.mint, SOL.mint, 1, 1, sent_at=150)
        assert await executor.outcome(late) == TxOutcome.EXPIRED

    def test_an_old_wallet_file_has_an_empty_log(self, tmp_path):
        path = tmp_path / "old.json"
        path.write_text('{"balances": {}, "open_accounts": []}', encoding="utf-8")
        assert SimulatedWallet(path).applied("x") is None
