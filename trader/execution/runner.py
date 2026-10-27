"""O trade-runner (`main.py serve <modo>`): o único processo com a chave.

Serve o `TradeService` do modo para strategy-runners (`main.py connect`) por
JSON em linhas sobre `127.0.0.1` (`docs/architecture.md` §9). Cada conexão fala por
uma spec: `hello` manda os termos dela (`SpecTerms`: par, orçamento, perda
máxima, maior compra, prazo; a spec inteira fica no strategy-runner), que este
processo confere com a política **dele** antes de abrir (ou reusar) o bucket
`strategy:<id>`; depois `tick` (os preços do hub e o bucket numa resposta,
A21), `submit` (com o bucket depois do fill), e `bucket`, `price` e
`candles` (da Jupiter, para o aquecimento). Só este processo fala com a
Jupiter. A cada
`sweep_seconds`, resolve intenções sem desfecho (também no início) e vende o
que sobrou em buckets encerrados ou vencidos: as saídas não dependem do
strategy-runner estar vivo. Com uma posição aberta, uma queda dos preços ou
da conferência das perps por 2 min manda um Telegram, e outro quando passa (A28).
"""

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trader.execution.market.hub import PriceHub
from trader.execution.market.jupiter.candles import MAX_CANDLES
from trader.execution.market.prices import PriceOf, price_fn
from trader.execution.models.perp import perp_terms_for
from trader.execution.notification.outages import Outage
from trader.execution.trade.trading_service.service import TradeService
from trader.shared.logging_config import error_text
from trader.shared.market import CandleSource
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.position import Position
from trader.shared.models.public_data import Interval
from trader.shared.notification.notification_service import (
    NotificationService,
    Notifier,
)
from trader.shared.paths import connection_path
from trader.shared.spec.terms import SpecTerms
from trader.shared.spec.validate import SpecLimits, describe, validate
from trader.shared.trading_service.protocol import BucketStatus, OrderReply
from trader.shared.trading_service.wire import (
    candles_to_list,
    decode,
    encode,
    prices_to_dict,
    reply_to_dict,
    request_from_dict,
    snapshot_to_dict,
)

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 30
WATCH_SECONDS = 10  # de quanto em quanto confere os preços das posições (A28)


class HelloError(ValueError):
    """`hello` recusado (token, spec inválida, spec já conectada)."""


