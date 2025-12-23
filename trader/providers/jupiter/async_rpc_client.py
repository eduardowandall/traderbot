import logging
import os
from decimal import Decimal

from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts
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


def logger_wrapper(func):
    async def wrapper(*args, **kwargs):
        logger = logging.getLogger(func.__module__)
        try:
            result = await func(*args, **kwargs)
            logger.debug(
                f"{func.__name__} with args={args}, kwargs={kwargs} with result={result}"
            )
            return result
        except Exception as e:
            logger.error(
                f"{func.__name__}  with args={args}, kwargs={kwargs} error={str(e)}",
                exc_info=True,
            )
            raise

    return wrapper


class AsyncRPCClient:
    def __init__(self, client=None, is_dryrun=False):
        self.logger = logging.getLogger(self.__module__)
        if client:
            self.client = client
        else:
            rpc_url = os.getenv("HELIUS_RPC_URL")
            assert rpc_url, "RPC URL não definida"
            self.client = AsyncClient(rpc_url)
        self._client_connected = False
        self.is_dryrun = is_dryrun

    @logger_wrapper
    async def is_connected(self):
        if self._client_connected:
            return True
        self._client_connected = await self.client.is_connected()
        return self._client_connected

    @logger_wrapper
    async def check_signature_is_confirmed(self, signature) -> bool:
        if self.is_dryrun:
            return True
        result = await self.client.get_signature_statuses([signature])
        status = result.value[0]

        if status is not None:
            # Se a transação foi processada
            if status.confirmation_status in [
                TransactionConfirmationStatus.Confirmed,
                TransactionConfirmationStatus.Finalized,
            ]:
                return True
            if status.err is not None:
                raise Exception(f"Transação falhou: {status.err}")

        raise Exception(f"Transação falhou: {result.value}")

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
    async def simulate_transaction(self, new_tx: VersionedTransaction):
        await self.is_connected()
        simulation = await self.client.simulate_transaction(new_tx)
        if simulation.value.err:
            raise Exception(f"Erro ao simular transação: {str(simulation.value.err)}")
        return simulation

    @logger_wrapper
    async def send_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        if self.is_dryrun:
            return SendTransactionResp(value=Signature.new_unique())

        await self.is_connected()
        resp = await self.client.send_raw_transaction(bytes(new_tx))
        return resp

    @logger_wrapper
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
    async def get_account_balance(self, owner: Pubkey) -> dict[Pubkey, Decimal]:
        await self.is_connected()
        balances: dict[Pubkey, Decimal] = {}

        for token in [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]:
            try:
                token_accounts = await self.client.get_token_accounts_by_owner(
                    owner, TokenAccountOpts(program_id=token)
                )

                for token_acc in token_accounts.value:
                    info = token_acc.account.data  # base64 data
                    decoded = bytes(info)
                    mint = Pubkey(decoded[0:32])
                    amount = int.from_bytes(decoded[64:72], "little")
                    balances[mint] = Decimal(amount)
            except SolanaRpcException as ex:
                self.logger.error(f"ERROR.get_account_balance: {str(ex)}")

        return balances
