import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import List

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.solders import SendTransactionResp
from solders.transaction import VersionedTransaction

from trader.models import SOLANA_MINTS, TickerData
from trader.models.account_data import MintBalance
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient, Interval
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse


class AsyncJupiterProvider:
    def __init__(
        self, keypair: Keypair, rpc_client=None, jupiter_client=None, is_dryrun=False
    ):
        self.keypair = keypair

        self.rpc_client = rpc_client or AsyncRPCClient(is_dryrun=is_dryrun)
        self.jupiter_client = jupiter_client or AsyncJupiterClient()
        self.logger = logging.getLogger(self.__module__)
        self.logger.info(f"Starting bot on {is_dryrun=}")

    def __repr__(self):
        return f"{self.__class__.__name__}.{str(self.keypair.pubkey())} with rpc {str(self.rpc_client)} and client {str(self.jupiter_client)}"

    async def get_candles(
        self,
        mint: Pubkey,
        interval: Interval = Interval.SECOND_15,
        candle_qty: int = 100,
    ) -> list[TickerData]:
        candles_json = await self.jupiter_client.get_candles(str(mint))
        tickers: list[TickerData] = []
        for candle in candles_json:
            tickers.append(
                TickerData(
                    pair="ignored",
                    timestamp=datetime.fromtimestamp(candle["time"]),
                    high=Decimal(candle["high"]),
                    low=Decimal(candle["low"]),
                    open=Decimal(candle["open"]),
                    last=Decimal(candle["close"]),
                    buy=Decimal(candle["open"]),
                    sell=Decimal(candle["open"]),
                    vol=Decimal(candle["volume"]),
                )
            )
        return tickers

    async def get_price_ticker_data(self, mint: Pubkey) -> Decimal:
        price = await self.jupiter_client.get_price(str(mint))
        return price

    async def get_account_balance(self) -> List[MintBalance]:
        balances = []

        # Saldo de SOL (lamports)
        amount = await self.rpc_client.get_lamports(self.keypair.pubkey())
        solana_mint = SOLANA_MINTS.get_by_symbol("SOL")
        balances.append(
            MintBalance(
                available=solana_mint.raw_to_ui(amount),
                mint=solana_mint.pubkey,
            )
        )

        mint_balances = await self.rpc_client.get_account_balance(self.keypair.pubkey())
        for mint, amount in mint_balances.items():
            mint_info = SOLANA_MINTS.get(mint)
            if not mint_info:
                continue
            balances.append(
                MintBalance(available=mint_info.raw_to_ui(amount), mint=mint)
            )
        return balances

    async def buy(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        type_order: str,
        quantity: Decimal,
        price: Decimal | None = None,
    ) -> str:
        amount_in = quantity * price if price else quantity
        raw_quantity = SOLANA_MINTS[input_mint].ui_to_raw(amount_in)
        return await self.swap(
            str(input_mint),
            str(output_mint),
            raw_quantity,
        )

    async def sell(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        type_order: str,
        quantity: Decimal,
    ) -> str:
        raw_quantity = SOLANA_MINTS[input_mint].ui_to_raw(quantity)
        # venda inverte os mints
        return await self.swap(
            str(output_mint),
            str(input_mint),
            raw_quantity,
        )

    async def swap(
        self,
        input_mint: str,
        output_mint: str,
        raw_quantity: int,
    ) -> str:
        return await self._do_swap_with_retry(
            input_mint,
            output_mint,
            raw_quantity,
        )

    async def _do_swap_with_retry(
        self, input_mint: str, output_mint: str, amount_in: int
    ):
        for i in range(3):
            try:
                return await self._do_swap(
                    input_mint, output_mint, amount_in, [50, 50, 75][i]
                )
            except Exception as e:
                if i == 2:
                    raise e
                self.logger.warning(
                    f"Erro ao executar swap: {e}. Tentando novamente..."
                )
        raise Exception("Erro ao executar swap após múltiplas tentativas")

    async def _get_quote_with_route(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
    ) -> JupiterQuoteResponse:
        quote = await self.jupiter_client.get_quote(
            input_mint, output_mint, amount_in, slippage_bps
        )

        if not quote.routePlan:
            raise Exception("Nenhuma rota encontrada!")

        return quote

    async def _get_swap_transaction(
        self, quote: JupiterQuoteResponse
    ) -> VersionedTransaction:
        return await self.jupiter_client.get_swap_transaction(
            quote, self.keypair.pubkey()
        )

    async def _get_signed_transaction(
        self, tx: VersionedTransaction
    ) -> VersionedTransaction:
        return await self.rpc_client.sign_transaction(tx, self.keypair)

    async def _send_signed_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        await self.rpc_client.simulate_transaction(new_tx)

        resp = await self.rpc_client.send_transaction(new_tx)

        return resp

    async def _wait_for_confirmation(self, signature, timeout=30):
        # if self.is_dryrun:
        #     return True

        start = time.time()

        while True:
            try:
                if await self.rpc_client.check_signature_is_confirmed(signature):
                    return True
                else:
                    raise Exception("Transação falhou: Esperando confirmacao")
            except Exception as ex:
                if "Transação falhou" in str(ex):
                    await asyncio.sleep(1.0)

            if time.time() - start > timeout:
                raise TimeoutError("Transação não foi confirmada a tempo.")

    async def _send_transaction_and_wait_for_confirmation(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        resp = await self._send_signed_transaction(new_tx)
        signature = resp.value
        await self._wait_for_confirmation(signature)
        return resp

    async def _do_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
    ):
        quote = await self._get_quote_with_route(
            input_mint, output_mint, amount_in, slippage_bps
        )
        tx = await self._get_swap_transaction(quote)
        new_tx = await self._get_signed_transaction(tx)
        resp = await self._send_transaction_and_wait_for_confirmation(new_tx)
        # try:
        #     return json.loads(resp.value.to_bytes())["result"]
        # except Exception as ex:
        #     self.logger.error(f"ERROR.send_transaction.convert_result: {str(ex)}")
        return resp.to_json()
