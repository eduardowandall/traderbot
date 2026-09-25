import asyncio
import logging
import time
import traceback
from datetime import UTC, datetime
from decimal import Decimal
from functools import cached_property

from solders.pubkey import Pubkey

from trader import logging_config
from trader.async_account import AsyncAccount
from trader.backtest.ticks import TickRecorder
from trader.execution import DuplicateIntentError, PolicyDeniedError
from trader.models import SOLANA_MINTS
from trader.models.bot_config import BotConfig, RunningMode
from trader.models.costs import describe_costs
from trader.models.order import Order
from trader.models.position import Position

bot_logger = logging.getLogger("bot")


class AsyncWebsocketTradingBot:
    def __init__(
        self,
        config: BotConfig,
        tick_recorder: TickRecorder | None = None,
    ):
        self.tick_recorder = tick_recorder
        logging_config.botname.set(f"{config.mode}-{config.name}-{config.currency}")
        self.last_position: Position | None = None
        self.is_running = False
        self.logger = logging.getLogger(self.__module__)
        self.logger.debug(
            f"start bot {config.mode}-{config.name}-{config.currency} with config: {str(config)}"
        )
        bot_logger.debug(f"start bot {config.mode}-{config.name}-{config.currency}")

        self.input_mint = Pubkey.from_string(config.input_mint)
        self.output_mint = Pubkey.from_string(config.output_mint)

        self.mode = config.mode
        self.strategy = config.strategy
        self.account = AsyncAccount(
            config.provider,
            self.input_mint,
            self.output_mint,
            gateway=config.gateway,
            account_id=f"{config.mode}:{config.currency}",
            source=config.name,
        )
        self.notification_service = config.notifier

        self.total_pnl = Decimal("0.0")

        self.stop_when_error = False
        # espera entre iterações após erro; cresce até error_backoff_max
        self.error_backoff_initial = 1.0
        self.error_backoff_max = 60.0
        # após uma recusa da política, não tenta novas ordens por um tempo:
        # a mesma recusa a cada tick só encheria o ledger (a estratégia segue
        # recebendo os preços normalmente)
        self.denial_cooldown = 30.0
        self.monotonic = time.monotonic
        self._orders_paused_until = 0.0

    async def process_market_data(self, current_price):
        _bal = await self.account.get_balance(self.input_mint)
        _pos = self.account.get_position()
        position_signal = self.strategy.on_market_refresh(
            current_price,
            None,  # não vem no websocket
            _bal,
            _pos,
        )
        order = None
        if position_signal and self.monotonic() >= self._orders_paused_until:
            order = await self.account.place_order(
                current_price,
                position_signal.side,
                position_signal.quantity,
            )
            # da tempo da wallet atualizar a operacao feita.
            await asyncio.sleep(2.0)
        return order

    def stop(self):
        """Para o bot"""
        self.is_running = False

    def run(self, **kwargs):
        asyncio.run(self._run())

    @cached_property
    def symbol(self):
        return f"{SOLANA_MINTS[self.output_mint].symbol}-{SOLANA_MINTS[self.input_mint].symbol}"

    async def _run(self):
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
        try:
            await self.account.provider.aclose()
        except Exception as ex:
            self.logger.warning(f"Erro ao encerrar conexões: {ex}")

    async def _loop(self):
        await self._startup()
        backoff = self.error_backoff_initial
        while self.is_running:
            try:
                await self._tick()
                backoff = self.error_backoff_initial
            except KeyboardInterrupt:
                self.logger.warning("Bot interrompido pelo usuário")
                self.notification_service.send_message("Bot interrompido pelo usuário")
                self.is_running = False
            except Exception as ex:
                backoff = await self._on_error(ex, backoff)

    async def _startup(self):
        self.is_running = True
        self.account.restore_from_ledger()
        if self.mode != RunningMode.DRY:
            # em dry a carteira real não reflete as ordens simuladas
            await self.account.reconcile_position()
        self.strategy.setup(await self.account.get_candles(self.output_mint))
        self.notification_service.send_message(f"Bot iniciado para {self.symbol}")

    async def _tick(self):
        current_price = await self.account.get_price(self.output_mint)
        if self.tick_recorder is not None:
            self.tick_recorder.record(datetime.now(UTC), current_price)
        log_ticker(self.symbol, current_price, self.account.pnl_summary())

        order = await self.process_market_data(current_price)
        if order:
            self._report_order(order)

        position = self.account.get_position()
        if position:
            log_position(position, current_price)

    def _report_order(self, order: Order):
        log_placed_order(order)
        self.notification_service.send_message(
            f"Ordem executada: {order.side.upper()} "
            f"{order.quantity:.8f} {self.symbol} @ "
            f"USD {order.price:.8f}"
        )

    async def _on_error(self, ex: Exception, backoff: float) -> float:
        """Trata um erro do loop e retorna o próximo backoff."""
        if isinstance(ex, PolicyDeniedError | DuplicateIntentError):
            # recusa esperada: não é erro do bot, não aplica backoff
            self.logger.warning(f"{ex} (novas ordens em {self.denial_cooldown:.0f}s)")
            self._orders_paused_until = self.monotonic() + self.denial_cooldown
            return backoff

        self.logger.error(f"ERROR: Erro no loop principal: {str(ex)}", exc_info=True)
        traceback.print_exc()
        if self.stop_when_error:
            self.is_running = False
            return backoff
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
    bot_logger.debug(
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
        f"{position.type.name} {position.entry_order.quantity:.8f} @ ${position.entry_order.price:.8f}. PNL: {pnl_str}",
        extra={"markup": True},
    )