class SpecConnectedError(HelloError):
    """A spec já tem uma sessão: passa quando a antiga cai (`HELLO_RETRY_KINDS`)."""


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
    # avisos ao dono (Telegram): as quedas de rede com posição aberta (A28)
    notifier: Notifier = field(default_factory=NotificationService)
    watch_seconds: float = WATCH_SECONDS
    price_outage: Outage = field(init=False)
    perps_outage: Outage = field(init=False)

    def __post_init__(self) -> None:
        self.price_of = price_fn(self.hub)
        self.price_outage = Outage(f"{self.mode}: preços", self.notifier)
        self.perps_outage = Outage(f"{self.mode}: conferência das perps", self.notifier)

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
                    self._watch_forever(),
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
        if op == "stop":
            return {"stopped": await self._stop(message)}
        if session.bucket is None:
            raise HelloError("mande `hello` primeiro")
        ops = {
            "bucket": self._bucket,
            "submit": self._submit,
            "tick": self._tick,
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
        if reply.filled:
            # o bot loga e avisa com o bucket depois do fill (A21): sem outro
            # `bucket` em seguida
            reply = replace(reply, snapshot=await self.service.get_bucket(bucket))
        return {"reply": reply_to_dict(reply)}

    async def _tick(self, bucket: str, message: dict) -> dict:
        """Um tick de um strategy-runner (A21): os preços USD dos mints, do
        hub (um velho recusa o tick com `StalePriceError`), e o bucket."""
        mints = [str(mint) for mint in message["mints"]]
        quotes = await asyncio.gather(*(self.hub.price(mint) for mint in mints))
        prices = {mint: price for mint, (price, _) in zip(mints, quotes, strict=True)}
        snapshot = await self.service.get_bucket(bucket)
        return {
            "prices": prices_to_dict(prices),
            "snapshot": snapshot_to_dict(snapshot),
        }

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

    def _check_token(self, message: dict) -> None:
        if not secrets.compare_digest(str(message.get("token", "")), self.token):
            raise HelloError("token inválido")

    async def _hello(self, session: _Session, message: dict) -> str:
        self._check_token(message)
        spec = self._valid_terms(message.get("terms"))
        name = f"strategy:{spec.spec_id}"
        if name in self.live:
            raise SpecConnectedError(f"a spec {spec.spec_id} já está conectada")
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
            perp=perp_terms_for(spec.symbol, spec.market, spec.stop_pct),
        )
        self.specs[name] = spec
        # a posição restaurada e a perda máxima já atingida o serviço loga
        logger.warning(
            f"Bucket {name} aberto para a spec {spec.name}: {spec.symbol}, "
            f"orçamento {spec.budget_usd} USD, perda máxima {spec.max_loss_usd} USD"
        )

    # --- parada manual (A33) ---------------------------------------------------

    async def _stop(self, message: dict) -> list[dict]:
        """Para a spec dos termos (ou todas, `all`): sem compras, e a posição
        vendida agora. Não pede `hello`: a spec pode nem estar conectada."""
        self._check_token(message)
        if message.get("all"):
            names = list(self.specs)
        else:
            names = [await self._bucket_for(self._valid_terms(message.get("terms")))]
        return [await self.stop_bucket(name) for name in names]

    async def _bucket_for(self, spec: SpecTerms) -> str:
        name = f"strategy:{spec.spec_id}"
        if name not in self.specs:
            await self._open(name, spec)
        return name

    async def stop_bucket(self, name: str) -> dict:
        """Encerra o bucket e vende a posição; uma venda que falha a
        varredura tenta de novo, como em qualquer bucket encerrado."""
        self.service.stop(name)
        result = {"bucket": name, "spec": self.specs[name].name}
        snapshot = await self.service.get_bucket(name)
        if snapshot.position is None:
            return {**result, "outcome": "flat"}
        reply = await self._sell_leftover(name, snapshot.position)
        if reply is None or reply.filled:
            return {**result, "outcome": "sold" if reply else "no_price"}
        return {**result, "outcome": str(reply.status), "reasons": list(reply.reasons)}

    async def _sell_leftover(self, name: str, position: Position) -> OrderReply | None:
        entry = position.entry_order
        price = await self._pair_price(entry.output_mint, entry.input_mint)
        if price is None:
            return None
        return await self.service.close_bucket(name, price)

    # --- varredura de saídas ---------------------------------------------------

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_seconds)
            await self.sweep()

    async def sweep(self, now: datetime | None = None) -> None:
        """Resolve pendências, confere liquidações, encerra specs vencidas e
        vende sobras. Uma etapa que falha não impede as outras."""
        now = now or datetime.now(UTC)
        steps = (
            ("Resolução de intenções", self.service.resolve_intents),
            ("Resolução de fechamentos de conta", self.service.resolve_rent_refunds),
            ("Conferência das perps", self._check_perps),  # A8, A12
        )
        for label, step in steps:
            try:
                await step()
            except Exception as ex:
                logger.error(f"{label} falhou: {error_text(ex)}")
        for name, spec in list(self.specs.items()):
            try:
                await self._sweep_one(name, spec, now)
            except Exception as ex:
                logger.error(f"Varredura de {name} falhou: {ex}")

    async def _check_perps(self) -> None:
        try:
            await self.service.check_perps()
        except Exception as ex:
            with_position = bool(self.service.open_positions())
            self.perps_outage.failing(with_position, error_text(ex))
            raise
        self.perps_outage.ok()

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

    # --- quedas de preço (A28) -------------------------------------------------

    async def _watch_forever(self) -> None:
        while True:
            await asyncio.sleep(self.watch_seconds)
            try:
                await self.check_prices()
            except Exception as ex:
                logger.error(f"Conferência dos preços falhou: {error_text(ex)}")

    async def check_prices(self) -> None:
        """Os preços das posições abertas estão frescos no hub?

        Pergunta ao hub (`usd_prices`: passa a acompanhar um mint que nenhum
        `connect` pede, ex: o dele caiu), e uma posição sem preço fresco conta
        para a queda; sem posição, não há o que conferir.
        """
        mints = _position_mints(self.service.open_positions().values())
        fresh = await self.hub.usd_prices(mints)
        stale = sorted(set(mints) - set(fresh))
        if not stale:
            self.price_outage.ok()
            return
        ages = ", ".join(f"{_symbol(m)} {self.hub.age(m):.0f}s" for m in stale)
        self.price_outage.failing(True, f"sem preço fresco ({ages})")


def _position_mints(positions: Iterable[Position]) -> list[str]:
    """Os mints de mercado das posições (stablecoins valem 1: fora)."""
    mints = {
        str(mint)
        for position in positions
        for mint in (position.entry_order.output_mint, position.entry_order.input_mint)
    }
    return sorted(m for m in mints if not _is_stable(m))


def _is_stable(mint: str) -> bool:
    known = SOLANA_MINTS.get(mint)
    return known is not None and known.is_usd_stable


def _symbol(mint: str) -> str:
    known = SOLANA_MINTS.get(mint)
    return mint if known is None else known.symbol
