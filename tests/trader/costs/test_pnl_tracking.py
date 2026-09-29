import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from typer.testing import CliRunner

import main as main_module
from trader.async_account import AsyncAccount
from trader.execution import KillSwitch, TradeGateway
from trader.ledger import Ledger, ledger_path, order_from_json
from trader.ledger.ledger import _SCHEMA
from trader.models import SOLANA_MINTS
from trader.models.intent import IntentSide, IntentStatus, PolicyDecision, TradeIntent
from trader.paper import PaperJupiterProvider, SimulatedWallet
from trader.paths import data_dir
from trader.policy import Policy
from trader.providers import JupiterQuoteResponse
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
RENT = 2_039_280


def _paper(wallet, quotes):
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_quote = AsyncMock(side_effect=quotes)
    return PaperJupiterProvider(wallet, jupiter_client=client)


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
    gateway = TradeGateway(ledger, Policy(), KillSwitch("halt-file"), False)
    return AsyncAccount(
        provider, USDC.pubkey, SOL.pubkey, gateway=gateway, account_id="paper:SOL-USDC"
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

        report = ledger.pnl_report()["paper:SOL-USDC"]
        assert report.quote_symbol == "USDC"
        assert report.closed == 1 and report.trades == 2
        assert report.gross_quote == Decimal("1")
        assert report.costs_sol == Decimal("0.00001")
        assert report.net_quote == Decimal("1") - Decimal("0.00105")
        assert report.fee_lamports == 10_000
        assert report.incomplete == 0 and report.unknown_costs == 0

        fresh = _sol_usdc_account(ledger)
        fresh.restore_from_ledger()
        assert fresh.total_net_quote == account.total_net_quote
        assert fresh.total_costs_sol == account.total_costs_sol
        assert "PNL líquido +0.998950 USDC" in fresh.pnl_summary()
        assert ledger.verify_chain() is None
        ledger.close()

    def test_failed_transaction_fee_is_counted(self):
        ledger = Ledger()
        intent = TradeIntent(
            "t", "real:SOL-USDC", IntentSide.BUY, USDC.mint, SOL.mint, Decimal("1")
        )
        ledger.record_intent(intent, PolicyDecision(True))
        ledger.mark_unconfirmed(intent.intent_id, "timeout", signature="sig-x")
        assert ledger.get(intent.intent_id).signature == "sig-x"  # type: ignore[union-attr]
        ledger.resolve(intent.intent_id, IntentStatus.FAILED, "explorer")
        ledger.record_failed_fee(intent.intent_id, 5000)

        report = ledger.pnl_report()["real:SOL-USDC"]
        assert report.failed_fee_lamports == 5000
        assert report.paid_sol == Decimal("0.000005")
        ledger.close()


def test_migrates_old_ledger_files(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)  # esquema da primeira versão, sem colunas de custo
    conn.close()

    with Ledger(path) as ledger:
        columns = {r["name"] for r in ledger.conn.execute("PRAGMA table_info(intents)")}
        assert {"fee_lamports", "net_pnl_quote", "pnl_complete"} <= columns
    with Ledger(path):  # migrar de novo é inofensivo
        pass


def test_pnl_command_reports_native_and_usd():
    with Ledger(ledger_path("paper")) as ledger:
        import asyncio

        account = _sol_usdc_account(ledger)
        asyncio.run(account.buy(Decimal("100"), Decimal("0.1")))
        asyncio.run(account.sell(Decimal("110"), Decimal("0.1")))

    result = CliRunner().invoke(main_module.app, ["pnl", "paper"])

    assert result.exit_code == 0, result.output
    assert "paper:SOL-USDC: 2 pernas, 1 posições fechadas" in result.stdout
    assert "líquido +0.998950 USDC" in result.stdout
    assert "TOTAL líquido: +0.998950 USDC" in result.stdout

    listed = CliRunner().invoke(main_module.app, ["ledger", "list", "paper"])
    assert "custos=0.000005000SOL[simulated]" in listed.stdout
    assert "líquido=+0.998950USDC" in listed.stdout


def test_pnl_command_without_trades():
    result = CliRunner().invoke(main_module.app, ["pnl", "paper"])
    assert "Nenhum trade executado" in result.stdout


def test_resolve_failed_backfills_fee():
    with Ledger(ledger_path("real")) as ledger:
        intent = TradeIntent(
            "t", "real:SOL-USDC", IntentSide.BUY, USDC.mint, SOL.mint, Decimal("1")
        )
        ledger.record_intent(intent, PolicyDecision(True))
        ledger.mark_unconfirmed(intent.intent_id, "timeout", signature="sig-y")

    rpc = mock.Mock()
    rpc.get_confirmed_transaction = AsyncMock(
        return_value=SimpleNamespace(meta=SimpleNamespace(fee=7000))
    )
    rpc.aclose = AsyncMock()
    with mock.patch("main.AsyncRPCClient", return_value=rpc):
        result = CliRunner().invoke(
            main_module.app,
            ["ledger", "resolve", "real", intent.intent_id, "failed", "--note", "x"],
        )

    assert result.exit_code == 0, result.output
    rpc.get_confirmed_transaction.assert_awaited_once_with("sig-y")
    with Ledger(ledger_path("real")) as ledger:
        assert ledger.pnl_report()["real:SOL-USDC"].failed_fee_lamports == 7000
    assert data_dir().exists()
