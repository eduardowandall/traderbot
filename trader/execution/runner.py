"""O trade-runner (`main.py serve <modo>`): o único processo com a chave.

Serve o `TradeService` do modo para strategy-runners (`main.py connect`) por
JSON em linhas sobre `127.0.0.1` (`docs/plan.md` §3.3). Cada conexão fala por
uma spec: `hello` manda os termos dela (`SpecTerms`: par, orçamento, perda
máxima, maior compra, prazo; a spec inteira fica no strategy-runner), que este
processo confere com a política **dele** antes de abrir (ou reusar) o bucket
`strategy:<id>`; depois `bucket` e `submit`, e os dados de
mercado: `price` (do hub) e `candles` (da Jupiter, para o aquecimento). Só
este processo fala com a Jupiter. A cada
`sweep_seconds`, resolve intenções sem desfecho (também no início) e vende o
que sobrou em buckets encerrados ou vencidos: as saídas não dependem do
strategy-runner estar vivo.
"""

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trader.execution.market.hub import PriceHub
from trader.execution.market.jupiter.candles import MAX_CANDLES
from trader.execution.market.prices import PriceOf, price_fn
from trader.execution.trade.trading_service.service import TradeService
from trader.shared.logging_config import error_text
from trader.shared.market import CandleSource
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.public_data import Interval
from trader.shared.paths import connection_path
from trader.shared.spec.terms import SpecTerms
from trader.shared.spec.validate import SpecLimits, describe, validate
from trader.shared.trading_service.protocol import BucketStatus
from trader.shared.trading_service.wire import (
    candles_to_list,
    decode,
    encode,
    reply_to_dict,
    request_from_dict,
    snapshot_to_dict,
)

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 30


class HelloError(ValueError):
    """`hello` recusado (token, spec inválida, spec já conectada)."""


@dataclass
class _Session:
    """Estado de uma conexão: a spec dela, depois do `hello`."""

    bucket: str | None = None
    # "nome (id)" da spec, para o log
    label: str = ""


