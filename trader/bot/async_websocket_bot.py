import asyncio
import logging
import traceback
from decimal import Decimal
from functools import cached_property

from rich.console import Console
from solders.pubkey import Pubkey

# import logging_config  # pyright: ignore[reportMissingImports]
from trader.async_account import AsyncAccount
from trader.models import SOLANA_MINTS
from trader.models.bot_config import BotConfig
from trader.models.order import Order
from trader.models.position import Position

console = Console()


class AsyncWebsocketTradingBot:
    def __init__(
        self,
        config: BotConfig,
    ):
        self.last_position: Position | None = None
        self.is_running = False
        self.logger = logging.getLogger(self.__module__)
        self.logger.debug(
            f"start bot {config.name}-{config.currency} with config: {str(config)}"
        )

        self.input_mint = Pubkey.from_string(config.input_mint)
        self.output_mint = Pubkey.from_string(config.output_mint)

        self.strategy = config.strategy
        self.account = AsyncAccount(
            config.provider,  # type: ignore
            self.input_mint,
            self.output_mint,
        )
        self.notification_service = config.notifier

        self.total_pnl = Decimal("0.0")

        self.stop_when_error = False

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
        if position_signal:
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
        self.is_running = True
        asyncio.run(self._run())

    @cached_property
    def symbol(self):
        return f"{SOLANA_MINTS[self.output_mint].symbol}-{SOLANA_MINTS[self.input_mint].symbol}"

    async def _run(self):
        self.strategy.setup(await self.account.get_candles(self.output_mint))
        should_stop = False
        self.notification_service.send_message(f"Bot iniciado para {self.symbol}")

        has_error = False
        while not should_stop and not (self.stop_when_error and has_error):
            try:
                current_price = await self.account.get_price(self.output_mint)
                log_ticker(
                    self.symbol,
                    current_price,
                    self.account.get_total_realized_pnl(),
                )

                order = await self.process_market_data(current_price)
                if order:
                    log_placed_order(order)
                    self.notification_service.send_message(
                        f"Ordem executada: {order.side.upper()} "
                        f"{order.quantity:.8f} {self.symbol} @ "
                        f"{self.symbol.split('-')[1]} {order.price:.2f}"
                    )

                position = self.account.get_position()
                if position:
                    log_position(position, current_price)

            except KeyboardInterrupt:
                self.logger.warning("Bot interrompido pelo usuário")
                self.notification_service.send_message("Bot interrompido pelo usuário")
                should_stop = True
                return

            except Exception as ex:
                self.logger.error(
                    f"ERROR: Erro no loop principal: {str(ex)}", exc_info=True
                )
                traceback.print_exc()
                has_error = True


bot_logger = logging.getLogger("bot")


def log_ticker(symbol: str, price: Decimal, realized_pnl: Decimal | None = None):
    fiat_symbol = symbol.split("-")[1]
    if realized_pnl is not None:
        bot_logger.info(
            f"[blue]{symbol}[/blue] @ {fiat_symbol} {price:.9f}. PNL Realizado: R$ {realized_pnl:.9f}",
            extra={"markup": True},
        )
    else:
        bot_logger.info(
            f"[blue]{symbol}[/blue] @ {fiat_symbol} {price:.9f}.",
            extra={"markup": True},
        )


def log_placed_order(order: Order):
    msg = f"[gray]{order.side.upper()}[/gray] {order.quantity:.8f} @ ${order.price:.8f} [gray]({order.order_id})[gray]"
    bot_logger.info(
        msg,
        extra={"markup": True},
    )


def log_position(position: Position, current_price: Decimal):
    pnl = (
        position.unrealized_pnl_percent(current_price)
        if position.exit_order is None
        else position.realized_pnl_percent
    )
    pnl_style = "green" if pnl > 0 else "red"
    pnl_str = f"[{pnl_style}]{pnl:.2f}%[/{pnl_style}]"

    bot_logger.info(
        f"{position.type.name} {position.entry_order.quantity:.8f} @ ${position.entry_order.price:.8f}. PNL: {pnl_str}",
        extra={"markup": True},
    )
