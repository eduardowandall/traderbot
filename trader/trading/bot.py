import time
import traceback
from decimal import Decimal

from trader.base_bot import BaseBot


class TradingBot(BaseBot):
    """Bot para trading em tempo real"""

    def get_current_price(self) -> Decimal:
        """Obtém preço atual do par"""
        ticker = self.api.get_ticker(self.symbol)
        return Decimal(ticker.last)

    def run(self, interval: int = 60):
        """Executa o bot de trading em tempo real"""
        self.is_running = True
        self.trading_logger.log_bot_start(self.symbol)

        while self.is_running:
            try:
                current_price = self.get_current_price()
                self.trading_logger.log_price(self.symbol, float(current_price))

                # Usar método da classe base para processar dados de mercado
                self.process_market_data(current_price)

                time.sleep(interval)

            except KeyboardInterrupt:
                self.logger.info("🛑 Bot interrompido pelo usuário")
                self.stop()
            except Exception as e:
                self.trading_logger.log_error("Erro no loop principal", e)
                traceback.print_exc()
                time.sleep(interval)
