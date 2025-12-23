import asyncio
import base64
import json
import logging
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Dict

import httpx
import websockets
from solders.pubkey import Pubkey
from solders.solders import VersionedTransaction
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed
from websockets.asyncio.client import ClientConnection

from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse


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


class Interval(StrEnum):
    SECOND_15 = "15_SECOND"
    MINUTE_1 = "1_MINUTE"
    HOUR_1 = "1_HOUR"


class AsyncJupiterClient:
    def __init__(self, client=None, websocket=None):
        self.logger = logging.getLogger(self.__module__)

        self.websocket = websocket
        if client:
            self.client = client
        else:
            self.client = httpx.AsyncClient()
            # Headers padrão para requisições públicas
            self.client.headers.update(
                {
                    "Content-Type": "application/json",
                }
            )

    @logger_wrapper
    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
        only_direct_routes: bool = False,
        max_accounts: int | None = None,
    ) -> JupiterQuoteResponse:
        params: Dict[str, Any] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }

        if only_direct_routes:
            params["onlyDirectRoutes"] = "true"

        if max_accounts is not None:
            params["maxAccounts"] = str(max_accounts)

        url = "https://lite-api.jup.ag/swap/v1/quote"
        response = await self.client.get(url, params=params)
        try:
            response.raise_for_status()
            response_json = response.json()
            return JupiterQuoteResponse.from_dict(response_json)
        except Exception as ex:
            if response.status_code == 429:
                await asyncio.sleep(1.5)
            ex.add_note(f"URL: {url}")
            if response:
                ex.add_note(f"Status Code: {response.status_code}")
                ex.add_note(f"Response: {response.text}")
            raise ex

    @logger_wrapper
    @retry(
        wait=wait_fixed(2),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.ReadTimeout),
        reraise=True,
    )
    async def get_candles(
        self, mint: str, interval: Interval = Interval.SECOND_15, candle_qty: int = 100
    ) -> list[Dict[str, Any]]:
        end_time = int(datetime.now().timestamp() * 1000)
        url = (
            f"https://datapi.jup.ag/v2/charts/{mint}"
            f"?interval={interval}&to={end_time}&candles={candle_qty}"
            "&type=price&quote=usd"
        )
        response = None
        try:
            response = await self.client.get(
                url,
                headers={
                    "Origin": "https://jup.ag",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=20,
            )
            response.raise_for_status()
            response_json = response.json()

            return response_json["candles"]
        except Exception as ex:
            ex.add_note(f"URL: {url}")
            if response is not None:
                ex.add_note(f"Status Code: {response.status_code}")
                ex.add_note(f"Response: {response.text}")
            raise ex

    @logger_wrapper
    async def get_price(self, mint: str) -> Decimal:
        try:
            if not self.websocket:
                self.websocket = await self._connect_price_ws(mint)
            return await self._get_price(self.websocket)
        except websockets.exceptions.ConnectionClosedError as ex:
            self.logger.info(f"INFO: WebSocket Closed: {str(ex)}")
            await asyncio.sleep(2)  # Espera antes de tentar reconectar
            self.websocket = None
            return await self.get_price(mint)
        except Exception as ex:
            self.logger.error(f"Erro ao conectar WebSocket: {str(ex)}", exc_info=ex)
            raise ex

    async def _get_price(self, ws: ClientConnection) -> Decimal:
        msg = await ws.recv()
        # '{"type":"prices","data":[{"assetId":"DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263","price":0.000010537070513205161,"blockId":380968492}]}'
        json_msg = json.loads(msg)
        price = Decimal(json_msg["data"][0]["price"])
        return price

    @logger_wrapper
    async def _connect_price_ws(self, mint: str):
        ws = await websockets.connect(
            "wss://trench-stream.jup.ag/ws",
            additional_headers={"Origin": "https://jup.ag"},
            compression="deflate",
        )

        await ws.send(json.dumps({"type": "subscribe:prices", "assets": [mint]}))
        self.websocket = ws
        return ws

    @logger_wrapper
    async def get_swap_transaction(
        self, quote: JupiterQuoteResponse, pubkey: Pubkey
    ) -> VersionedTransaction:
        response = None
        try:
            response = await self.client.post(
                "https://lite-api.jup.ag/swap/v1/swap",
                json={
                    "quoteResponse": asdict(quote),
                    "userPublicKey": str(pubkey),
                },
            )
            response.raise_for_status()
            swap_tx = response.json()
            raw_tx = base64.b64decode(swap_tx["swapTransaction"])

            # ---------- desserializar ----------
            tx = VersionedTransaction.from_bytes(raw_tx)
            return tx

        except Exception as ex:
            if response is not None:
                ex.add_note(f"Status Code: {response.status_code}")
                ex.add_note(f"Response: {response.text}")
            raise ex
