"""Inspeção da transação de swap antes de enviá-la (só modo real).

A Jupiter monta a transação; a chave só a assina se ela fizer o que a quote
diz:

1. **Programas:** toda instrução de topo chama um programa da lista
   (`ALLOWED_PROGRAMS`: Jupiter v6, Token, Token-2022, ATA, ComputeBudget,
   System; conferido ao vivo em transações reais). As rotas chamam as AMMs por
   dentro da Jupiter, então nada mais aparece no topo.
2. **Saldos:** numa simulação que devolve a carteira e todas as contas de
   token dela, nenhuma conta de token cai, exceto a do token de entrada, e no
   máximo o `inAmount` da quote; os lamports da carteira caem no máximo a
   entrada (se for SOL) mais `MAX_NATIVE_SPEND_LAMPORTS` (taxas, priority fee,
   rent de contas novas).

Uma falha é `TransactionInspectionError` (um `SwapRejectedError`: nada foi
enviado, e a mesma rota seria recusada de novo).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)

from trader.execution.models.errors import SwapRejectedError
from trader.shared.models.mints import SOL_MINT

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
TOKEN = str(TOKEN_PROGRAM_ID)
TOKEN_2022 = str(TOKEN_2022_PROGRAM_ID)
ATA = str(ASSOCIATED_TOKEN_PROGRAM_ID)
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
ALLOWED_PROGRAMS = frozenset(
    {JUPITER_V6, TOKEN, TOKEN_2022, ATA, COMPUTE_BUDGET, SYSTEM}
)
# taxas + priority fee + rent de algumas contas novas (~0.002 SOL cada)
MAX_NATIVE_SPEND_LAMPORTS = 10_000_000  # 0.01 SOL


class TransactionInspectionError(SwapRejectedError):
    """A transação não faz só o que a quote diz: não é assinada nem enviada."""


@dataclass(frozen=True)
class WalletState:
    """Lamports da carteira e as contas de token dela: conta -> (mint, raw)."""

    lamports: int
    tokens: dict[str, tuple[str, int]]

    def addresses(self, owner: Pubkey) -> list[Pubkey]:
        """O que pedir à simulação, nesta ordem: a carteira e as contas de token."""
        return [owner, *(Pubkey.from_string(a) for a in self.tokens)]


def token_amount(data: bytes) -> int:
    """O saldo raw de uma conta de token (Token e Token-2022: bytes 64-72)."""
    return int.from_bytes(data[64:72], "little") if len(data) >= 72 else 0


def state_after(before: WalletState, accounts: Sequence[Any] | None) -> WalletState:
    """O estado simulado, na ordem de `before.addresses()`.

    Sem as contas na resposta (um RPC que ignora o pedido) não dá para
    conferir: recusa.
    """
    if accounts is None or len(accounts) != len(before.tokens) + 1:
        raise TransactionInspectionError("a simulação não devolveu as contas")
    wallet, *tokens = accounts
    return WalletState(
        lamports=wallet.lamports if wallet is not None else 0,
        tokens={
            address: (mint, token_amount(bytes(acc.data)) if acc is not None else 0)
            for (address, (mint, _)), acc in zip(
                before.tokens.items(), tokens, strict=True
            )
        },
    )


def programs_of(tx: VersionedTransaction) -> set[str]:
    keys = tx.message.account_keys
    return {str(keys[ix.program_id_index]) for ix in tx.message.instructions}


def check_programs(
    tx: VersionedTransaction, extra: frozenset[str] = frozenset()
) -> None:
    """Só programas conhecidos; `extra`, os que quem chama também permite (o da
    Jupiter Perps, nos pedidos de perp, A11b)."""
    unknown = programs_of(tx) - ALLOWED_PROGRAMS - extra
    if unknown:
        raise TransactionInspectionError(
            f"transação chama programas fora da lista: {sorted(unknown)}"
        )


def check_balances(
    before: WalletState, after: WalletState, input_mint: str, in_amount: int
) -> None:
    """Só o token de entrada sai da carteira, até `in_amount` (e SOL p/ taxas)."""
    for account, (mint, amount) in before.tokens.items():
        _check_token(
            account, mint, amount - _post(after, account), input_mint, in_amount
        )
    allowance = MAX_NATIVE_SPEND_LAMPORTS + (in_amount if input_mint == SOL_MINT else 0)
    spent = before.lamports - after.lamports
    if spent > allowance:
        raise TransactionInspectionError(
            f"a carteira gastaria {spent} lamports (limite {allowance})"
        )


def _post(after: WalletState, account: str) -> int:
    # conta fechada pela transação: o saldo dela saiu
    return after.tokens.get(account, ("", 0))[1]


def _check_token(
    account: str, mint: str, spent: int, input_mint: str, in_amount: int
) -> None:
    if spent <= 0:
        return
    if mint != input_mint:
        raise TransactionInspectionError(
            f"a conta {account} ({mint}) perderia {spent}: não é o token de entrada"
        )
    if spent > in_amount:
        raise TransactionInspectionError(
            f"a conta {account} perderia {spent}, mais que o inAmount {in_amount}"
        )
