import logging
from datetime import datetime
from decimal import Decimal
from typing import List

from trader.models.order import Order

from .api.private_api import MercadoBitcoinPrivateAPIBase
from .models import OrderSide, Position, PositionType


class Account:
    """Classe responsável por gerenciar balanço, posições e execução de ordens"""

    def __init__(self, api: MercadoBitcoinPrivateAPIBase, symbol: str = "BTC-BRL"):
        self.api = api
        self.symbol = symbol
        self.account_id = self.get_api_account_id("BRL")
        self.current_position: Position | None = None
        self.position_history: List[Position] = []

        self.logger = logging.getLogger("Account")

    def get_api_account_id(self, currency: str) -> str:
        accounts = self.api.get_accounts()
        for account in accounts:
            if account.currency == currency:
                return account.id
        raise Exception(f"Conta para {currency} não encontrada")

    def get_balance(self, currency: str) -> Decimal:
        """Obtém saldo de uma moeda específica"""
        balances = self.api.get_account_balance(self.account_id)
        for balance in balances:
            if balance.symbol == currency:
                return balance.available
        return Decimal("0.0")

    def get_position(self) -> Position | None:
        """Retorna a posição atual"""
        return self.current_position

    def can_buy(self) -> bool:
        """Verifica se é possível executar uma compra"""
        # Não pode comprar se já tem posição long
        if (
            self.current_position is not None
            and self.current_position.type == PositionType.LONG
        ):
            return False
        # Verifica se tem saldo suficiente em BRL
        brl_balance = self.get_balance("BRL")
        return brl_balance > Decimal("50.0")  # Mínimo para operar

    def can_sell(self) -> bool:
        """Verifica se é possível executar uma venda"""
        # Só pode vender se tem posição long
        if (
            self.current_position is None
            or self.current_position.type != PositionType.LONG
        ):
            return False

        # Verifica se tem BTC suficiente
        btc_balance = self.get_balance("BTC")
        return btc_balance > Decimal("0.00001")  # Mínimo para vender

    def place_order(self, price: Decimal, side: OrderSide, quantity: Decimal) -> Order:
        if side == OrderSide.BUY and not self.can_buy():
            raise ValueError("Não é possível executar compra no momento")
        if side == OrderSide.SELL and not self.can_sell():
            raise ValueError("Não é possível executar compra no momento")

        try:
            order_id = self.api.place_order(
                account_id=self.account_id,
                symbol=self.symbol,
                side=str(side),
                type_order="market",
                quantity=str(quantity),
            )
            order = Order(
                order_id=order_id,
                symbol=self.symbol,
                quantity=quantity,
                price=price,
                side=side,
                timestamp=datetime.now(),
            )
            if not self.current_position:
                # Criar nova posição
                self.current_position = Position(
                    type=PositionType.LONG,
                    entry_order=order,
                    exit_order=None,
                )
                self.position_history.append(self.current_position)
            else:
                self.current_position.exit_order = order
                self.current_position = None

            return order

        except Exception as ex:
            self.logger.error(f"Erro ao executar ordem: {str(ex)}")
            raise

    def get_total_realized_pnl(self) -> Decimal:
        """Retorna o PnL total realizado"""
        return Decimal(str(sum(pos.realized_pnl for pos in self.position_history)))

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Retorna o PnL não realizado da posição atual"""
        if self.current_position:
            return self.current_position.unrealized_pnl(current_price)
        return Decimal("0.0")
