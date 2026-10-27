"""Loop de uma estratégia: preço -> estratégia -> ordem no bucket.

Camada strategy-side: o bot lê preços de um `MarketData`, vê o próprio
bucket e pede ordens por um `TradeClient`. Não importa execução, política,
ledger nem provider, e não sabe em que modo roda: o `connect` lhe dá um
`RemoteTradeClient` para o trade-runner.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from trader.shared import logging_config
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import describe_costs, sol_text
from trader.shared.models.order import Order, OrderSide
from trader.shared.models.perp import describe_fill
from trader.shared.models.position import Position
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    HelloRefusedError,
    OrderReply,
    PriceUnavailableError,
    ReplyStatus,
    TradeServiceError,
)
from trader.strategy.bot.config import BotConfig
from trader.strategy.bot.decision import bucket_done, order_for

bot_logger = logging.getLogger("bot")

# falhas seguidas de inicialização antes de avisar o dono
STARTUP_ALERT_AFTER = 5
# A29: a espera mais longa que uma recusa com janela impõe; no fim dela a
# ordem pergunta de novo (e recebe a espera nova)
MAX_DENIAL_WAIT = 3600.0


def wait_text(seconds: float) -> str:
    """Uma espera legível: `30s`, `12 min`, `1h05`."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{round(total / 60)} min"
    hours, rest = divmod(total, 3600)
    return f"{hours}h{rest // 60:02d}"


