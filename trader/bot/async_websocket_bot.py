"""Loop de uma estratégia: preço -> estratégia -> ordem no bucket.

Camada strategy-side: o bot lê preços de um `MarketData`, vê o próprio
bucket e pede ordens por um `TradeClient`. Não importa execução, política,
ledger nem provider, e não sabe em que modo roda — no mesmo processo
(`LocalTradeClient`) ou, no futuro (B3), falando com um trade-runner.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from trader import logging_config
from trader.bot.config import BotConfig
from trader.bot.decision import bucket_done, order_for
from trader.models import SOLANA_MINTS
from trader.models.costs import describe_costs
from trader.models.order import Order
from trader.models.position import Position
from trader.trading_service.protocol import (
    BucketSnapshot,
    OrderReply,
    ReplyStatus,
    TradeServiceError,
)

bot_logger = logging.getLogger("bot")

# falhas seguidas de inicialização antes de avisar o dono
STARTUP_ALERT_AFTER = 5


class AsyncWebsocketTradingBot:
    def __init__(self, config: BotConfig):
        self.name = config.name
        self.symbol = config.symbol
        token, _ = SOLANA_MINTS.get_pair(config.symbol)
        self.output_mint = token.mint
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
        # da tempo da wallet atualizar a operacao feita.
        await asyncio.sleep(2.0)
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
        current_price = await self.market.get_price(self.output_mint)
        if self.on_tick is not None:
            self.on_tick(datetime.now(UTC), current_price)
        snapshot = await self.trader.bucket()
        log_ticker(self.symbol, current_price, snapshot.pnl_summary)

        order = await self.process_market_data(current_price, snapshot)
        if order:
            self._report_order(order)
        elif snapshot.position:
            log_position(snapshot.position, current_price)

    def _report_order(self, order: Order):
        log_placed_order(order)
        self.notification_service.send_message(
            f"Ordem executada: {order.side.upper()} "
            f"{order.quantity:.8f} {self.symbol} @ "
            f"USD {order.price:.8f}"
        )

    async def _on_error(self, ex: Exception, backoff: float) -> float:
        """Trata um erro do loop e retorna o próximo backoff."""
        self.logger.error(f"ERROR: Erro no loop principal: {str(ex)}", exc_info=True)
        await asyncio.sleep(backoff)
        return min(backoff * 2, self.error_backoff_max)


def log_ticker(symbol: str, price: Decimal, pnl_summary: str | None = None):
    # o feed de preços da Jupiter cota em USD
    suffix = f" {pnl_summary}" if pnl_summary else ""
    bot_logger.debug(
        f"[blue]{symbol}[/blue] @ USD {price:.9f}.{suffix}",
        extra={"markup": True},
    )


def log_placed_order(order: Order):
    msg = f"[gray]{order.side.upper()}[/gray] {order.quantity:.8f} @ ${order.price:.8f} [gray]({order.order_id})[gray]"
    bot_logger.info(
        msg,
        extra={"markup": True},
    )
    bot_logger.debug(describe_costs(order.costs, order.sol_usd))


def log_position(position: Position, current_price: Decimal):
    pnl = (
        position.unrealized_pnl_percent(current_price)
        if position.exit_order is None
        else position.realized_pnl_percent
    )
    pnl_style = "green" if pnl > 0 else "red"
    pnl_str = f"[{pnl_style}]{pnl:.2f}%[/{pnl_style}]"

    bot_logger.debug(
        f"LONG {position.entry_order.quantity:.8f} @ ${position.entry_order.price:.8f}. PNL: {pnl_str}",
        extra={"markup": True},
    )
