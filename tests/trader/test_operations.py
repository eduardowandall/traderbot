"""R9: operações (carteira paper entre processos, logs, prazo de swap)."""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest

from trader.models import SOLANA_MINTS
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


def test_logs_follow_the_paths_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_LOG_DIR", "custom-logs")
    assert logs_dir().name == "custom-logs"
    assert logs_dir().is_absolute()  # relativo à raiz do projeto, nunca ao cwd


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
    # a leitura também espera o lock (no Windows um leitor quebra o replace)
    with pytest.raises(TimeoutError):
        wallet.reload()

    lock.unlink()
    assert SimulatedWallet(path).balance(USDC.mint) == Decimal("100")


def test_a_busy_destination_is_replaced_after_a_retry(tmp_path, monkeypatch):
    import os

    wallet = SimulatedWallet(tmp_path / "w.json", initial={"USDC": Decimal("100")})
    real_replace = os.replace
    calls = []

    def busy_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("em uso por outro processo")
        real_replace(src, dst)

    monkeypatch.setattr("trader.paper.wallet.os.replace", busy_once)
    monkeypatch.setattr("trader.paper.wallet.time.sleep", lambda s: None)
    _swap(wallet)

    assert len(calls) == 2
    assert SimulatedWallet(tmp_path / "w.json").balance(USDC.mint) == Decimal("90")
