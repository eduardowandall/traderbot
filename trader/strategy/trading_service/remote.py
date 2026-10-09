"""`RemoteTradeClient`: o `TradeClient` de um strategy-runner (camada strategy-side).

Fala com o trade-runner (`main.py serve`) por JSON em linhas sobre TCP local
(`docs/plan.md` §3.3). Não tem chave, ledger nem modo. O `hello` leva só os
termos da spec (`SpecTerms`); a spec inteira fica neste processo. Toda ordem sai com
chave de idempotência: se a conexão cai, o cliente reconecta (espera
crescente), manda `hello` de novo e reenvia o pedido com a mesma chave, então
uma ordem executa no máximo uma vez (o reenvio de uma que já executou volta
recusado, e o próximo `bucket()` mostra a posição).

Os dados de mercado também vêm do trade-runner: `price` (o hub dele) e
`candles` (o aquecimento); o strategy-runner não fala com a Jupiter.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from trader.shared.models.public_data import Interval, TickerData
from trader.shared.spec.terms import SpecTerms
from trader.shared.trading_service.protocol import (
    HELLO_RETRY_KINDS,
    REMOTE_ERRORS,
    BucketSnapshot,
    HelloRefusedError,
    OrderReply,
    OrderRequest,
    TradeServiceError,
)
from trader.shared.trading_service.wire import (
    candles_from_list,
    decode,
    encode,
    reply_from_dict,
    request_to_dict,
    snapshot_from_dict,
)

logger = logging.getLogger(__name__)

# espera entre reconexões: começa em BACKOFF_INITIAL, dobra até BACKOFF_MAX
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 30.0
# tentativas de reconexão por chamada (depois, o loop do bot assume)
RECONNECT_ATTEMPTS = 5
# maior linha aceita do trade-runner: 900 candles dão ~120 KB, e o padrão do
# asyncio (64 KB) recusaria
LINE_LIMIT = 4 * 1024 * 1024


class RemoteTradeClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        terms: SpecTerms,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
        # relê o endereço (host, port, token): um trade-runner reiniciado tem
        # porta e token novos no arquivo de conexão
        resolve: Callable[[], dict] | None = None,
    ):
        self.resolve = resolve
        self.host = host
        self.port = port
        self.token = token
        self.terms = terms
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.bucket_name: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    # --- TradeClient -----------------------------------------------------------

    async def open(self) -> None:
        await self._connect()

    async def bucket(self) -> BucketSnapshot:
        answer = await self._call({"op": "bucket"})
        return snapshot_from_dict(answer["snapshot"])

    async def submit(self, request: OrderRequest) -> OrderReply:
        if request.idempotency_key is None:
            # a chave é fixada aqui: um reenvio depois de reconectar não duplica
            request = replace(request, idempotency_key=uuid.uuid4().hex)
        answer = await self._call({"op": "submit", "request": request_to_dict(request)})
        return reply_from_dict(answer["reply"])

    async def price(self, mint: str) -> Decimal:
        """O preço USD do mint pelo hub do trade-runner (um feed para todos).

        Preço velho ou ausente lá vira `PriceUnavailableError`: o bot não decide.
        """
        answer = await self._call({"op": "price", "mint": mint})
        return Decimal(answer["price"])

    async def candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]:
        """Os candles do mint pelo trade-runner (só no aquecimento)."""
        answer = await self._call(
            {
                "op": "candles",
                "mint": mint,
                "interval": str(interval),
                "qty": candle_qty,
            }
        )
        return candles_from_list(answer["candles"])

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None

    # --- conexão ---------------------------------------------------------------

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, limit=LINE_LIMIT
        )
        answer = await self._exchange(
            {
                "op": "hello",
                "token": self.token,
                "terms": self.terms.model_dump(mode="json"),
            }
        )
        if not answer.get("ok"):
            await self.aclose()
            error = answer.get("error") or "hello recusado"
            if answer.get("kind") in HELLO_RETRY_KINDS:
                raise ConnectionError(error)  # passa sozinha: tenta de novo
            raise HelloRefusedError(error)
        self.bucket_name = answer["bucket"]

    async def _exchange(self, message: dict) -> dict:
        """Um pedido e a resposta; conexão caída vira `ConnectionError`.

        Um por vez: num par sem stablecoin, o bot pede o preço do token e o da
        cotação juntos, e duas leituras na mesma conexão não podem se cruzar.
        """
        async with self._lock:
            if self._reader is None or self._writer is None:
                raise ConnectionError("sem conexão com o trade-runner")
            self._writer.write(encode(message))
            await self._writer.drain()
            line = await self._reader.readline()
        if not line:
            raise ConnectionError("o trade-runner fechou a conexão")
        return decode(line)

    async def _call(self, message: dict) -> dict:
        """O pedido, reconectando (e reenviando igual) se a conexão cair.

        Desiste depois de `RECONNECT_ATTEMPTS`: o erro volta para o loop do bot,
        que tem a espera e o alerta dele, e a próxima chamada tenta de novo.
        """
        delay = self.backoff_initial
        for _ in range(RECONNECT_ATTEMPTS):
            try:
                answer = await self._exchange(message)
                break
            except (ConnectionError, OSError) as ex:
                logger.warning(f"Trade-runner fora ({ex}); reconectando em {delay}s")
                await self.aclose()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.backoff_max)
                await self._reconnect_quietly()
        else:
            raise TradeServiceError("trade-runner fora do ar")
        if not answer.get("ok"):
            error = answer.get("error") or "erro no trade-runner"
            raise REMOTE_ERRORS.get(answer.get("kind", ""), TradeServiceError)(error)
        return answer

    async def _reconnect_quietly(self) -> None:
        try:
            self._refresh_address()
            await self._connect()
        except (ConnectionError, OSError, ValueError) as ex:
            logger.warning(f"Reconexão falhou: {ex}")

    def _refresh_address(self) -> None:
        if self.resolve is None:
            return
        info = self.resolve()
        self.host, self.port, self.token = (
            info["host"],
            int(info["port"]),
            info["token"],
        )


class RemoteCandles:
    """A metade de candles de um `MarketData`, pelo trade-runner (op `candles`).

    Fechar não fecha a conexão: ela é do `RemoteTradeClient`, que o bot fecha.
    """

    def __init__(self, trader: RemoteTradeClient):
        self.trader = trader

    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]:
        return await self.trader.candles(mint, interval, candle_qty)

    async def aclose(self) -> None:
        pass
