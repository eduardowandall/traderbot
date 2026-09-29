"""Carteira simulada para paper trading e backtest.

Guarda saldos em unidades raw (inteiros, como on-chain) por mint. Com `path`,
persiste em JSON (escrita atômica) para sobreviver a reinícios junto com o
ledger do modo `paper`; sem `path`, fica só em memória (backtest).
"""

import json
import os
from decimal import Decimal
from pathlib import Path

from trader.models import SOLANA_MINTS
from trader.models.errors import SwapRejectedError

SOL_MINT = SOLANA_MINTS.get_by_symbol("SOL").mint

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
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._raw = {mint: int(raw) for mint, raw in data["balances"].items()}
            # arquivos antigos não têm a lista: quem tem saldo já tem conta
            self._open = set(data.get("open_accounts", self._funded()))
        elif initial is not None:
            self.reset(initial)

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
        """Saldos UI por mint (só os não-zero)."""
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
        """Debita a entrada (+ taxa e rent em SOL) e credita a saída, atomicamente."""
        need = {input_mint: in_amount}
        need[SOL_MINT] = need.get(SOL_MINT, 0) + fee_lamports + rent_lamports
        for mint, amount in need.items():
            if amount and self.raw_balance(mint) < amount:
                symbol = SOLANA_MINTS[mint].symbol
                raise InsufficientFundsError(
                    f"Saldo simulado insuficiente de {symbol}: "
                    f"tem {self.balance(mint)}, precisa "
                    f"{SOLANA_MINTS.raw_to_ui(mint, amount)}"
                )
        for mint, amount in need.items():
            self._raw[mint] = self.raw_balance(mint) - amount
        self._raw[output_mint] = self.raw_balance(output_mint) + out_amount
        self._open.add(output_mint)
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"balances": self._raw, "open_accounts": sorted(self._open)},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
