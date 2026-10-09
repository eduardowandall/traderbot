import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import httpx
import httpx2
from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized
from solana.rpc.models import TokenAccountOpts
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.rpc.responses import SendTransactionResp
from solders.signature import Signature
from solders.solders import (
    TOKEN_PROGRAM_ID,
    TransactionConfirmationStatus,
    VersionedTransaction,
)
from spl.token.constants import TOKEN_2022_PROGRAM_ID
from spl.token.instructions import get_associated_token_address
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from trader.execution.market.jupiter.logging_utils import logger_wrapper
from trader.execution.models.intent import TxOutcome
from trader.execution.trade.venues.jupiter.tx_inspection import (
    WalletState,
    token_amount,
)

TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError)


def is_transient(ex: BaseException) -> bool:
    """Falha passageira de leitura: rede, timeout, 429 ou 5xx.

    O solana-py embrulha os erros do `httpx2` em `SolanaRpcException`, com o
    original em `__cause__`.
    """
    cause = ex.__cause__ if isinstance(ex, SolanaRpcException) else ex
    if isinstance(cause, TRANSPORT_ERRORS):
        return True
    status = getattr(getattr(cause, "response", None), "status_code", None)
    return status == 429 or (status is not None and status >= 500)


# só para leituras: um envio nunca é re-tentado (poderia duplicar o swap)
_READ_RETRY = retry(
    wait=wait_exponential(multiplier=0.5, max=4),
    stop=stop_after_attempt(4),
    retry=retry_if_exception(is_transient),
    reraise=True,
)


class TransactionFailedError(Exception):
    """A transação foi processada pela rede, mas falhou (status.err)."""


def tx_outcome(status) -> TxOutcome:
    """O status de uma assinatura (`signature_status`), em Confirmed.

    A única leitura de um status (A12): a espera depois do envio e a
    resolução de uma intenção (A3) usam esta. Transações que falham também
    são confirmadas no bloco, então o erro vem antes. None (a rede ainda
    não a viu) e Processed são PENDING.
    """
    if status is None:
        return TxOutcome.PENDING
    if status.err is not None:
        return TxOutcome.FAILED
    if status.confirmation_status in (
        TransactionConfirmationStatus.Confirmed,
        TransactionConfirmationStatus.Finalized,
    ):
        return TxOutcome.LANDED
    return TxOutcome.PENDING


@dataclass(frozen=True)
class TokenAccount:
    """Uma conta de token da carteira, lida pelo endereço."""

    address: Pubkey
    program: Pubkey  # Token ou Token-2022 (o dono da conta)
    lamports: int  # o rent que fechar a conta devolve
    amount: int  # saldo raw


@dataclass(frozen=True)
class SignedTx:
    tx: VersionedTransaction
    # o blockhash dela vale até esta altura de bloco: depois, nunca entra
    last_valid_block_height: int

    @property
    def signature(self) -> str:
        return str(self.tx.signatures[0])


def resign(
    instructions: list[Instruction], keypair: Keypair, like: SignedTx
) -> SignedTx:
    """As instruções assinadas de novo com o blockhash de `like` (A12).

    Local, sem rede: o limite de unidades muda depois da simulação, e a
    transação final vale até a mesma altura de bloco.
    """
    blockhash = like.tx.message.recent_blockhash
    return _signed(instructions, keypair, blockhash, like.last_valid_block_height)


def _signed(
    instructions: list[Instruction],
    keypair: Keypair,
    blockhash,
    last_valid_block_height: int,
) -> SignedTx:
    """Uma transação nossa compilada e assinada pela carteira (que paga)."""
    message = MessageV0.try_compile(keypair.pubkey(), instructions, [], blockhash)
    tx = VersionedTransaction(message, [keypair])
    return SignedTx(tx, last_valid_block_height)


