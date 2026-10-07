import json
from decimal import Decimal
from unittest.mock import AsyncMock

from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.gateway.account import SpotAccount
from trader.execution.trade.ledger import Ledger
from trader.execution.trade.policy import Policy
from trader.execution.trade.venues import JupiterQuoteResponse
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.order import order_from_json

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
RENT = 2_039_280


def _paper(wallet, quotes):
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_quote = AsyncMock(side_effect=quotes)
    return paper_provider(wallet, jupiter_client=client)


def _quote(in_mint, in_amount, out_mint, out_amount):
    return JupiterQuoteResponse.single_route(in_mint, in_amount, out_mint, out_amount)


class TestPaperRent:
    async def test_first_token_account_pays_rent_once(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        buy = _quote(USDC.mint, USDC.ui_to_raw("10"), JUP.mint, JUP.ui_to_raw("20"))
        provider = _paper(wallet, [buy, buy])

        first = await provider.swap_with_details(USDC.mint, JUP.mint, int(buy.inAmount))
        second = await provider.swap_with_details(
            USDC.mint, JUP.mint, int(buy.inAmount)
        )

        assert first.costs is not None and first.costs.rent_lamports == RENT
        assert second.costs is not None and second.costs.rent_lamports == 0
        assert wallet.raw_balance(SOL.mint) == 10**9 - RENT - 2 * 5000

    async def test_sol_never_pays_rent(self):
        wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
        buy = _quote(USDC.mint, USDC.ui_to_raw("10"), SOL.mint, SOL.ui_to_raw("0.1"))
        result = await _paper(wallet, [buy]).swap_with_details(
            USDC.mint, SOL.mint, int(buy.inAmount)
        )
        assert result.costs is not None and result.costs.rent_lamports == 0

    def test_old_wallet_files_still_load(self, tmp_path):
        path = tmp_path / "wallet.json"
        path.write_text(
            json.dumps({"balances": {USDC.mint: 1, JUP.mint: 0}}), encoding="utf-8"
        )
        wallet = SimulatedWallet(path)
        # quem tem saldo já tem conta; JUP com saldo 0 ainda precisa abrir
        assert not wallet.needs_account(USDC.mint)
        assert wallet.needs_account(JUP.mint)
        assert not wallet.needs_account(SOL.mint)

    def test_open_accounts_are_persisted(self, tmp_path):
        path = tmp_path / "wallet.json"
        wallet = SimulatedWallet(path, initial={"USDC": Decimal("1")})
        wallet.apply_swap(USDC.mint, 1, JUP.mint, 5)
        assert not SimulatedWallet(path).needs_account(JUP.mint)


def _sol_usdc_account(ledger):
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    provider = _paper(
        wallet,
        [
            _quote(USDC.mint, USDC.ui_to_raw("10"), SOL.mint, SOL.ui_to_raw("0.1")),
            _quote(SOL.mint, SOL.ui_to_raw("0.1"), USDC.mint, USDC.ui_to_raw("11")),
        ],
    )
    gateway = TradeGateway(ledger, Policy(), False)
    return SpotAccount(
        SpotVenue(provider),
        USDC.pubkey,
        SOL.pubkey,
        gateway=gateway,
        account_id="paper:SOL-USDC",
    )


class TestLedgerRecordsCostsAndNetPnl:
    async def test_round_trip_is_recorded_and_restored(self):
        ledger = Ledger()
        account = _sol_usdc_account(ledger)
        buy = await account.buy(Decimal("100"), Decimal("0.1"))
        await account.sell(Decimal("110"), Decimal("0.1"))

        # custos vão junto com a ordem para o ledger (e voltam no restore)
        restored = order_from_json(ledger.list_intents()[-1].order_json or "")
        assert restored.costs == buy.costs
        assert restored.quote_amount == Decimal("10")

        report = ledger.pnl_totals("paper:SOL-USDC")
        assert report.quote_symbol == "USDC"
        assert report.closed == 1 and report.trades == 2
        assert report.gross_quote == Decimal("1")
        assert report.costs_sol == Decimal("0.00001")
        assert report.net_quote == Decimal("1") - Decimal("0.00105")
        assert report.fee_lamports == 10_000
        assert report.incomplete == 0 and report.unknown_costs == 0

        fresh = _sol_usdc_account(ledger)
        fresh.restore_from_ledger()
        assert fresh.book.net_quote == account.book.net_quote
        assert fresh.book.costs_sol == account.book.costs_sol
        assert "PNL líquido +0.998950 USDC" in fresh.book.summary()
        ledger.close()
