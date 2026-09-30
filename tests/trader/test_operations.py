"""R9: operações (carteira paper entre processos, resolve seguro, logs)."""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from factories import make_intent
from typer.testing import CliRunner

import main as main_module
from trader.ledger import Ledger, ledger_path
from trader.models import SOLANA_MINTS
from trader.models.intent import IntentStatus, PolicyDecision
from trader.paper import SimulatedWallet
from trader.paths import logs_dir

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")


def _swap(wallet, usdc="10"):
    wallet.apply_swap(USDC.mint, USDC.ui_to_raw(usdc), SOL.mint, SOL.ui_to_raw("0.1"))


class TestPaperWallet:
    def test_two_processes_never_overwrite_each_other(self, tmp_path):
        path = tmp_path / "wallet.json"
        SimulatedWallet(path, initial={"USDC": Decimal("100")})
        bot, cli = SimulatedWallet(path), SimulatedWallet(path)

        _swap(bot)
        _swap(cli)  # antes: partia do saldo antigo e apagava a troca do bot

        assert SimulatedWallet(path).balance(USDC.mint) == Decimal("80")
        assert SimulatedWallet(path).balance(SOL.mint) == Decimal("0.2")

    def test_a_failed_save_changes_nothing(self, tmp_path):
        path = tmp_path / "wallet.json"
        wallet = SimulatedWallet(path, initial={"USDC": Decimal("100")})

        with (
            mock.patch("os.replace", side_effect=PermissionError("arquivo em uso")),
            pytest.raises(PermissionError),
        ):
            _swap(wallet)

        # a re-tentativa não pode aplicar o swap duas vezes
        assert wallet.balance(USDC.mint) == Decimal("100")
        assert not (path.with_name(path.name + ".lock")).exists()

    def test_a_stale_lock_does_not_block_forever(self, tmp_path, monkeypatch):
        path = tmp_path / "wallet.json"
        wallet = SimulatedWallet(path, initial={"USDC": Decimal("100")})
        lock = path.with_name(path.name + ".lock")
        lock.write_text("", encoding="utf-8")
        monkeypatch.setattr("trader.paper.wallet.STALE_LOCK_SECONDS", -1)

        _swap(wallet)

        assert wallet.balance(USDC.mint) == Decimal("90")


class TestResolveChecksFirst:
    def test_real_resolve_without_rpc_writes_nothing(self, monkeypatch):
        monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
        intent = make_intent(account="real:SOL-USDC")
        with Ledger(ledger_path("real")) as ledger:
            ledger.record_intent(intent, PolicyDecision(True))
            ledger.mark_unconfirmed(intent.intent_id, "timeout", signature="sig")

        result = CliRunner().invoke(
            main_module.app,
            ["ledger", "resolve", "real", intent.intent_id, "failed", "--note", "x"],
        )

        assert result.exit_code != 0
        assert "HELIUS_RPC_URL" in result.output
        with Ledger(ledger_path("real")) as ledger:
            record = ledger.get(intent.intent_id)
        assert record and record.status == IntentStatus.UNCONFIRMED


def test_logs_follow_the_paths_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_LOG_DIR", "custom-logs")
    assert logs_dir().name == "custom-logs"
    assert logs_dir().is_absolute()  # relativo à raiz do projeto, nunca ao cwd


class TestResolveReadsFirst:
    def test_a_failed_chain_read_leaves_the_intent_unresolved(self, monkeypatch):
        monkeypatch.setenv("HELIUS_RPC_URL", "https://rpc.test")
        intent = make_intent(account="real:SOL-USDC")
        with Ledger(ledger_path("real")) as ledger:
            ledger.record_intent(intent, PolicyDecision(True))
            ledger.mark_unconfirmed(intent.intent_id, "timeout", signature="sig")
        rpc = mock.AsyncMock()
        rpc.get_confirmed_transaction.side_effect = OSError("429 do Helius")

        with mock.patch("trader.cli.ledger.AsyncRPCClient", return_value=rpc):
            result = CliRunner().invoke(
                main_module.app,
                [
                    "ledger",
                    "resolve",
                    "real",
                    intent.intent_id,
                    "failed",
                    "--note",
                    "x",
                ],
            )

        assert result.exit_code != 0
        with Ledger(ledger_path("real")) as ledger:
            record = ledger.get(intent.intent_id)
        # nada foi gravado: o comando pode ser repetido quando o RPC voltar
        assert record and record.status == IntentStatus.UNCONFIRMED


async def test_no_new_swap_attempt_after_the_deadline(monkeypatch):
    from solders.keypair import Keypair

    from trader.providers.jupiter import async_jupiter_svc as svc
    from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
    from trader.providers.jupiter.async_rpc_client import AsyncRPCClient

    provider = svc.AsyncJupiterProvider.on_chain(
        Keypair(),
        rpc_client=mock.AsyncMock(spec=AsyncRPCClient),
        jupiter_client=mock.AsyncMock(spec=AsyncJupiterClient),
    )
    provider._do_swap = mock.AsyncMock(side_effect=OSError("quote lenta"))
    clock = iter([0.0, svc.SWAP_DEADLINE_SECONDS + 1, svc.SWAP_DEADLINE_SECONDS + 2])
    # só o relógio do provider (o event loop usa o `time.monotonic` real)
    monkeypatch.setattr(svc, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    with pytest.raises(RuntimeError):
        await provider._do_swap_with_retry("a", "b", 1000)

    provider._do_swap.assert_awaited_once()  # o prazo acabou: sem 2a tentativa


def test_a_reset_waits_for_the_wallet_lock(tmp_path, monkeypatch):
    path = tmp_path / "wallet.json"
    wallet = SimulatedWallet(path, initial={"USDC": Decimal("100")})
    lock = path.with_name(path.name + ".lock")
    lock.write_text("", encoding="utf-8")  # outro processo gravando
    monkeypatch.setattr("trader.paper.wallet.LOCK_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(TimeoutError):
        wallet.reset({"USDC": Decimal("5")})

    assert SimulatedWallet(path).balance(USDC.mint) == Decimal("100")
