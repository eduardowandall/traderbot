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
from trader.shared.models.costs import describe_costs
from trader.shared.models.order import Order
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
        self._orders_paused_until = 0.0
        # preço e posição vão ao log uma vez por barra (F3), não por tick
        self.wall_clock = time.time
        self._bar_seconds = 1
        self._logged_bar: int | None = None

    async def process_market_data(
        self, current_price: Decimal, snapshot: BucketSnapshot
    ) -> Order | None:
        if bucket_done(snapshot):
            self.logger.warning(f"Bucket {snapshot.bucket} encerrado: parando o bot")
            self.is_running = False
            return None
        request = order_for(self.strategy, current_price, snapshot)
        if request is None or self.monotonic() < self._orders_paused_until:
            return None
        return await self._handle_reply(await self.trader.submit(request))

    async def _handle_reply(self, reply: OrderReply) -> Order | None:
        if reply.status == ReplyStatus.ERROR:
            # falha inesperada: passa pelo backoff do loop
            raise TradeServiceError(reply.error or "erro no serviço de trading")
        if not reply.filled:
            # recusa esperada (política, saldo, orçamento): não é erro do bot
            self.logger.warning(
                f"Ordem {reply.status}: {'; '.join(reply.reasons)} "
                f"(novas ordens em {self.denial_cooldown:.0f}s)"
            )
            self._orders_paused_until = self.monotonic() + self.denial_cooldown
            return None
        return reply.order

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

    async def _tick(self):
        # no token de cotação do par (o feed divide, num par sem stablecoin)
        current_price = await self.market.get_price(self.output_mint)
        snapshot = await self.trader.bucket()
        if self.on_tick is not None:
            # o replay mede o patrimônio em USD com o USD da cotação
            self.on_tick(datetime.now(UTC), current_price, snapshot.quote_usd)
        self._log_bar(current_price, snapshot)

        order = await self.process_market_data(current_price, snapshot)
        if order:
            await self._report_order(order, current_price)

    def _log_bar(self, price: Decimal, snapshot: BucketSnapshot):
        """Preço e posição no primeiro tick de cada barra do timeframe."""
        bar = int(self.wall_clock() // self._bar_seconds)
        if bar == self._logged_bar:
            return
        self._logged_bar = bar
        log_ticker(self.symbol, price, snapshot.pnl_summary)
        if snapshot.position:
            log_position(snapshot.position, price, snapshot.quote_usd)

    async def _report_order(self, order: Order, current_price: Decimal):
        log_placed_order(order)
        # o bucket depois do fill: PnL realizado e, com posição, a marcação
        after = await self.trader.bucket()
        perp = "" if order.perp is None else f"\n{describe_fill(order.perp)}"
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


def log_ticker(symbol: str, price: Decimal, pnl_summary: str | None = None):
    # no token de cotação do par (USD em USDC/USDT)
    suffix = f" {pnl_summary}" if pnl_summary else ""
    bot_logger.debug(
        f"[blue]{symbol}[/blue] @ {format_price(price)}.{suffix}",
        extra={"markup": True},
    )


def log_placed_order(order: Order):
    bot_logger.info(
        f"[gray]{order.side.upper()}[/gray] {order.quantity:.8f} @ "
        f"${order.price:.8f} [gray]({order.order_id})[/gray]",
        extra={"markup": True},
    )
    bot_logger.debug(describe_costs(order.costs, order.sol_usd))


def bucket_line(snapshot: BucketSnapshot, price: Decimal) -> str:
    """PnL realizado do bucket e, com posição aberta, a marcação a mercado.

    `price` é no token de cotação; a marcação é em USD (sem o preço USD da
    cotação, fica de fora).
    """
    line = snapshot.pnl_summary
    if snapshot.position is not None and snapshot.quote_usd:
        unrealized = snapshot.position.unrealized_usd(price * snapshot.quote_usd)
        line += f"; aberto ~${unrealized:+.4f} a {price:.8f}"
    perp = snapshot.position and snapshot.position.perp_line(datetime.now(UTC))
    return f"{line}; {perp}" if perp else line


def log_position(position: Position, current_price: Decimal, quote_usd: Decimal | None):
    # a posição aberta do bucket (o snapshot nunca traz uma fechada)
    pnl = position.unrealized_pnl_percent(current_price)
    pnl_style = "green" if pnl > 0 else "red"
    pnl_str = f"[{pnl_style}]{pnl:.2f}%[/{pnl_style}]"
    usd = position.unrealized_usd(current_price * quote_usd) if quote_usd else None
    unrealized = "" if usd is None else f" (~${usd:+.4f})"

    side = str(position.direction).upper()
    perp = position.perp_line(datetime.now(UTC))
    extra = f" [{perp}]" if perp else ""
    bot_logger.debug(
        f"{side} {position.entry_order.quantity:.8f} @ {format_price(position.entry_order.quote_price)}. PNL: {pnl_str}{unrealized}{extra}",
        extra={"markup": True},
    )
