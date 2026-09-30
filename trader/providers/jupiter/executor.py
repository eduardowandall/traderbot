"""Onde uma quote vira fundos movidos: o `Executor` e a execução on-chain.

`AsyncJupiterProvider` cuida do que é comum a todo local de execução (quote,
teto de impacto de preço, re-tentativas, conversão de unidades) e delega a
execução a um `Executor`:

- `OnChainExecutor` (aqui): monta a transação na Jupiter, assina, simula,
  envia e confirma; lê saldos e custos na blockchain. Precisa da chave.
- `SimulatedExecutor` (`trader/paper/executor.py`): aplica a quote numa
  carteira simulada. Sem chave e sem RPC.

Regra que vale para qualquer executor: depois que a transação foi enviada,
qualquer falha vira `TransactionSubmittedError` (nunca re-tentada, para não
duplicar o swap).
"""

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Protocol

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.solders import SendTransactionResp
from solders.transaction import VersionedTransaction

from trader.models import SOLANA_MINTS, SwapResult
from trader.models.account_data import MintBalance
from trader.models.costs import BASE_FEE_LAMPORTS, ESTIMATED, TradeCosts
from trader.models.errors import TransactionSubmittedError
from trader.models.mints import SOL_MINT
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_rpc_client import (
    AsyncRPCClient,
    TransactionFailedError,
)
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.providers.jupiter.swap_costs import SwapLegs, parse_swap_costs

# SOL mínimo mantido na carteira para taxas de transação e rent
DEFAULT_SOL_FEE_RESERVE = Decimal("0.02")
CONFIRMATION_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class Executor(Protocol):
    # os saldos refletem as nossas próprias ordens? (real e paper: sim; dry
    # run: não, a carteira real não muda). Decide se vale reconciliar.
    balances_track_fills: bool
    # SOL que a conta deixa de gastar, reservado para taxas deste local
    native_fee_reserve: Decimal

    async def execute(
        self, input_mint: str, output_mint: str, quote: JupiterQuoteResponse
    ) -> SwapResult: ...

    async def balances(self) -> list[MintBalance]: ...

    async def fetch_costs(self, result: SwapResult) -> TradeCosts | None: ...

    async def aclose(self) -> None: ...


class OnChainExecutor:
    """Execução real (ou dry run: simula e nunca envia) com a chave da carteira."""

    native_fee_reserve = DEFAULT_SOL_FEE_RESERVE

    def __init__(
        self,
        keypair: Keypair,
        rpc_client: AsyncRPCClient | None = None,
        jupiter_client: AsyncJupiterClient | None = None,
        is_dryrun: bool = False,
    ):
        if keypair is None:
            raise ValueError("execução on-chain precisa da chave da carteira")
        self.keypair = keypair
        self.is_dryrun = is_dryrun
        self.rpc_client = rpc_client or AsyncRPCClient(is_dryrun=is_dryrun)
        # compartilhado com o provider (quem fecha é o provider)
        self.jupiter_client = jupiter_client or AsyncJupiterClient()
        self.balances_track_fills = not is_dryrun
        self.logger = logger
        self.logger.info(f"Execução on-chain com {is_dryrun=}")

    @property
    def pubkey(self) -> Pubkey:
        return self.keypair.pubkey()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.pubkey}, dry={self.is_dryrun})"

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

    async def execute(
        self, input_mint: str, output_mint: str, quote: JupiterQuoteResponse
    ) -> SwapResult:
        tx = await self._get_swap_transaction(quote)
        new_tx = await self._get_signed_transaction(tx)
        resp = await self._send_transaction_and_wait_for_confirmation(new_tx)
        # valores da quote; os efetivos (e os custos) vêm de `fetch_costs`,
        # chamado só depois que o ledger marcou a intenção como executada
        return SwapResult(
            signature=json.loads(resp.to_json())["result"],
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(quote.inAmount),
            out_amount=int(quote.outAmount),
            quote=quote,
            message=new_tx.message,
        )

    async def _get_swap_transaction(
        self, quote: JupiterQuoteResponse
    ) -> VersionedTransaction:
        return await self.jupiter_client.get_swap_transaction(quote, self.pubkey)

    async def _get_signed_transaction(
        self, tx: VersionedTransaction
    ) -> VersionedTransaction:
        return await self.rpc_client.sign_transaction(tx, self.keypair)

    async def _send_signed_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        # simulação: nada saiu ainda, uma falha aqui pode ser re-tentada
        await self.rpc_client.simulate_transaction(new_tx)
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
        """Uma consulta; erros transitórios de RPC contam como "ainda não"."""
        try:
            return await self.rpc_client.check_signature_is_confirmed(signature)
        except TransactionFailedError:
            raise
        except Exception as ex:
            self.logger.warning(f"Erro ao consultar confirmação: {ex}")
            return False

    async def _send_transaction_and_wait_for_confirmation(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        resp = await self._send_signed_transaction(new_tx)
        signature = resp.value
        try:
            await self._wait_for_confirmation(signature)
        except Exception as ex:
            raise TransactionSubmittedError(
                f"Transação {signature} enviada mas não confirmada: {ex}",
                signature=str(signature),
            ) from ex
        return resp

    async def fetch_costs(self, result: SwapResult) -> TradeCosts | None:
        """Custos lidos da transação confirmada (ou estimados, em dry run)."""
        if self.is_dryrun:
            return await self._estimated_costs(result)
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

    async def _estimated_costs(self, result: SwapResult) -> TradeCosts | None:
        """Dry run: a transação não é enviada; a taxa é a que a rede cobraria."""
        if result.message is None:
            return None
        fee = await self.rpc_client.get_fee_for_message(result.message)
        if fee is None:
            return None
        signatures = result.message.header.num_required_signatures
        return TradeCosts(
            source=ESTIMATED,
            fee_lamports=fee,
            priority_fee_lamports=max(fee - BASE_FEE_LAMPORTS * signatures, 0),
            actual_in_amount=result.in_amount,
            actual_out_amount=result.out_amount,
        )

    async def aclose(self) -> None:
        # só o RPC é exclusivo do executor; o cliente Jupiter é do provider
        await self.rpc_client.aclose()


def _signature_of(tx: VersionedTransaction) -> str | None:
    """A assinatura da transação assinada (conhecida antes do envio)."""
    try:
        return str(tx.signatures[0])
    except Exception:
        return None
