import asyncio
import base64
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import websockets
from solders.pubkey import Pubkey
from solders.solders import VersionedTransaction
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from websockets.asyncio.client import ClientConnection

# `Interval` mora em models (camada core); reexportado por compatibilidade
from trader.models.public_data import Interval as Interval
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.providers.jupiter.logging_utils import logger_wrapper

# lite-api.jup.ag está obsoleta (sem data final confirmada, mas o desligamento
# vem). A API paga/gratuita atual é api.jup.ag; sem `x-api-key` as requisições
# ainda funcionam, só que num limite de taxa menor (keyless). O endpoint
# /swap/v1/* segue com o mesmo formato de request/response de antes, só muda
# o host. Ver docs/plan.md §7.2.
DEFAULT_JUPITER_API_URL = "https://api.jup.ag"

# teto do `Retry-After`: com as re-tentativas do provider por cima, um valor
# alto seguraria uma ordem (e o lock) por minutos
MAX_RETRY_AFTER_SECONDS = 10
_backoff = wait_exponential(multiplier=0.5, max=8)


def is_retryable(ex: BaseException) -> bool:
    """Rede, timeout, 429 (limite da API sem chave) ou 5xx."""
    if isinstance(ex, httpx.TransportError):
        return True
    if isinstance(ex, httpx.HTTPStatusError):
        status = ex.response.status_code
        return status == 429 or status >= 500
    return False


def _wait(state: RetryCallState) -> float:
    """`Retry-After` quando a API manda (com teto); senão, backoff exponencial."""
    ex = state.outcome.exception() if state.outcome else None
    response = getattr(ex, "response", None)
    header = response.headers.get("Retry-After") if response is not None else None
    try:
        return (
            min(float(header), MAX_RETRY_AFTER_SECONDS) if header else _backoff(state)
        )
    except ValueError:
        return _backoff(state)


# só chamadas anteriores ao envio: re-tentar nunca duplica um swap
_HTTP_RETRY = retry(
    wait=_wait,
    stop=stop_after_attempt(4),
    retry=retry_if_exception(is_retryable),
    reraise=True,
)


def _quote_params(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int,
    only_direct_routes: bool,
    max_accounts: int | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
    }
    if only_direct_routes:
        params["onlyDirectRoutes"] = "true"
    if max_accounts is not None:
        params["maxAccounts"] = str(max_accounts)
    return params


def _add_response_notes(ex: Exception, url: str, response) -> None:
    ex.add_note(f"URL: {url}")
    if response:
        ex.add_note(f"Status Code: {response.status_code}")
        ex.add_note(f"Response: {response.text}")


class AsyncJupiterClient:
    def __init__(
        self,
        client=None,
        websocket=None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.logger = logging.getLogger(self.__module__)

        self.websocket = websocket
        # base da API de swap/quote; configurável porque a lite-api.jup.ag
        # está sendo descontinuada em favor de api.jup.ag (docs/plan.md §7.2)
        self.base_url = (
            base_url or os.getenv("JUPITER_API_URL") or DEFAULT_JUPITER_API_URL
        ).rstrip("/")
        self.api_key = api_key or os.getenv("JUPITER_API_KEY") or None

        if client:
            self.client = client
        else:
            self.client = httpx.AsyncClient()
            # Headers padrão para requisições públicas
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["x-api-key"] = self.api_key
            self.client.headers.update(headers)

    @logger_wrapper
    @_HTTP_RETRY
    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
        only_direct_routes: bool = False,
        max_accounts: int | None = None,
    ) -> JupiterQuoteResponse:
        params = _quote_params(
            input_mint,
            output_mint,
            amount,
            slippage_bps,
            only_direct_routes,
            max_accounts,
        )
        url = f"{self.base_url}/swap/v1/quote"
        response = await self.client.get(url, params=params)
        try:
            response.raise_for_status()
            return JupiterQuoteResponse.from_dict(response.json())
        except Exception as ex:
            _add_response_notes(ex, url, response)
            raise ex

    @logger_wrapper
    @_HTTP_RETRY
    async def get_candles(
        self, mint: str, interval: Interval = Interval.SECOND_15, candle_qty: int = 100
    ) -> list[dict[str, Any]]:
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

    @_HTTP_RETRY
    async def get_usd_prices(self, mints: list[str]) -> dict[str, Decimal]:
        """Preços USD da Price API V3 (documentada); mints sem preço ficam de fora.

        Conferido na API real (2026-09-29): `GET /price/v3?ids=a,b` responde
        `{mint: {"usdPrice": ..., "blockId": ..., ...}}` sem chave e omite
        ids desconhecidos.
        """
        if not mints:
            return {}
        url = f"{self.base_url}/price/v3"
        response = await self.client.get(
            url, params={"ids": ",".join(mints)}, timeout=10
        )
        try:
            response.raise_for_status()
            data = json.loads(response.text, parse_float=Decimal)
        except Exception as ex:
            _add_response_notes(ex, url, response)
            raise
        return {
            mint: Decimal(item["usdPrice"])
            for mint, item in data.items()
            if isinstance(item, dict) and item.get("usdPrice") is not None
        }

    @logger_wrapper
    async def get_price(self, mint: str, max_reconnects: int = 5) -> Decimal:
        for attempt in range(max_reconnects + 1):
            try:
                return await self._read_price(mint)
            except websockets.exceptions.ConnectionClosed as ex:
                # ConnectionClosedOK (fechamento normal) também exige reconexão
                self.logger.info(f"INFO: WebSocket Closed: {str(ex)}")
                self.websocket = None
                if attempt == max_reconnects:
                    raise
                await asyncio.sleep(2)  # Espera antes de tentar reconectar
            except Exception as ex:
                self.logger.error(f"Erro ao conectar WebSocket: {str(ex)}", exc_info=ex)
                raise ex
        raise AssertionError("unreachable")

    async def _read_price(self, mint: str) -> Decimal:
        if not self.websocket:
            self.websocket = await self._connect_price_ws(mint)
        return await self._get_price(self.websocket, mint)

    async def _get_price(self, ws: ClientConnection, mint: str) -> Decimal:
        # '{"type":"prices","data":[{"assetId":"DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263","price":0.000010537070513205161,"blockId":380968492}]}'
        # ignora mensagens que não são de preço do mint (acks, heartbeats, outros ativos)
        while True:
            msg = await ws.recv()
            json_msg = json.loads(msg, parse_float=Decimal)
            for item in json_msg.get("data") or []:
                if item.get("assetId", mint) == mint and "price" in item:
                    return Decimal(item["price"])

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

    async def aclose(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        await self.client.aclose()

    @logger_wrapper
    @_HTTP_RETRY
    async def get_swap_transaction(
        self, quote: JupiterQuoteResponse, pubkey: Pubkey
    ) -> VersionedTransaction:
        response = None
        try:
            response = await self.client.post(
                f"{self.base_url}/swap/v1/swap",
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
