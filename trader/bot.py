import time
import traceback
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .account import Account
from .api import MercadoBitcoinPublicAPI
from .colored_logger import get_trading_logger
from .trading_strategy import TradingStrategy


class TradingBot:
    def __init__(
        self, api: MercadoBitcoinPublicAPI, strategy: TradingStrategy, account: Account
    ):
        self.api = api
        self.strategy = strategy
        self.symbol = account.symbol
        self.is_running = False
        self.account = account

        # Rastreamento para análise de "hold strategy"
        self.first_position_entry_price: Optional[Decimal] = None
        self.first_position_quantity: Optional[Decimal] = None
        self.first_position_time: Optional[datetime] = None
        self.final_price: Optional[Decimal] = None

        # Configurar logging colorido
        self.trading_logger = get_trading_logger("TradingBot")
        self.logger = self.trading_logger.get_logger()

    def get_current_price(self) -> Decimal:
        """Obtém preço atual do par"""
        ticker = self.api.get_ticker(self.symbol)
        return Decimal(ticker.last)

    def run(self, interval: int = 60):
        self.is_running = True

        self.trading_logger.log_bot_start(self.symbol)

        while self.is_running:
            try:
                current_price = self.get_current_price()
                self.trading_logger.log_price(self.symbol, float(current_price))

                # Atualizar preço da posição atual
                self.account.update_position_price(current_price)

                position_signal = self.strategy.on_market_refresh(
                    current_price,
                    self.account.get_position(),
                    self.account.position_history,
                )

                if position_signal:
                    last_position = self.account.get_position()
                    success = self.account.place_order(
                        current_price,
                        position_signal.side,
                        position_signal.quantity,
                    )
                    self.trading_logger.log_position_signal(
                        position_signal.side, float(current_price)
                    )
                    if success:
                        position = self.account.get_position()
                        order_id = (
                            position.order_id
                            if position
                            else last_position.order_id
                            if last_position
                            else "N/A"
                        )
                        self.trading_logger.log_order_placed(
                            order_id,
                            position_signal.side,
                            float(position.entry_price)
                            if position
                            else float(current_price),
                            float(position_signal.quantity),
                        )
                        if position and position_signal.side == "buy":
                            # Rastrear primeira posição para análise de "hold strategy"
                            if self.first_position_entry_price is None:
                                self.first_position_entry_price = position.entry_price
                                self.first_position_quantity = position.quantity
                                self.first_position_time = position.entry_time
                                self.logger.info(
                                    "📌 Primeira posição registrada para análise de hold strategy"
                                )

                # Log de informações da conta
                position = self.account.get_position()
                if position:
                    self.trading_logger.log_position(
                        position.side,
                        float(position.quantity),
                        float(position.entry_price),
                    )

                # Log de PnL
                unrealized_pnl = self.account.get_unrealized_pnl()
                total_pnl = self.account.get_total_realized_pnl()
                self.trading_logger.log_pnl(float(unrealized_pnl), float(total_pnl))

                # Atualizar preço final para análise de hold strategy
                self.final_price = current_price

                time.sleep(interval)

            except KeyboardInterrupt:
                self.logger.info("🛑 Bot interrompido pelo usuário")
                self.stop()
            except Exception as e:
                self.trading_logger.log_error("Erro no loop principal", e)
                traceback.print_exc()
                time.sleep(interval)

    def show_execution_report(self):
        """Mostra relatório de execução"""
        self.logger.info("📊 ===== RELATÓRIO DE EXECUÇÃO =====")

        realized_pnl = self.account.get_total_realized_pnl()
        unrealized_pnl = self.account.get_unrealized_pnl()

        self.trading_logger.log_pnl(float(unrealized_pnl), float(realized_pnl))

        if len(self.strategy.price_history) > 1:
            price_variation = (
                self.strategy.price_history[-1] - self.strategy.price_history[0]
            )
            self.logger.info(f"📈 Variação do preço: R$ {price_variation:.2f}")

        # Mostrar histórico de posições
        if self.account.position_history:
            self.logger.info(
                f"📋 Total de operações realizadas: {len(self.account.position_history)}"
            )
            profitable_trades = sum(
                1 for pos in self.account.position_history if pos.realized_pnl > 0
            )
            self.logger.info(
                f"✅ Operações lucrativas: {profitable_trades}/{len(self.account.position_history)}"
            )

        # Análise de "Hold Strategy" - quanto teria ganhado mantendo a primeira posição
        self._show_hold_strategy_analysis()

        self.logger.info("📊 ===========================")

    def _show_hold_strategy_analysis(self):
        """Mostra análise de quanto teria ganhado com estratégia de hold"""
        if (
            self.first_position_entry_price is None
            or self.first_position_quantity is None
            or self.final_price is None
        ):
            self.logger.info("📊 Análise de Hold Strategy: Dados insuficientes")
            return

        # Calcular PnL se tivesse mantido a primeira posição
        hold_pnl = (
            self.final_price - self.first_position_entry_price
        ) * self.first_position_quantity

        # Calcular PnL real do bot
        actual_pnl = (
            self.account.get_total_realized_pnl() + self.account.get_unrealized_pnl()
        )

        # Calcular diferença
        difference = hold_pnl - actual_pnl

        # Calcular percentual de retorno
        hold_return_pct = (
            hold_pnl / (self.first_position_entry_price * self.first_position_quantity)
        ) * 100
        actual_return_pct = (
            (
                actual_pnl
                / (self.first_position_entry_price * self.first_position_quantity)
            )
            * 100
            if self.first_position_entry_price * self.first_position_quantity != 0
            else 0
        )

        self.logger.info("🔍 ===== ANÁLISE HOLD STRATEGY =====")
        self.logger.info(
            f"📌 Primeira posição: {self.first_position_quantity:.8f} @ R$ {self.first_position_entry_price:.2f}"
        )
        self.logger.info(f"💰 Preço inicial: R$ {self.first_position_entry_price:.2f}")
        self.logger.info(f"💰 Preço final: R$ {self.final_price:.2f}")

        # Log colorido baseado no resultado
        if hold_pnl > 0:
            self.logger.info(
                f"💰 PnL se tivesse mantido (HOLD): R$ {hold_pnl:.2f} ({hold_return_pct:+.2f}%)"
            )
        else:
            self.logger.info(
                f"💸 PnL se tivesse mantido (HOLD): R$ {hold_pnl:.2f} ({hold_return_pct:+.2f}%)"
            )

        if actual_pnl > 0:
            self.logger.info(
                f"💰 PnL real do bot (TRADING): R$ {actual_pnl:.2f} ({actual_return_pct:+.2f}%)"
            )
        else:
            self.logger.info(
                f"💸 PnL real do bot (TRADING): R$ {actual_pnl:.2f} ({actual_return_pct:+.2f}%)"
            )

        # Comparação final
        if difference > 0:
            self.logger.info(f"📈 HOLD teria sido MELHOR por R$ {difference:.2f}")
            self.logger.info(
                f"💡 Estratégia de hold teria superado o trading em {abs(difference):.2f} reais"
            )
        elif difference < 0:
            self.logger.info(f"📉 TRADING foi MELHOR por R$ {abs(difference):.2f}")
            self.logger.info(
                f"🎯 Bot superou a estratégia de hold em {abs(difference):.2f} reais"
            )
        else:
            self.logger.info("⚖️ EMPATE: Ambas estratégias tiveram o mesmo resultado")

        self.logger.info("🔍 ================================")

    def stop(self):
        """Para o bot"""
        self.is_running = False
        self.trading_logger.log_bot_stop()
        self.show_execution_report()