class AsyncRPCClient:
    def __init__(self, client=None):
        self.logger = logging.getLogger(self.__module__)
        if client:
            self.client = client
        else:
            rpc_url = os.getenv("HELIUS_RPC_URL")
            if not rpc_url:
                # `assert` some com `python -O`
                raise ValueError("HELIUS_RPC_URL não definida")
            # Confirmed, como a confirmação dos swaps: em Finalized (o padrão
            # do solana-py) o saldo logo após um swap ainda é o de antes
            self.client = AsyncClient(rpc_url, commitment=Confirmed)

    async def aclose(self) -> None:
        await self.client.close()

    @_READ_RETRY
    async def get_confirmed_transaction(
        self, signature: str, delays: tuple[float, ...] = (0.5, 1, 2, 4, 4)
    ):
        """Transação confirmada com `meta`, ou None se o RPC ainda não a tem.

        O índice de transações do RPC costuma atrasar em relação ao status da
        assinatura, por isso tenta de novo com espera crescente (~11s).
        """
        sig = Signature.from_string(signature)
        for delay in (0, *delays):
            await asyncio.sleep(delay)
            resp = await self.client.get_transaction(
                sig,
                encoding="json",
                commitment=Confirmed,  # o padrão do cliente é Finalized
                max_supported_transaction_version=0,
            )
            if resp.value is not None:
                return resp.value.transaction
        return None

    @logger_wrapper
    async def signature_status(self, signature, history: bool = False):
        """O status da assinatura, ou None se a rede não a viu (`tx_outcome`).

        `history`: busca no histórico, para resolver uma intenção depois (A3):
        o nó pode já ter esquecido o status recente. A espera depois de um
        envio não re-tenta aqui: quem espera já consulta de novo.
        """
        if isinstance(signature, str):
            signature = Signature.from_string(signature)
        result = await self.client.get_signature_statuses(
            [signature], search_transaction_history=history
        )
        return result.value[0]

    @logger_wrapper
    @_READ_RETRY
    async def finalized_block_height(self) -> int:
        """Altura de bloco finalizada: passou do `last_valid_block_height` de
        uma transação não vista, ela nunca mais entra."""
        return (await self.client.get_block_height(commitment=Finalized)).value

    @logger_wrapper
    async def sign_instructions(
        self, instructions: list[Instruction], keypair: Keypair
    ) -> SignedTx:
        """Uma transação nossa (não da Jupiter), paga e assinada pela carteira."""
        latest = await self.client.get_latest_blockhash()
        return _signed(
            instructions,
            keypair,
            latest.value.blockhash,
            latest.value.last_valid_block_height,
        )

    @logger_wrapper
    async def sign_transaction(
        self, tx: VersionedTransaction, keypair: Keypair
    ) -> SignedTx:
        """A transação da Jupiter com um blockhash novo, assinada pela carteira."""
        latest = await self.client.get_latest_blockhash()
        message = MessageV0(
            header=tx.message.header,
            account_keys=tx.message.account_keys,
            recent_blockhash=latest.value.blockhash,
            instructions=tx.message.instructions,
            address_table_lookups=tx.message.address_table_lookups,  # type: ignore
        )
        signed = VersionedTransaction(message, [keypair])
        return SignedTx(signed, latest.value.last_valid_block_height)

    @logger_wrapper
    async def simulate_transaction(
        self, new_tx: VersionedTransaction, addresses: list[Pubkey] | None = None
    ):
        """Simula; com `addresses`, a resposta traz essas contas como ficariam."""
        simulation = await self.client.simulate_transaction(
            new_tx, accounts_addresses=addresses
        )
        if simulation.value.err:
            raise Exception(f"Erro ao simular transação: {str(simulation.value.err)}")
        return simulation

    @logger_wrapper
    @_READ_RETRY
    async def wallet_state(self, owner: Pubkey) -> WalletState:
        """Lamports e todas as contas de token da carteira (Token e Token-2022)."""
        info, accounts = await asyncio.gather(
            self.client.get_account_info(owner), self._token_accounts(owner)
        )
        tokens = {
            str(keyed.pubkey): (
                str(Pubkey(bytes(keyed.account.data)[0:32])),
                token_amount(bytes(keyed.account.data)),
            )
            for keyed in accounts
        }
        lamports = info.value.lamports if info.value is not None else 0
        return WalletState(lamports, tokens)

    async def _token_accounts(self, owner: Pubkey) -> list:
        """As contas de token da carteira, dos dois programas (Token e 2022)."""
        responses = await asyncio.gather(
            *(
                self.client.get_token_accounts_by_owner(
                    owner, TokenAccountOpts(program_id=program)
                )
                for program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
            )
        )
        return [keyed for resp in responses for keyed in resp.value]

    @logger_wrapper
    async def send_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        return await self.client.send_raw_transaction(bytes(new_tx))

    @logger_wrapper
    @_READ_RETRY
    async def get_lamports(self, pubkey: Pubkey) -> Decimal:
        resp = await self.client.get_account_info(pubkey)

        if resp.value:
            lamports = resp.value.lamports
            return Decimal(lamports)

        raise Exception(
            f"Não foi possivel obter o balanco da pubkey: {str(pubkey)}. {resp=}"
        )

    @logger_wrapper
    @_READ_RETRY
    async def get_account_balance(self, owner: Pubkey) -> dict[Pubkey, Decimal]:
        """Saldos raw por mint; uma leitura que falha levanta (nunca vira zero)."""
        balances: dict[Pubkey, Decimal] = {}
        for keyed in await self._token_accounts(owner):
            data = bytes(keyed.account.data)
            mint = Pubkey(data[0:32])
            # a mesma mint pode estar em mais de uma conta de token
            balances[mint] = balances.get(mint, Decimal(0)) + token_amount(data)
        return balances

    @logger_wrapper
    @_READ_RETRY
    async def associated_token_accounts(
        self, owner: Pubkey, mint: Pubkey
    ) -> list[TokenAccount]:
        """As contas associadas de `mint` da carteira (Token e Token-2022).

        Lidas pelo endereço (`getMultipleAccounts`), não pelo índice por dono
        de `getTokenAccountsByOwner`, que já voltou sem uma conta que existia
        (A6 F1). Uma conta que não existe não entra na lista.
        """
        programs = (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
        addresses = [get_associated_token_address(owner, mint, p) for p in programs]
        resp = await self.client.get_multiple_accounts(addresses)
        return [
            TokenAccount(
                address, program, info.lamports, token_amount(bytes(info.data))
            )
            for address, program, info in zip(
                addresses, programs, resp.value, strict=True
            )
            if info is not None and info.owner == program
        ]
