"""Onde uma quote vira fundos movidos: o `Executor` e a execução on-chain.

`AsyncJupiterProvider` cuida do que é comum a todo local de execução (quote,
teto de impacto de preço, re-tentativas, conversão de unidades) e delega a
execução a um `Executor`:

- `OnChainExecutor` (aqui): monta a transação na Jupiter, assina, simula,
  envia e confirma; lê saldos e custos na blockchain. Precisa da chave.
- `SimulatedExecutor` (`trader/execution/trade/venues/paper/executor.py`): aplica a quote numa
  carteira simulada. Sem chave e sem RPC.

Regra que vale para qualquer executor: depois que a transação foi enviada,
qualquer falha vira `TransactionSubmittedError` (nunca re-tentada, para não
duplicar o swap), exceto uma transação que a rede confirmou como falha
(`TransactionFailedOnChainError`): nada foi trocado, pode ser re-tentada.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from typing import Protocol

from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.solders import SendTransactionResp
from solders.transaction import VersionedTransaction
from spl.token.instructions import close_account
from spl.token.models import CloseAccountParams

from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.execution.market.jupiter.quote import JupiterQuoteResponse
from trader.execution.models.errors import (
    TransactionFailedOnChainError,
    TransactionSubmittedError,
)
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, TxOutcome, announce_send
from trader.execution.models.rent import RentRefund
from trader.execution.models.venue import DEFAULT_SOL_FEE_RESERVE, MintBalance
from trader.execution.trade.venues.jupiter.compute_budget import (
    ComputeBudget,
    budgeted,
    unit_limit,
    unit_price,
)
from trader.execution.trade.venues.jupiter.rpc import (
    AsyncRPCClient,
    SignedTx,
    TransactionFailedError,
    resign,
    tx_outcome,
)
from trader.execution.trade.venues.jupiter.swap_costs import SwapLegs, parse_swap_costs
from trader.execution.trade.venues.jupiter.tx_inspection import (
    check_balances,
    check_programs,
    state_after,
)
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import TradeCosts
from trader.shared.models.mints import SOL_MINT

CONFIRMATION_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class Executor(Protocol):
    # SOL que a conta deixa de gastar, reservado para taxas deste local
    native_fee_reserve: Decimal

    async def execute(
        self, input_mint: str, output_mint: str, quote: JupiterQuoteResponse
    ) -> ExecutionResult: ...

    async def balances(self) -> list[MintBalance]: ...

    # saldo de um token lido direto da conta dele (A6 F1), em unidades de UI
    async def token_balance(self, mint: str) -> Decimal: ...

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts | None: ...

    # `meta.fee` de uma transação que falhou na rede; None se não achou
    async def fetch_fee(self, signature: str) -> int | None: ...

    # o que aconteceu com um envio gravado (resolver intenções, A3)
    async def outcome(self, sent: SentTx) -> TxOutcome: ...

    # fecha a conta vazia do token (A15); `announce` grava o envio antes dele.
    # None: não há conta vazia para fechar
    async def close_token_account(
        self, mint: str, announce: Callable[[SentTx], None]
    ) -> RentRefund | None: ...

    async def aclose(self) -> None: ...


class OnChainExecutor:
    """Execução real com a chave da carteira."""

    native_fee_reserve = DEFAULT_SOL_FEE_RESERVE

    def __init__(
        self,
        keypair: Keypair,
        rpc_client: AsyncRPCClient | None,  # None: o do ambiente
        jupiter_client: AsyncJupiterClient | None,
        # teto da priority fee (`max_priority_fee_lamports` da política)
        max_priority_fee_lamports: int,
    ):
        if keypair is None:
            raise ValueError("execução on-chain precisa da chave da carteira")
        self.keypair = keypair
        self.max_priority_fee_lamports = max_priority_fee_lamports
        self.rpc_client = rpc_client or AsyncRPCClient()
        # compartilhado com o provider (quem fecha é o provider)
        self.jupiter_client = jupiter_client or AsyncJupiterClient()
        self.logger = logger

    @property
    def pubkey(self) -> Pubkey:
        return self.keypair.pubkey()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.pubkey})"

    async def balances(self) -> list[MintBalance]:
        sol = SOLANA_MINTS[SOL_MINT]
        lamports = await self.rpc_client.get_lamports(self.pubkey)
        balances = [MintBalance(available=sol.raw_to_ui(lamports), mint=sol.pubkey)]
        tokens = await self.rpc_client.get_account_balance(self.pubkey)
        for mint, amount in tokens.items():
            info = SOLANA_MINTS.get(mint)
            if info:
                balances.append(
                    MintBalance(available=info.raw_to_ui(amount), mint=mint)
                )
        return balances

    async def token_balance(self, mint: str) -> Decimal:
        accounts = await self.rpc_client.associated_token_accounts(
            self.pubkey, Pubkey.from_string(mint)
        )
        return SOLANA_MINTS.raw_to_ui(mint, sum(a.amount for a in accounts))

    async def execute(
        self, input_mint: str, output_mint: str, quote: JupiterQuoteResponse
    ) -> ExecutionResult:
        tx = await self._get_swap_transaction(quote)
        check_programs(tx)  # antes de assinar: só programas conhecidos
        signed = await self._get_signed_transaction(tx)
        sent = SentTx(
            signed.signature,
            input_mint,
            output_mint,
            int(quote.inAmount),
            int(quote.outAmount),
            signed.last_valid_block_height,
        )
        # no ledger antes de enviar: um processo morto daqui em diante deixa a
        # assinatura para resolver a intenção (A3); sem quem grave, não envia
        await self._inspect(signed, quote.inputMint, int(quote.inAmount))
        resp = await self._announce_and_send(
            signed, sent, partial(announce_send, required=True)
        )
        # valores da quote; os efetivos (e os custos) vêm de `fetch_costs`,
        # chamado só depois que o ledger marcou a intenção como executada
        return ExecutionResult(
            signature=json.loads(resp.to_json())["result"],
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(quote.inAmount),
            out_amount=int(quote.outAmount),
            quote=quote,
        )

    async def _get_swap_transaction(
        self, quote: JupiterQuoteResponse
    ) -> VersionedTransaction:
        return await self.jupiter_client.get_swap_transaction(
            quote, self.pubkey, self.max_priority_fee_lamports
        )

    async def _get_signed_transaction(self, tx: VersionedTransaction) -> SignedTx:
        return await self.rpc_client.sign_transaction(tx, self.keypair)

    async def _inspect(
        self, signed: SignedTx, spend_mint: str, spend_max: int
    ) -> int | None:
        """Simula com a carteira: só `spend_mint` sai, até `spend_max` (e SOL
        para taxas), senão `TransactionInspectionError`.

        O caminho de toda transação nossa, antes de `_announce_and_send`. Uma
        simulação que falha (nada saiu) pode ser re-tentada. Devolve as
        unidades de computação que a simulação usou (None: não informou).
        """
        before = await self.rpc_client.wallet_state(self.pubkey)
        simulation = await self.rpc_client.simulate_transaction(
            signed.tx, before.addresses(self.pubkey)
        )
        after = state_after(before, simulation.value.accounts)
        check_balances(before, after, spend_mint, spend_max)
        return getattr(simulation.value, "units_consumed", None)

    async def _announce_and_send(
        self, signed: SignedTx, sent: SentTx, announce: Callable[[SentTx], None]
    ) -> SendTransactionResp:
        """Grava o envio e envia. `announce` grava antes (se falha, nada é
        enviado). Depois do envio, uma falha é `TransactionFailedOnChainError`
        (nada trocado) ou `TransactionSubmittedError` (resolver depois)."""
        announce(sent)
        return await self._send_transaction_and_wait_for_confirmation(signed.tx)

    async def _send_signed_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        try:
            return await self.rpc_client.send_transaction(new_tx)
        except Exception as ex:
            # o nó pode ter aceitado a transação antes de o erro chegar (ex:
            # timeout na resposta): conta como enviada e nunca é re-tentada,
            # senão um novo envio poderia duplicar o swap
            signature = _signature_of(new_tx)
            raise TransactionSubmittedError(
                f"Envio da transação {signature} sem resposta: {ex}",
                signature=signature,
            ) from ex

    async def _wait_for_confirmation(
        self, signature, timeout=CONFIRMATION_TIMEOUT_SECONDS
    ):
        start = time.time()
        while True:
            if await self._poll_confirmation(signature):
                return True
            if time.time() - start > timeout:
                raise TimeoutError("Transação não foi confirmada a tempo.")
            await asyncio.sleep(1.0)

    async def _poll_confirmation(self, signature) -> bool:
        """Uma consulta; erros transitórios de RPC contam como "ainda não".

        Confirmada com erro: `TransactionFailedError` (nada foi trocado).
        """
        try:
            status = await self.rpc_client.signature_status(signature)
        except Exception as ex:
            self.logger.warning(f"Erro ao consultar confirmação: {ex}")
            return False
        outcome = tx_outcome(status)
        if outcome == TxOutcome.FAILED:
            assert status is not None  # FAILED só vem de um status com erro
            raise TransactionFailedError(f"Transação falhou: {status.err}")
        return outcome == TxOutcome.LANDED

    async def _send_transaction_and_wait_for_confirmation(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        resp = await self._send_signed_transaction(new_tx)
        signature = resp.value
        try:
            await self._wait_for_confirmation(signature)
        except TransactionFailedError as ex:
            # confirmada com erro: a rede garante que nada foi trocado
            raise TransactionFailedOnChainError(
                f"Transação {signature} falhou na rede: {ex}", signature=str(signature)
            ) from ex
        except Exception as ex:
            raise TransactionSubmittedError(
                f"Transação {signature} enviada mas não confirmada: {ex}",
                signature=str(signature),
            ) from ex
        return resp

    async def close_token_account(
        self, mint: str, announce: Callable[[SentTx], None]
    ) -> RentRefund | None:
        """Fecha a conta associada do token, se existe e está vazia (A15).

        Uma instrução `CloseAccount` (o rent volta para a carteira, que também
        é a autoridade), só com a taxa base: nada urgente. Antes de enviar, a
        mesma checagem de programas dos swaps e uma simulação. Depois do
        envio, as falhas são as dos swaps: `TransactionFailedOnChainError`
        (a conta continua aberta) ou `TransactionSubmittedError` (resolver).
        """
        accounts = await self.rpc_client.associated_token_accounts(
            self.pubkey, Pubkey.from_string(mint)
        )
        account = next((a for a in accounts if a.amount == 0), None)
        if account is None:
            return None
        instruction = close_account(
            CloseAccountParams(
                program_id=account.program,
                account=account.address,
                dest=self.pubkey,
                owner=self.pubkey,
            )
        )

        def sent(signed: SignedTx) -> SentTx:
            return SentTx(
                signed.signature,
                mint,
                SOL_MINT,
                0,
                account.lamports,
                signed.last_valid_block_height,
            )

        # nenhum token sai: só o rent volta (e a taxa sai em SOL)
        signed = await self.send_instructions(
            [instruction], sent, announce, SOL_MINT, 0
        )
        return RentRefund(signed.signature, mint, account.lamports)

    async def send_instructions(
        self,
        instructions: list[Instruction],
        sent: Callable[[SignedTx], SentTx],
        announce: Callable[[SentTx], None],
        spend_mint: str,
        spend_max: int,
        extra_programs: frozenset[str] = frozenset(),
        budget: ComputeBudget | None = None,
    ) -> SignedTx:
        """Uma transação nossa (não da Jupiter): assina, confere, envia.

        Só programas conhecidos (mais `extra_programs`), depois a mesma
        simulação e o mesmo envio dos swaps. Com `budget` (A12), a primeira
        assinatura vai com o limite máximo, e a simulação (uma só) dá as
        unidades: a transação é assinada de novo, localmente, com o limite
        justo e o preço por unidade do `budget`. Sem ele, só a taxa base.
        """
        first = self._budgeted(instructions, budget, None)
        signed = await self.rpc_client.sign_instructions(first, self.keypair)
        check_programs(signed.tx, extra_programs)
        units = await self._inspect(signed, spend_mint, spend_max)
        if budget is not None:
            final = self._budgeted(instructions, budget, units)
            signed = resign(final, self.keypair, signed)
        await self._announce_and_send(signed, sent(signed), announce)
        return signed

    def _budgeted(
        self,
        instructions: list[Instruction],
        budget: ComputeBudget | None,
        units: int | None,
    ) -> list[Instruction]:
        if budget is None:
            return instructions
        limit = unit_limit(units)
        price = unit_price(self.max_priority_fee_lamports, limit, budget.recent)
        return budgeted(instructions, limit, price)

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts | None:
        """Custos lidos da transação confirmada."""
        tx = await self.rpc_client.get_confirmed_transaction(result.signature)
        if tx is None:
            return None
        ui_tx = tx.transaction
        return parse_swap_costs(
            tx.meta,
            list(getattr(getattr(ui_tx, "message", None), "account_keys", [])),
            len(getattr(ui_tx, "signatures", [])),
            self.pubkey,
            SwapLegs(
                result.input_mint,
                result.output_mint,
                result.in_amount,
                result.out_amount,
            ),
        )

    async def fetch_fee(self, signature: str) -> int | None:
        """A taxa paga por uma transação (inclusive uma que falhou)."""
        tx = await self.rpc_client.get_confirmed_transaction(signature)
        return None if tx is None or tx.meta is None else int(tx.meta.fee)

    async def outcome(self, sent: SentTx) -> TxOutcome:
        """O status da assinatura na rede, em Confirmed.

        Sem status: expirou se a altura finalizada passou do
        `last_valid_block_height` (nunca mais entra), senão ainda pendente.
        """
        status = await self.rpc_client.signature_status(sent.signature, history=True)
        if status is not None:
            return tx_outcome(status)
        if sent.last_valid_block_height is None:
            return TxOutcome.PENDING
        height = await self.rpc_client.finalized_block_height()
        if height > sent.last_valid_block_height:
            return TxOutcome.EXPIRED
        return TxOutcome.PENDING

    async def aclose(self) -> None:
        # só o RPC é exclusivo do executor; o cliente Jupiter é do provider
        await self.rpc_client.aclose()


def _signature_of(tx: VersionedTransaction) -> str | None:
    """A assinatura da transação assinada (conhecida antes do envio)."""
    try:
        return str(tx.signatures[0])
    except Exception:
        return None