@dataclass
class TradeRunner:
    service: TradeService
    mode: str
    limits: SpecLimits
    # preços para os strategy-runners (op `price`) e para a varredura: um feed
    # para todos; roda junto do servidor
    hub: PriceHub
    # candles para os strategy-runners (op `candles`): o aquecimento deles;
    # fecha quando o servidor para
    candles: CandleSource
    token: str = field(default_factory=lambda: secrets.token_hex(16))
    sweep_seconds: float = SWEEP_SECONDS
    specs: dict[str, SpecTerms] = field(default_factory=dict)
    live: set[str] = field(default_factory=set)
    # preço USD de um mint (stablecoins a 1), do hub: vender sobras
    price_of: PriceOf = field(init=False)
    # o que mais roda junto do servidor até ele parar (o relatório diário)
    background: Sequence[Callable[[], Coroutine[Any, Any, None]]] = ()

    def __post_init__(self) -> None:
        self.price_of = price_fn(self.hub)

    # --- servidor ------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> asyncio.Server:
        server = await asyncio.start_server(self._handle, host, port)
        addr = server.sockets[0].getsockname()
        logger.warning(f"Trade-runner {self.mode} em {addr[0]}:{addr[1]}")
        return server

    async def serve(self) -> None:
        """Serve até ser cancelado (Ctrl+C); o arquivo de conexão sai no fim."""
        # antes de aceitar conexões: um processo anterior morto no meio de
        # um swap deixou intenções sem desfecho (A3)
        await self.service.resolve_intents()
        server = await self.start()
        path = connection_path(self.mode)
        self._write_connection(path, server.sockets[0].getsockname()[1])
        try:
            async with server:
                await asyncio.gather(
                    server.serve_forever(),
                    self._sweep_forever(),
                    self.hub.run(),
                    *(start() for start in self.background),
                )
        finally:
            path.unlink(missing_ok=True)
            await self.candles.aclose()

    def _write_connection(self, path: Path, port: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        info = {"host": "127.0.0.1", "port": port, "token": self.token}
        path.write_text(json.dumps({**info, "pid": os.getpid()}), encoding="utf-8")

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = _Session()
        try:
            while line := await reader.readline():
                writer.write(encode(await self._answer(session, line)))
                await writer.drain()
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            if session.bucket is not None:
                self.live.discard(session.bucket)
                logger.warning(
                    f"Spec {session.label} desconectada ({self._connected()})"
                )
            writer.close()

    def _connected(self) -> str:
        return f"{len(self.live)} conectada(s)"

    async def _answer(self, session: _Session, line: bytes) -> dict:
        try:
            message = decode(line)
            return {"ok": True, **await self._dispatch(session, message)}
        except Exception as ex:
            # recusas e erros viram resposta: a conexão continua
            logger.warning(f"Pedido recusado: {type(ex).__name__}: {ex}")
            kind = type(ex).__name__  # o lado da estratégia escolhe a exceção
            return {"ok": False, "error": f"{kind}: {ex}", "kind": kind}

    async def _dispatch(self, session: _Session, message: dict) -> dict:
        op = message.get("op")
        if op == "hello":
            return {"bucket": await self._hello(session, message)}
        if session.bucket is None:
            raise HelloError("mande `hello` primeiro")
        ops = {
            "bucket": self._bucket,
            "submit": self._submit,
            "price": self._price,
            "candles": self._candles,
        }
        if op not in ops:
            raise ValueError(f"operação desconhecida: {op!r}")
        return await ops[op](session.bucket, message)

    async def _bucket(self, bucket: str, message: dict) -> dict:
        snapshot = await self.service.get_bucket(bucket)
        return {"snapshot": snapshot_to_dict(snapshot)}

    async def _submit(self, bucket: str, message: dict) -> dict:
        request = request_from_dict(message["request"])
        reply = await self.service.submit_order(bucket, request)
        return {"reply": reply_to_dict(reply)}

    async def _price(self, bucket: str, message: dict) -> dict:
        """O preço USD de um mint, do hub (`StalePriceError` se velho)."""
        price, age = await self.hub.price(str(message["mint"]))
        return {"price": str(price), "age": age}

    async def _candles(self, bucket: str, message: dict) -> dict:
        """Os candles do aquecimento de um strategy-runner, da Jupiter."""
        qty = int(message["qty"])
        if not 1 <= qty <= MAX_CANDLES:
            raise ValueError(f"qty deve estar entre 1 e {MAX_CANDLES}: {qty}")
        candles = await self.candles.get_candles(
            str(message["mint"]), Interval(message["interval"]), qty
        )
        return {"candles": candles_to_list(candles)}

    # --- hello -----------------------------------------------------------------

    async def _hello(self, session: _Session, message: dict) -> str:
        if not secrets.compare_digest(str(message.get("token", "")), self.token):
            raise HelloError("token inválido")
        spec = self._valid_terms(message.get("terms"))
        name = f"strategy:{spec.spec_id}"
        if name in self.live:
            raise HelloError(f"a spec {spec.spec_id} já está conectada")
        if name not in self.specs:
            await self._open(name, spec)
        self.live.add(name)
        session.bucket = name
        session.label = f"{spec.name} ({spec.spec_id})"
        logger.warning(
            f"Spec {session.label} conectada ao bucket {name} ({self._connected()})"
        )
        return name

    def _valid_terms(self, raw) -> SpecTerms:
        try:
            spec = SpecTerms.model_validate(raw)
        except ValidationError as ex:
            raise HelloError(f"termos inválidos: {ex.error_count()} erro(s)") from ex
        errors = validate(spec, self.limits)
        if errors:
            raise HelloError(f"spec inválida para {self.mode}: {describe(errors)}")
        return spec

    async def _open(self, name: str, spec: SpecTerms) -> None:
        token, quote = SOLANA_MINTS.get_pair(spec.symbol)
        await self.service.open_bucket(
            name,
            quote.mint,
            token.mint,
            budget_usd=spec.budget_usd,
            source=f"serve:{spec.name}",
            max_loss_usd=spec.max_loss_usd,
        )
        self.specs[name] = spec
        # a posição restaurada e a perda máxima já atingida o serviço loga
        logger.warning(
            f"Bucket {name} aberto para a spec {spec.name}: {spec.symbol}, "
            f"orçamento {spec.budget_usd} USD, perda máxima {spec.max_loss_usd} USD"
        )

    # --- varredura de saídas ---------------------------------------------------

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_seconds)
            await self.sweep()

    async def sweep(self, now: datetime | None = None) -> None:
        """Resolve intenções pendentes, encerra specs vencidas e vende sobras."""
        now = now or datetime.now(UTC)
        try:
            await self.service.resolve_intents()
        except Exception as ex:
            logger.error(f"Resolução de intenções falhou: {ex}")
        try:
            await self.service.resolve_rent_refunds()
        except Exception as ex:
            logger.error(f"Resolução de fechamentos de conta falhou: {error_text(ex)}")
        for name, spec in list(self.specs.items()):
            try:
                await self._sweep_one(name, spec, now)
            except Exception as ex:
                logger.error(f"Varredura de {name} falhou: {ex}")

    async def _sweep_one(self, name: str, spec: SpecTerms, now: datetime) -> None:
        snapshot = await self.service.get_bucket(name)
        status = snapshot.status
        if (
            status == BucketStatus.ACTIVE
            and spec.expiry(snapshot.opened_at or now) <= now
        ):
            self.service.retire(name, "spec vencida")
            status = BucketStatus.RETIRING
        if status != BucketStatus.RETIRING:
            return
        if snapshot.position is None:
            # encerrado e vendido: a conta do token devolve o rent (A15)
            await self.service.close_token_account(name)
            return
        entry = snapshot.position.entry_order
        price = await self._pair_price(entry.output_mint, entry.input_mint)
        if price is not None:
            await self.service.close_bucket(name, price)

    async def _pair_price(self, token: str, quote: str) -> Decimal | None:
        """O preço do token no token de cotação (como a estratégia o vê)."""
        token_usd, quote_usd = await asyncio.gather(
            self.price_of(token), self.price_of(quote)
        )
        if token_usd is None or not quote_usd:
            return None
        return token_usd / quote_usd
