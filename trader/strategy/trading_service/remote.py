"""`RemoteTradeClient`: o `TradeClient` de um strategy-runner (camada strategy-side).

Fala com o trade-runner (`main.py serve`) por JSON em linhas sobre TCP local
(`docs/plan.md` §3.3). Não tem chave, ledger nem modo. Toda ordem sai com
chave de idempotência: se a conexão cai, o cliente reconecta (espera
crescente), manda `hello` de novo e reenvia o pedido com a mesma chave, então
uma ordem executa no máximo uma vez (o reenvio de uma que já executou volta
recusado, e o próximo `bucket()` mostra a posição).
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    OrderReply,
    OrderRequest,
    TradeServiceError,
)
from trader.shared.trading_service.wire import (
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


class HelloRefusedError(TradeServiceError):
    """O trade-runner recusou a spec ou o token: reconectar não resolve."""


class RemoteTradeClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        spec: dict,
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
        self.spec = spec
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.bucket_name: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

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

        Preço velho ou ausente lá vira `TradeServiceError`: o bot não decide.
        """
        answer = await self._call({"op": "price", "mint": mint})
        return Decimal(answer["price"])

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None

    # --- conexão ---------------------------------------------------------------

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        answer = await self._exchange(
            {"op": "hello", "token": self.token, "spec": self.spec}
        )
        if not answer.get("ok"):
            await self.aclose()
            raise HelloRefusedError(answer.get("error") or "hello recusado")
        self.bucket_name = answer["bucket"]

    async def _exchange(self, message: dict) -> dict:
        """Um pedido e a resposta; conexão caída vira `ConnectionError`."""
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
            raise TradeServiceError(answer.get("error") or "erro no trade-runner")
        return answer

    async def _reconnect_quietly(self) -> None:
        try:
            self._refresh_address()
            await self._connect()
        except HelloRefusedError:
            raise
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
