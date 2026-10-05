import asyncio
import logging
import os
from decimal import Decimal

import httpx
import httpx2
from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.models import TokenAccountOpts
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.rpc.responses import SendTransactionResp
from solders.signature import Signature
from solders.solders import (
    TOKEN_PROGRAM_ID,
    TransactionConfirmationStatus,
    VersionedTransaction,
)
from spl.token.constants import TOKEN_2022_PROGRAM_ID
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from trader.execution.venues.jupiter.tx_inspection import WalletState, token_amount
from trader.shared.market.jupiter.logging_utils import logger_wrapper

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
        self._client_connected = False

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
    async def is_connected(self):
        if self._client_connected:
            return True
        self._client_connected = await self.client.is_connected()
        return self._client_connected

    @logger_wrapper
    async def check_signature_is_confirmed(self, signature) -> bool:
        result = await self.client.get_signature_statuses([signature])
        status = result.value[0]

        if status is None:
            # ainda não visível para o RPC
            return False
        # transações que falham também são incluídas no bloco (e confirmadas),
        # por isso o erro precisa ser checado antes do status de confirmação
        if status.err is not None:
            raise TransactionFailedError(f"Transação falhou: {status.err}")
        return status.confirmation_status in [
            TransactionConfirmationStatus.Confirmed,
            TransactionConfirmationStatus.Finalized,
        ]

    @logger_wrapper
    async def sign_transaction(
        self, tx: VersionedTransaction, keypair: Keypair
    ) -> VersionedTransaction:
        await self.is_connected()
        latest = await self.client.get_latest_blockhash()

        blockhash = latest.value.blockhash
        message = tx.message
        message = MessageV0(
            header=tx.message.header,
            account_keys=tx.message.account_keys,
            recent_blockhash=blockhash,
            instructions=tx.message.instructions,
            address_table_lookups=tx.message.address_table_lookups,  # type: ignore
        )

        new_tx = VersionedTransaction(
            message=message,
            keypairs=[keypair],
        )

        signature = keypair.sign_message(to_bytes_versioned(message))

        new_tx.signatures = [signature]
        return new_tx

    @logger_wrapper
    async def simulate_transaction(
        self, new_tx: VersionedTransaction, addresses: list[Pubkey] | None = None
    ):
        """Simula; com `addresses`, a resposta traz essas contas como ficariam."""
        await self.is_connected()
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
        await self.is_connected()
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
        await self.is_connected()
        resp = await self.client.send_raw_transaction(bytes(new_tx))
        return resp

    @logger_wrapper
    @_READ_RETRY
    async def get_lamports(self, pubkey: Pubkey) -> Decimal:
        await self.is_connected()
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
        await self.is_connected()
        balances: dict[Pubkey, Decimal] = {}
        for keyed in await self._token_accounts(owner):
            data = bytes(keyed.account.data)
            mint = Pubkey(data[0:32])
            # a mesma mint pode estar em mais de uma conta de token
            balances[mint] = balances.get(mint, Decimal(0)) + token_amount(data)
        return balances
