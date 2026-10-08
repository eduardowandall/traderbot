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

from trader.execution.models.errors import SwapRejectedError
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.mints import SOL_MINT

# saldo inicial quando a carteira de paper ainda não existe
DEFAULT_PAPER_BALANCES = {"USDC": Decimal("100"), "SOL": Decimal("0.5")}
# swaps aplicados guardados no arquivo (os últimos): resolver uma intenção de
# um processo morto no meio (A3). Um envio que pode ter saído do registro
# (`trimmed_at`) nunca é dado como não aplicado
APPLIED_LOG_SIZE = 200


class InsufficientFundsError(SwapRejectedError):
    """Saldo simulado insuficiente: recusado sem re-tentativa."""


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
        self._applied: list[dict] = []
        # posições de perp abertas (A8), por "<mercado>:<lado>": o venue
        # simulado (`perps.py`) é quem sabe o que guardar em cada uma
        self._perps: dict[str, dict] = {}
        # horário do swap mais novo que já saiu do registro (None: nenhum)
        self.trimmed_at: float | None = None
        if self.path and self.path.exists():
            self.reload()
        elif initial is not None:
            self.reset(initial)

    def reload(self) -> None:
        """Relê o arquivo: outro processo (outro bot) pode ter operado.

        Sob o lock: no Windows, um leitor com o arquivo aberto faz o
        `os.replace` de quem grava falhar.
        """
        with _file_lock(self.path if self.path and self.path.exists() else None):
            self._read()

    def _read(self) -> None:
        """Relê sem pegar o lock (quem chama já o tem)."""
        if self.path is None or not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._raw = {mint: int(raw) for mint, raw in data["balances"].items()}
        # arquivos antigos não têm a lista: quem tem saldo já tem conta
        self._open = set(data.get("open_accounts", self._funded()))
        self._perps = dict(data.get("perps", {}))
        log = data.get("applied", {})
        if isinstance(log, list):  # o primeiro formato do registro: só a lista
            self._applied, self.trimmed_at = log, None
        else:
            self._applied = list(log.get("swaps", []))
            self.trimmed_at = log.get("trimmed_at")

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
        self._perps = {}
        with _file_lock(self.path):  # um bot rodando não pode sobrescrever o reset
            self._commit()

    def _funded(self) -> list[str]:
        return [mint for mint, raw in self._raw.items() if raw]

    def needs_account(self, mint: str) -> bool:
        """True se receber `mint` exige criar uma conta de token (e pagar rent)."""
        return mint != SOL_MINT and mint not in self._open

    def applied(self, signature: str) -> dict | None:
        """O swap aplicado com esta assinatura, ou None.

        Procura na memória e, sem achar, relê o arquivo (outro processo).
        """
        found = self._find_applied(signature)
        if found is None:
            self.reload()
            found = self._find_applied(signature)
        return found

    def _find_applied(self, signature: str) -> dict | None:
        return next((a for a in self._applied if a["signature"] == signature), None)

    def raw_balance(self, mint: str) -> int:
        return self._raw.get(mint, 0)

    def balance(self, mint: str) -> Decimal:
        return SOLANA_MINTS.raw_to_ui(mint, self.raw_balance(mint))

    def balances(self) -> dict[str, Decimal]:
        """Saldos UI por mint (só os não-zero), relidos do arquivo."""
        self.reload()
        return {mint: self.balance(mint) for mint, raw in self._raw.items() if raw}

    def perps(self) -> dict[str, dict]:
        """As posições de perp abertas, relidas do arquivo (A8)."""
        self.reload()
        return dict(self._perps)

    def apply_perp(
        self,
        key: str,
        record: dict | None,
        moves: dict[str, int],
        signature: str | None = None,
    ) -> None:
        """Abre (`record`) ou fecha (None) a posição `key` e move os saldos.

        `moves`: mint -> raw (negativo debita). Como `apply_swap`: sob o lock,
        relê, confere o saldo, grava e só então muda a memória. Abrir uma
        posição que já existe, ou fechar uma que não existe, é recusado.
        """
        with _file_lock(self.path):
            self._read()
            if (key in self._perps) == (record is not None):
                state = "já existe" if record is not None else "não existe"
                raise InsufficientFundsError(f"posição perp {key} {state}")
            raw = self._moved(moves)
            perps = {k: v for k, v in self._perps.items() if k != key}
            if record is not None:
                perps[key] = record
            logged = None
            if signature is not None:
                logged = self._logged(signature, 0, 0, -moves.get(SOL_MINT, 0), 0)
            self._commit(raw=raw, perps=perps, logged=logged)

    def _moved(self, moves: dict[str, int]) -> dict[str, int]:
        raw = dict(self._raw)
        for mint, amount in moves.items():
            if raw.get(mint, 0) + amount < 0:
                symbol = SOLANA_MINTS[mint].symbol
                raise InsufficientFundsError(
                    f"Saldo simulado insuficiente de {symbol}: tem "
                    f"{self.balance(mint)}, precisa "
                    f"{SOLANA_MINTS.raw_to_ui(mint, -amount)}"
                )
            raw[mint] = raw.get(mint, 0) + amount
        return raw

    def apply_swap(
        self,
        input_mint: str,
        in_amount: int,
        output_mint: str,
        out_amount: int,
        fee_lamports: int = 0,
        rent_lamports: int = 0,
        signature: str | None = None,
    ) -> None:
        """Debita a entrada (+ taxa e rent em SOL) e credita a saída, atomicamente.

        Com arquivo: sob um lock, relê, calcula numa cópia, grava e só então
        muda a memória. Uma gravação que falha não aplica nada (a re-tentativa
        não aplica o swap duas vezes), e dois processos não se sobrescrevem.
        Com `signature`, o swap entra no registro dos aplicados.
        """
        with _file_lock(self.path):
            self._read()
            raw, opened = self._after_swap(
                input_mint,
                in_amount,
                output_mint,
                out_amount,
                fee_lamports + rent_lamports,
            )
            logged = None
            if signature is not None:
                logged = self._logged(
                    signature, in_amount, out_amount, fee_lamports, rent_lamports
                )
            self._commit(raw=raw, opened=opened, logged=logged)

    def close_account(
        self, mint: str, rent_lamports: int, fee_lamports: int, signature: str
    ) -> bool:
        """Fecha a conta vazia do token: o rent volta, menos a taxa (A15).

        False (nada muda) se não há conta aberta do token, se ela tem saldo,
        ou se é o SOL. Como `apply_swap`: sob o lock, relê, grava e só então
        muda a memória; entra no registro dos aplicados (rent negativo).
        """
        with _file_lock(self.path):
            self._read()
            if self.needs_account(mint) or mint == SOL_MINT or self.raw_balance(mint):
                return False
            raw = dict(self._raw)
            raw[SOL_MINT] = raw.get(SOL_MINT, 0) + rent_lamports - fee_lamports
            raw.pop(mint, None)
            logged = self._logged(
                signature, 0, rent_lamports, fee_lamports, -rent_lamports
            )
            self._commit(raw=raw, opened=self._open - {mint}, logged=logged)
        return True

    def _logged(
        self, signature: str, in_amount: int, out_amount: int, fee: int, rent: int
    ) -> tuple[list[dict], float | None]:
        """O registro dos aplicados com mais esta entrada (e o corte, se passou)."""
        entry = {
            "signature": signature,
            "in_amount": in_amount,
            "out_amount": out_amount,
            "fee": fee,
            "rent": rent,
            "at": time.time(),
        }
        applied, trimmed_at = [*self._applied, entry], self.trimmed_at
        if len(applied) > APPLIED_LOG_SIZE:
            # sem horário (o primeiro formato): agora, o lado seguro
            trimmed_at = applied[-APPLIED_LOG_SIZE - 1].get("at", time.time())
            applied = applied[-APPLIED_LOG_SIZE:]
        return applied, trimmed_at

    def _after_swap(
        self,
        input_mint: str,
        in_amount: int,
        output_mint: str,
        out_amount: int,
        sol: int,
    ) -> tuple[dict[str, int], set[str]]:
        moves = {input_mint: -in_amount}
        moves[SOL_MINT] = moves.get(SOL_MINT, 0) - sol
        raw = self._moved(moves)
        raw[output_mint] = raw.get(output_mint, 0) + out_amount
        return raw, self._open | {output_mint}

    def _commit(
        self,
        raw: dict[str, int] | None = None,
        opened: set[str] | None = None,
        perps: dict[str, dict] | None = None,
        logged: tuple[list[dict], float | None] | None = None,
    ) -> None:
        """Grava o estado novo (o que não vem, fica) e só então muda a memória.

        Quem chama tem o lock do arquivo: uma gravação que falha não muda nada.
        """
        raw = self._raw if raw is None else raw
        opened = self._open if opened is None else opened
        perps = self._perps if perps is None else perps
        applied, trimmed_at = logged or (self._applied, self.trimmed_at)
        self._write(raw, opened, applied, trimmed_at, perps)
        self._raw, self._open, self._perps = raw, opened, perps
        self._applied, self.trimmed_at = applied, trimmed_at

    def _write(
        self,
        raw: dict[str, int],
        opened: set[str],
        applied: list[dict],
        trimmed_at: float | None,
        perps: dict[str, dict],
    ) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # um .tmp por processo: dois escritores nunca dividem o arquivo
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "balances": raw,
                    "open_accounts": sorted(opened),
                    "applied": {"swaps": applied, "trimmed_at": trimmed_at},
                    "perps": perps,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        _replace(tmp, self.path)


REPLACE_ATTEMPTS = 20


def _replace(tmp: Path, path: Path) -> None:
    """`os.replace`, tentando de novo enquanto um leitor segura o arquivo.

    No Windows o destino aberto por outro processo (um leitor antigo, um
    antivírus) dá `PermissionError` por alguns milissegundos.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05)


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
