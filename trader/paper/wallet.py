"""Carteira simulada para paper trading e backtest.

Guarda saldos em unidades raw (inteiros, como on-chain) por mint. Com `path`,
persiste em JSON (escrita atômica) para sobreviver a reinícios junto com o
ledger do modo `paper`; sem `path`, fica só em memória (backtest).
"""

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from trader.models import SOLANA_MINTS
from trader.models.errors import SwapRejectedError
from trader.models.mints import SOL_MINT

# saldo inicial quando a carteira de paper ainda não existe
DEFAULT_PAPER_BALANCES = {"USDC": Decimal("100"), "SOL": Decimal("0.5")}


class InsufficientFundsError(SwapRejectedError):
    """Saldo simulado insuficiente: recusado sem re-tentativa."""


def parse_balances(spec: str) -> dict[str, Decimal]:
    """'USDC=100 SOL=0.5' -> {'USDC': Decimal('100'), 'SOL': Decimal('0.5')}"""
    balances: dict[str, Decimal] = {}
    for item in spec.split():
        symbol, sep, amount = item.partition("=")
        if not sep:
            raise ValueError(f"saldo inválido {item!r}; use SIMBOLO=quantidade")
        SOLANA_MINTS.get_by_symbol(symbol)  # valida
        value = Decimal(amount)
        if not value.is_finite() or value < 0:
            raise ValueError(f"quantidade inválida para {symbol}: {amount!r}")
        balances[symbol] = value
    return balances


class SimulatedWallet:
    def __init__(
        self,
        path: str | Path | None = None,
        initial: dict[str, Decimal] | None = None,
    ):
        self.path = Path(path) if path else None
        self._raw: dict[str, int] = {}
        # contas de token já abertas: a primeira vez que a carteira recebe um
        # token, a conta é criada e paga rent (como on-chain)
        self._open: set[str] = set()
        if self.path and self.path.exists():
            self.reload()
        elif initial is not None:
            self.reset(initial)

    def reload(self) -> None:
        """Relê o arquivo: outro processo (CLI, outro bot) pode ter operado."""
        if self.path is None or not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._raw = {mint: int(raw) for mint, raw in data["balances"].items()}
        # arquivos antigos não têm a lista: quem tem saldo já tem conta
        self._open = set(data.get("open_accounts", self._funded()))

    @property
    def is_empty(self) -> bool:
        return not any(self._raw.values())

    def reset(self, balances: dict[str, Decimal]) -> None:
        mints = {symbol: SOLANA_MINTS.get_by_symbol(symbol) for symbol in balances}
        self._raw = {
            mints[symbol].mint: mints[symbol].ui_to_raw(amount)
            for symbol, amount in balances.items()
        }
        self._open = set(self._funded())
        with _file_lock(self.path):  # um bot rodando não pode sobrescrever o reset
            self._save()

    def _funded(self) -> list[str]:
        return [mint for mint, raw in self._raw.items() if raw]

    def needs_account(self, mint: str) -> bool:
        """True se receber `mint` exige criar uma conta de token (e pagar rent)."""
        return mint != SOL_MINT and mint not in self._open

    def raw_balance(self, mint: str) -> int:
        return self._raw.get(mint, 0)

    def balance(self, mint: str) -> Decimal:
        return SOLANA_MINTS.raw_to_ui(mint, self.raw_balance(mint))

    def balances(self) -> dict[str, Decimal]:
        """Saldos UI por mint (só os não-zero), relidos do arquivo."""
        self.reload()
        return {mint: self.balance(mint) for mint, raw in self._raw.items() if raw}

    def apply_swap(
        self,
        input_mint: str,
        in_amount: int,
        output_mint: str,
        out_amount: int,
        fee_lamports: int = 0,
        rent_lamports: int = 0,
    ) -> None:
        """Debita a entrada (+ taxa e rent em SOL) e credita a saída, atomicamente.

        Com arquivo: sob um lock, relê, calcula numa cópia, grava e só então
        muda a memória. Uma gravação que falha não aplica nada (a re-tentativa
        não aplica o swap duas vezes), e dois processos não se sobrescrevem.
        """
        with _file_lock(self.path):
            self.reload()
            raw, opened = self._after_swap(
                input_mint,
                in_amount,
                output_mint,
                out_amount,
                fee_lamports + rent_lamports,
            )
            self._write(raw, opened)
            self._raw, self._open = raw, opened

    def _after_swap(
        self,
        input_mint: str,
        in_amount: int,
        output_mint: str,
        out_amount: int,
        sol: int,
    ) -> tuple[dict[str, int], set[str]]:
        need = {input_mint: in_amount}
        need[SOL_MINT] = need.get(SOL_MINT, 0) + sol
        for mint, amount in need.items():
            if amount and self.raw_balance(mint) < amount:
                symbol = SOLANA_MINTS[mint].symbol
                raise InsufficientFundsError(
                    f"Saldo simulado insuficiente de {symbol}: "
                    f"tem {self.balance(mint)}, precisa "
                    f"{SOLANA_MINTS.raw_to_ui(mint, amount)}"
                )
        raw = dict(self._raw)
        for mint, amount in need.items():
            raw[mint] = raw.get(mint, 0) - amount
        raw[output_mint] = raw.get(output_mint, 0) + out_amount
        return raw, self._open | {output_mint}

    def _save(self) -> None:
        self._write(self._raw, self._open)

    def _write(self, raw: dict[str, int], opened: set[str]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # um .tmp por processo: dois escritores nunca dividem o arquivo
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {"balances": raw, "open_accounts": sorted(opened)},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


LOCK_TIMEOUT_SECONDS = 5.0
STALE_LOCK_SECONDS = 30.0


@contextmanager
def _file_lock(path: Path | None) -> Iterator[None]:
    """Lock entre processos por arquivo (`O_EXCL`), para ler-alterar-gravar."""
    if path is None:
        yield
        return
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    _acquire(lock)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _acquire(lock: Path) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
        except FileExistsError:
            _break_if_stale(lock)
            if time.monotonic() > deadline:
                raise TimeoutError(f"carteira paper ocupada ({lock})") from None
            time.sleep(0.05)


def _break_if_stale(lock: Path) -> None:
    # um processo morto no meio da gravação não pode travar a carteira
    try:
        seen = lock.stat().st_mtime
        if time.time() - seen <= STALE_LOCK_SECONDS:
            return
        # confere de novo antes de apagar: outro processo pode ter acabado de
        # pegar um lock novo (mtime diferente) entre a leitura e o unlink
        if lock.stat().st_mtime == seen:
            lock.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