class TradingBot:
    """Um bot por spec: o loop de ticks, a inicialização e o tratamento de erros."""

    def __init__(self, config: BotConfig):
        self.name = config.name
        self.symbol = config.symbol
        self.output_mint = SOLANA_MINTS.get_pair(config.symbol)[0].mint
        self.strategy = config.strategy
        self.market = config.market
        self.trader = config.trader
        self.notification_service = config.notifier
        self.on_tick = config.on_tick
        self.ticks = config.ticks
        self.is_running = False
        self._opened = False  # bucket aberto (uma vez só)
        self._resumed = False  # estado da estratégia restaurado (uma vez só)
        self._started = False  # aquecido: o loop já processa ticks
        self._startup_failures = 0
        self.logger = logging.getLogger(self.__module__)
        self.logger.debug(f"start bot {self.name}-{self.symbol}: {self.strategy!r}")
        bot_logger.debug(f"start bot {self.name}-{self.symbol}")

        # espera entre iterações após erro; cresce até error_backoff_max
        self.error_backoff_initial = 1.0
        self.error_backoff_max = 60.0
        # após uma ordem recusada, não tenta novas ordens por um tempo: a
        # mesma recusa a cada tick só encheria o ledger (a estratégia segue
        # recebendo os preços normalmente)
        self.denial_cooldown = 30.0
        self.monotonic = time.monotonic
        # por lado (A29): uma compra recusada por uma janela de 24 h não
        # pode segurar o stop de uma venda
        self._orders_paused_until: dict[OrderSide, float] = {}
        # preço e posição vão ao log uma vez por barra (F3), não por tick
        self.wall_clock = time.time
        self._bar_seconds = 1
        self._logged_bar: int | None = None
        # a linha do bucket só sai quando muda (A32: repetia a cada barra)
        self._logged_bucket: str | None = None
        # A21: o bucket depois do último fill, se a resposta o trouxe
        self._after_fill: BucketSnapshot | None = None

    async def process_market_data(
        self, current_price: Decimal, snapshot: BucketSnapshot
    ) -> Order | None:
        if bucket_done(snapshot):
            self.logger.warning(f"Bucket {snapshot.bucket} encerrado: parando o bot")
            self.is_running = False
            return None
        request = order_for(self.strategy, current_price, snapshot)
        if request is None:
            return None
        if self.monotonic() < self._orders_paused_until.get(request.side, 0.0):
            return None
        reply = await self.trader.submit(request)
        return await self._handle_reply(reply, request.side)

    async def _handle_reply(self, reply: OrderReply, side: OrderSide) -> Order | None:
        if reply.status == ReplyStatus.ERROR:
            # falha inesperada: passa pelo backoff do loop
            raise TradeServiceError(reply.error or "erro no serviço de trading")
        self._after_fill = reply.snapshot
        if not reply.filled:
            # recusa esperada (política, saldo, orçamento): não é erro do bot
            wait = self._denial_wait(reply)
            self.logger.warning(
                f"Ordem {reply.status}: {'; '.join(reply.reasons)} "
                f"(novas ordens em {wait_text(wait)})"
            )
            self._orders_paused_until[side] = self.monotonic() + wait
            return None
        return reply.order

    def _denial_wait(self, reply: OrderReply) -> float:
        """Quanto não pedir ordens do lado: a janela da recusa (A29, até 1 h), ou
        `denial_cooldown` para uma recusa sem janela (orçamento, valor
        máximo, saldo) ou de um `serve` que não a manda."""
        if reply.retry_after is None:
            return self.denial_cooldown
        return min(max(reply.retry_after, 1.0), MAX_DENIAL_WAIT)

    def stop(self):
        """Para o bot"""
        self.is_running = False

    def run(self):
        asyncio.run(self.arun())

    async def arun(self):
        # por task: vários bots podem rodar no mesmo processo
        logging_config.botname.set(f"{self.name}-{self.symbol}")
        try:
            await self._loop()
        except asyncio.CancelledError:
            # Ctrl+C sob asyncio.run cancela a task principal
            self.logger.warning("Bot cancelado")
            self.notification_service.send_message("Bot interrompido pelo usuário")
            raise
        finally:
            await self._shutdown()

    async def _shutdown(self):
        self.is_running = False
        for resource in (self.market, self.trader, self.notification_service):
            try:
                await resource.aclose()
            except Exception as ex:
                self.logger.warning(f"Erro ao encerrar conexões: {ex}")

    async def _loop(self):
        self.is_running = True
        backoff = self.error_backoff_initial
        while self.is_running:
            try:
                await self._step()
                backoff = self.error_backoff_initial
            except KeyboardInterrupt:
                self.logger.warning("Bot interrompido pelo usuário")
                self.notification_service.send_message("Bot interrompido pelo usuário")
                self.is_running = False
            except Exception as ex:
                backoff = await self._on_error(ex, backoff)

    async def _step(self):
        # a inicialização também passa pelo backoff: um 429 ao buscar os
        # candles de aquecimento não derruba o bot
        if not self._started:
            await self._guarded_startup()
            return
        await self._tick()

    async def _guarded_startup(self):
        try:
            await self._startup()
        except Exception:
            self._startup_failures += 1
            if self._startup_failures == STARTUP_ALERT_AFTER:
                # ex: candles (datapi, sem fallback) fora do ar: o bot não opera
                self.notification_service.send_message(
                    f"Bot {self.symbol} não consegue iniciar "
                    f"({self._startup_failures} tentativas); veja o log"
                )
            raise

    async def _startup(self):
        if not self._opened:
            # restaura a posição do ledger e reconcilia (lado da execução)
            await self.trader.open()
            self._opened = True
        if not self._resumed:
            # separado de `_opened`: um erro aqui (ex: saldo) tenta de novo no
            # próximo passo, em vez de perder o cooldown e a validade
            await self._resume_strategy()
            self._resumed = True
        interval, count = self.strategy.warmup()
        self._bar_seconds = interval.seconds
        self.strategy.setup(
            await self.market.get_candles(self.output_mint, interval, count)
        )
        self._started = True
        self.notification_service.send_message(f"Bot iniciado para {self.symbol}")

    async def _resume_strategy(self):
        snapshot = await self.trader.bucket()
        self.strategy.resume(
            snapshot.last_exit_at,
            snapshot.opened_at,
            last_exit_price=snapshot.last_exit_price,
        )

    async def _read_tick(self) -> tuple[Decimal, BucketSnapshot]:
        """O preço (no token de cotação do par) e o bucket: uma ida e volta
        com `ticks` (A21), senão o feed e o bucket em separado."""
        if self.ticks is not None:
            return await self.ticks.tick(self.output_mint)
        # o feed divide, num par sem stablecoin
        current_price = await self.market.get_price(self.output_mint)
        return current_price, await self.trader.bucket()

    async def _tick(self):
        current_price, snapshot = await self._read_tick()
        if self.on_tick is not None:
            # o replay mede o patrimônio em USD com o USD da cotação
            self.on_tick(datetime.now(UTC), current_price, snapshot.quote_usd)
        self._log_bar(current_price, snapshot)

        order = await self.process_market_data(current_price, snapshot)
        if order:
            await self._report_order(order, current_price, snapshot.position)

    def _log_bar(self, price: Decimal, snapshot: BucketSnapshot):
        """Preço e posição no primeiro tick de cada barra do timeframe."""
        bar = int(self.wall_clock() // self._bar_seconds)
        if bar == self._logged_bar:
            return
        self._logged_bar = bar
        bucket = with_entry_costs(snapshot)
        if bucket != self._logged_bucket:
            self._logged_bucket = bucket
            log_bucket(bucket)
        log_ticker(self.symbol, price, snapshot.position, snapshot.quote_usd)

    async def _report_order(
        self, order: Order, current_price: Decimal, held: Position | None = None
    ):
        """`held`: a posição antes do fill; numa venda de perp, a linha diz o
        que o fechamento fez no preço e pagou ao venue (A31)."""
        closed = round_trip_line(held, order)
        log_placed_order(order, closed)
        # o bucket depois do fill: PnL realizado e, com posição, a marcação;
        # o trade-runner o manda na resposta (A21)
        after = self._after_fill or await self.trader.bucket()
        perp = "" if order.perp is None else f"\n{describe_fill(order.perp)}"
        perp += f"\n{closed}" if closed else ""
        self.notification_service.send_message(
            f"Ordem executada: {order.side.upper()} "
            f"{order.quantity:.8f} {self.symbol} @ "
            f"USD {order.price:.8f}{perp}\n{bucket_line(after, current_price)}"
        )

    def _refused(self, ex: HelloRefusedError) -> None:
        """O trade-runner recusou a spec: tentar de novo não muda nada (F5)."""
        self.logger.error(f"Spec recusada pelo trade-runner: {ex}; parando o bot")
        self.notification_service.send_message(
            f"[!] Bot {self.symbol} recusado pelo trade-runner: {ex}"
        )
        self.is_running = False

    async def _on_error(self, ex: Exception, backoff: float) -> float:
        """Trata um erro do loop e retorna o próximo backoff."""
        if isinstance(ex, HelloRefusedError):
            # recusa que não passa sozinha (F5); a da sessão antiga ainda viva
            # chega como conexão caída e segue o backoff (`HELLO_RETRY_KINDS`)
            self._refused(ex)
            return backoff
        if isinstance(ex, PriceUnavailableError):
            # esperado (reinício do trade-runner, Price API fora): sem traceback
            self.logger.warning(f"Sem preço agora: {ex}")
        else:
            self.logger.error(
                f"ERROR: Erro no loop principal: {str(ex)}", exc_info=True
            )
        await asyncio.sleep(backoff)
        return min(backoff * 2, self.error_backoff_max)


PRICE_DIGITS = 8  # algarismos significativos dos preços no log


def format_price(price: Decimal, digits: int = PRICE_DIGITS) -> str:
    """`digits` algarismos significativos, sem expoente (0.00060612300)."""
    places = max(0, digits - 1 - price.adjusted()) if price else digits
    return f"{price:.{places}f}"


def log_ticker(
    symbol: str,
    price: Decimal,
    position: Position | None = None,
    quote_usd: Decimal | None = None,
):
    """Uma linha por barra (A32): o preço (no token de cotação) e a posição."""
    held = "sem posição"
    if position is not None:
        held = position_text(position, price, quote_usd, datetime.now(UTC))
    bot_logger.debug(
        f"[blue]{symbol}[/blue] {format_price(price)} | {held}",
        extra={"markup": True},
    )


def log_bucket(summary: str) -> None:
    """O PnL realizado do bucket (e os custos da entrada aberta)."""
    bot_logger.info(f"bucket: {summary}")


def short_signature(signature: str) -> str:
    """O começo e o fim de uma assinatura (a inteira vai na linha dos custos)."""
    if len(signature) <= 16:
        return signature
    return f"{signature[:8]}...{signature[-6:]}"


def log_placed_order(order: Order, closed: str | None = None):
    suffix = f" {closed}" if closed else ""
    bot_logger.info(
        f"[bold]{order.side.upper()}[/bold] {order.quantity:.8f} @ "
        f"${format_price(order.price)} [gray]({short_signature(order.order_id)})"
        f"[/gray]{suffix}",
        extra={"markup": True},
    )
    bot_logger.debug(
        f"{describe_costs(order.costs, order.sol_usd)}; tx {order.order_id}"
    )


def round_trip_line(held: Position | None, order: Order) -> str | None:
    """Numa venda de perp, o que o fechamento fez (A31): o bruto aberto em
    preço, taxas do venue (abertura e fechamento) e empréstimo, e a rede em
    SOL. None no spot, numa compra ou sem os valores das pernas."""
    if held is None or order.side != OrderSide.SELL:
        return None
    pnl = Position(held.entry_order, order).realized_pnl_detail()
    if pnl is None or pnl.price_quote is None:
        return None
    return (
        f"fechamento: bruto {pnl.gross_quote:+.6f} {pnl.quote_symbol}"
        f"{pnl.perp_split()}, rede {pnl.costs_sol:.9f} SOL"
    )


def bucket_line(snapshot: BucketSnapshot, price: Decimal) -> str:
    """PnL realizado do bucket e, com posição aberta, a marcação a mercado.

    `price` é no token de cotação; a marcação é em USD (sem o preço USD da
    cotação, fica de fora).
    """
    line = with_entry_costs(snapshot)
    now = datetime.now(UTC)
    if snapshot.position is not None and snapshot.quote_usd:
        # numa perp, com o empréstimo acumulado (A31)
        unrealized = snapshot.position.unrealized_usd(price * snapshot.quote_usd, now)
        line += f"; aberto ~${unrealized:+.4f} a {price:.8f}"
    perp = snapshot.position and snapshot.position.perp_line(now)
    return f"{line}; {perp}" if perp else line


def with_entry_costs(snapshot: BucketSnapshot) -> str:
    """O PnL realizado e, com posição aberta, os custos da entrada (A27): o
    resumo do bucket só os conta no fechamento, e leria `custos 0` até lá."""
    entry = entry_costs(snapshot.position)
    if not entry:
        return snapshot.pnl_summary
    return f"{snapshot.pnl_summary}; {entry}" if snapshot.pnl_summary else entry


def entry_costs(position: Position | None) -> str | None:
    """Os custos da entrada aberta: rede e rent em SOL, as taxas da perp."""
    if position is None:
        return None
    order = position.entry_order
    parts = []
    if order.costs is not None and order.costs.known:
        costs = order.costs
        rent = f" (rent {sol_text(costs.rent_lamports)})" if costs.rent_lamports else ""
        parts.append(f"custos {sol_text(costs.native_cost_lamports)} SOL{rent}")
    if order.perp is not None:
        parts.append(f"taxas perp ${order.perp.fees_usd:.4f}")
    return f"entrada: {', '.join(parts)}" if parts else None


def _styled(percent: Decimal) -> str:
    style = "green" if percent > 0 else "red"
    return f"[{style}]{percent:.2f}%[/{style}]"


def position_pnl(
    position: Position, price: Decimal, quote_usd: Decimal | None, now: datetime
) -> str:
    """O PnL aberto: o movimento do preço e, numa perp, o retorno sobre o
    colateral postado (A31), com a marcação em USD. A da perp já desconta o
    empréstimo até agora e as taxas (a da abertura e a de fechar agora)."""
    move = _styled(position.unrealized_pnl_percent(price))
    if not quote_usd:
        return move
    usd = position.unrealized_usd(price * quote_usd, now)
    roc = position.return_on_collateral(price * quote_usd, now)
    if roc is None:
        return f"{move} (~${usd:+.4f})"
    # o USD já desconta as taxas e o empréstimo (docs/specs.md, A32)
    return f"preço {move}, colateral {_styled(roc)} ~${usd:+.4f}"


def position_text(
    position: Position, price: Decimal, quote_usd: Decimal | None, now: datetime
) -> str:
    """A posição aberta: lado, tamanho, entrada, PnL e, numa perp, a
    liquidação e o empréstimo."""
    pnl = position_pnl(position, price, quote_usd, now)
    entry = position.entry_order
    side = str(position.direction).upper()
    held = f"{entry.quantity:.8f} @ {format_price(entry.quote_price)}"
    perp = entry.perp
    if perp is None:
        return f"{side} {held} | {pnl}"
    borrow = perp.borrow_at(entry.timestamp, now)
    liquidation = perp.liquidation_price
    risk = "" if liquidation is None else f"liq {format_price(liquidation, 6)}, "
    return f"{side} {perp.leverage}x {held} | {pnl} | {risk}empréstimo ${borrow:.4f}"
