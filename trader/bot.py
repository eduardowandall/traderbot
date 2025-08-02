import time
import traceback
from datetime import datetime
from decimal import Decimal

from report.report_method import BaseReport

from .account import Account
from .api import MercadoBitcoinPublicAPI
from .colored_logger import get_trading_logger
from .models import Position
from .trading_strategy import TradingStrategy


class TradingBot:
    def __init__(
        self,
        api: MercadoBitcoinPublicAPI,
        strategy: TradingStrategy,
        report: BaseReport,
        account: Account,
    ):
        self.api = api
        self.strategy = strategy
        self.symbol = account.symbol
        self.is_running = False
        self.account = account
        self.report = report

        self.price_history: list[Decimal] = []
        self.last_position: Position | None = None
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
                self.price_history.append(current_price)
                self.trading_logger.log_price(self.symbol, float(current_price))

                position_signal = self.strategy.on_market_refresh(
                    current_price,
                    self.account.get_position(),
                    self.account.position_history,
                )

                if position_signal:
                    order = self.account.place_order(
                        current_price,
                        position_signal.side,
                        position_signal.quantity,
                    )
                    self.trading_logger.log_order_placed(
                        order.order_id,
                        order.side,
                        order.price,
                        order.quantity,
                    )

                # Log de informações da conta
                position = self.account.get_position()
                if position:
                    self.trading_logger.log_position(
                        position.type,
                        float(position.entry_order.quantity),
                        float(position.entry_order.price),
                    )
                elif self.account.position_history:
                    # Log colorido baseado no resultado
                    last_position = self.account.position_history[-1]
                    if last_position != self.last_position:
                        self.last_position = last_position
                        realized_pnl = last_position.realized_pnl
                        if realized_pnl > 0:
                            self.logger.info(
                                f"💰 Posição fechada com LUCRO - PnL: R$ {realized_pnl:.2f}"
                            )
                        else:
                            self.logger.info(
                                f"💸 Posição fechada com PREJUÍZO - PnL: R$ {realized_pnl:.2f}"
                            )

                # Log de PnL
                if position:
                    unrealized_pnl = position.unrealized_pnl(current_price)
                    self.trading_logger.log_unrealized_pnl(float(unrealized_pnl))
                total_pnl = self.account.get_total_realized_pnl()
                self.trading_logger.log_realized_pnl(float(total_pnl))

                # Salvar dados da iteração
                self.report.save_iteration_data(
                    timestamp=datetime.now(),
                    symbol=self.symbol,
                    current_price=current_price,
                    position=self.account.get_position(),
                    unrealized_pnl=unrealized_pnl,
                    realized_pnl=total_pnl,
                    position_signal=position_signal.side if position_signal else None,
                )

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
        unrealized_pnl = self.account.get_unrealized_pnl(self.price_history[-1])

        self.trading_logger.log_unrealized_pnl(float(unrealized_pnl))
        self.trading_logger.log_realized_pnl(float(realized_pnl))

        if len(self.price_history) > 1:
            price_variation = self.price_history[-1] - self.price_history[0]
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

        if self.account.position_history:
            first_position_entry_price = self.account.position_history[
                0
            ].entry_order.price
            first_position_quantity = self.account.position_history[
                0
            ].entry_order.quantity
            final_price = self.price_history[-1]
        else:
            self.logger.info("📊 Análise de Hold Strategy: Dados insuficientes")
            return

        # Calcular PnL se tivesse mantido a primeira posição
        hold_pnl = (final_price - first_position_entry_price) * first_position_quantity

        # Calcular PnL real do bot
        actual_pnl = (
            self.account.get_total_realized_pnl()
            + self.account.get_unrealized_pnl(final_price)
        )

        # Calcular diferença
        difference = hold_pnl - actual_pnl

        # Calcular percentual de retorno
        hold_return_pct = (
            hold_pnl / (first_position_entry_price * first_position_quantity)
        ) * 100
        actual_return_pct = (
            (actual_pnl / (first_position_entry_price * first_position_quantity)) * 100
            if first_position_entry_price * first_position_quantity != 0
            else 0
        )

        self.logger.info("🔍 ===== ANÁLISE HOLD STRATEGY =====")
        self.logger.info(
            f"📌 Primeira posição: {first_position_quantity:.8f} @ R$ {first_position_entry_price:.2f}"
        )
        self.logger.info(f"💰 Preço inicial: R$ {first_position_entry_price:.2f}")
        self.logger.info(f"💰 Preço final: R$ {final_price:.2f}")

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
