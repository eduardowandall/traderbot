import traceback
from datetime import datetime
from decimal import Decimal

from trader.base_bot import BaseBot
from trader.models.public_data import Candles


class BacktestingBot(BaseBot):
    """Bot para backtesting com dados históricos"""

    INTERVAL_TO_RESOLUTION = {
        60: "1m",
        900: "15m",
        3600: "1h",
        10800: "3h",
        86400: "1d",
        604800: "1w",
        2592000: "1M",
    }

    def get_historical_prices(
        self, start_date: datetime, end_date: datetime, resolution: str
    ) -> Candles:
        return self.api.get_candles(self.symbol, start_date, end_date, resolution)

    def run(self, start_date: datetime, end_date: datetime, interval: int = 60):
        """Executa o backtesting com dados históricos"""
        self.is_running = True

        candles = self.get_historical_prices(
            start_date, end_date, self.INTERVAL_TO_RESOLUTION[interval]
        )

        for index, str_price in enumerate(candles.c):
            current_price = Decimal(str_price)
            try:
                timestamp = datetime.fromtimestamp(candles.t[index])

                # Usar método da classe base para processar dados de mercado
                self.process_market_data(current_price, timestamp)

            except KeyboardInterrupt:
                self.logger.info("🛑 Bot interrompido pelo usuário")
                self.stop()
            except Exception as e:
                self.trading_logger.log_error("Erro no loop principal", e)
                traceback.print_exc()

        self.logger.info("📈 simulação finalizada")
        self.stop()
