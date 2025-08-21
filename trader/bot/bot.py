import time
import traceback
from decimal import Decimal

from rich.console import Console
from rich.text import Text

from trader.bot.base_bot import BaseBot
from trader.models.order import Order
from trader.models.position import Position
from trader.models.public_data import TickerData

console = Console()


class TradingBot(BaseBot):
    def get_current_ticker(self) -> TickerData:
        ticker = self.api.get_ticker(self.symbol)
        return ticker

    def run(self, interval: int = 60):
        self.is_running = True
        while self.is_running:
            try:
                current_ticker = self.get_current_ticker()
                total_pnl = self.account.get_total_realized_pnl()
                log_ticker(self.symbol, current_ticker.last, total_pnl)

                order = self.process_market_data(current_ticker)
                if order:
                    log_placed_order(order)

                position = self.get_position()
                if position:
                    log_position(position, self.ticker_history[-1].last)

                time.sleep(interval)

            except KeyboardInterrupt:
                self.logger.warning("Bot interrompido pelo usuário")
                self.stop()
            except Exception as ex:
                self.logger.error(f"Erro no loop principal: {str(ex)}")
                traceback.print_exc()
                time.sleep(interval)

    def get_position(self):
        position = self.account.get_position()
        last_position = (
            self.account.position_history[-1] if self.account.position_history else None
        )
        if last_position != self.last_position:
            self.last_position = last_position
            position = last_position
        return position


def log_ticker(symbol: str, price: Decimal, total_pnl: Decimal):
    console.print(
        f"[blue]{symbol}[/blue] @ R$ {price:.2f}. Realized PNL: R$ {total_pnl:.2f}"
    )


def log_placed_order(order: Order):
    console.print(
        *[
            Text(
                order.side.upper(),
                style="bold red" if order.side == "sell" else "bold green",
            ),
            Text(f"{order.quantity:.8f} @ R$ {order.price:.2f}", style="bold white"),
            Text(f"({order.order_id})", style="dim blue"),
        ]
    )


def log_position(position: Position, current_price: Decimal):
    pnl = (
        position.unrealized_pnl(current_price)
        if position.exit_order is None
        else position.realized_pnl
    )
    pnl_style = "bold green" if pnl > 0 else "bold red"
    pnl_str = f"[{pnl_style}]R$ {pnl:.2f}[/{pnl_style}]"

    console.print(
        f"{position.type.name} {position.entry_order.quantity:.8f} @ R$ {position.entry_order.price:.2f}. PNL: {pnl_str}"
    )
