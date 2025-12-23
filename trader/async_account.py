import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider

from .models import Order, OrderSide, Position, PositionType, TickerData


class AsyncAccount:
    """Classe responsável por gerenciar balanço, posições e execução de ordens"""

    def __init__(
        self, provider: AsyncJupiterProvider, input_mint: Pubkey, output_mint: Pubkey
    ):
        self.provider = provider
        self.input_mint = input_mint
        self.output_mint = output_mint

        self.current_position: Position | None = None
        self.logger = logging.getLogger(self.__module__)
        self.total_pnl = Decimal("0.0")

        self.balances = None
        self.balances_last_update = datetime.min
        self.position_last_update = datetime.min

    def __repr__(self):
        return f"{self.__class__.__name__} for {self.input_mint=} and {self.output_mint=} with current_position on {self.current_position}"

    async def get_price(self, mint: Pubkey) -> Decimal:
        return await self.provider.get_price_ticker_data(mint)

    async def get_candles(self, mint: Pubkey) -> list[TickerData]:
        return await self.provider.get_candles(mint)

    async def get_balance(self, mint: Pubkey) -> Decimal:
        # cachezinho babaca pra não ficar comendo token do RPC
        # temporario até achar um jeito mais eficiente
        if (
            not self.balances
            or self.balances_last_update < datetime.now() - timedelta(minutes=3)
            or self.position_last_update > self.balances_last_update
        ):
            self.balances = await self.provider.get_account_balance()
            self.balances_last_update = datetime.now()
        for balance in self.balances:
            if balance.mint == mint:
                self.logger.debug(
                    f"get_balance: {str(balance.mint)=} {balance.available=}"
                )
                return balance.available
        return Decimal("0.0")

    def get_position(self) -> Position | None:
        """Retorna a posição atual"""
        return self.current_position

    async def can_buy(self):
        """Verifica se é possível executar uma compra"""
        # Não pode comprar se já tem posição long
        if (
            self.current_position is not None
            and self.current_position.type == PositionType.LONG
        ):
            raise ValueError(
                "Não é possível executar compra no momento. Já existe posicão"
            )

        balance = await self.get_balance(self.input_mint)
        self.logger.debug(f"can_buy: input_mint={str(self.input_mint)} {balance=}")
        if balance < Decimal("0.01"):  # Mínimo para operar
            raise ValueError(
                "Não é possível executar compra no momento. Sem valor minimo"
            )

    async def can_sell(self):
        """Verifica se é possível executar uma venda"""
        # Só pode vender se tem posição long
        if (
            self.current_position is None
            or self.current_position.type != PositionType.LONG
        ):
            raise ValueError(
                "Não é possível executar venda no momento. Sem posicão de compra"
            )

        balance = await self.get_balance(self.output_mint)
        self.logger.debug(f"can_sell: output_mint={str(self.output_mint)} {balance=}")
        if balance < Decimal("0.00001"):  # Mínimo para vender
            raise ValueError(
                "Não é possível executar venda no momento. Sem valor minimo"
            )

    async def place_order(
        self, price: Decimal, side: OrderSide, quantity: Decimal
    ) -> Order:
        if side == OrderSide.BUY:
            return await self.buy(price, quantity)
        if side == OrderSide.SELL:
            return await self.sell(price, quantity)

        raise ValueError("Invalid Order Side.")

    async def buy(self, price: Decimal, quantity: Decimal) -> Order:
        await self.can_buy()

        try:
            order_id = await self.provider.buy(
                self.input_mint,
                self.output_mint,
                type_order="market",
                quantity=quantity,
                price=price,
            )
            order = Order(
                order_id=order_id,
                input_mint=str(self.input_mint),
                output_mint=str(self.output_mint),
                quantity=quantity,
                price=price,
                side=OrderSide.BUY,
                timestamp=datetime.now(),
            )
            self.logger.debug(f"ORDER PLACED: {asdict(order)}", extra=asdict(order))
            # Criar nova posição
            self.current_position = Position(
                type=PositionType.LONG,
                entry_order=order,
                exit_order=None,
            )
            self.position_last_update = datetime.now()
            return order

        except Exception as ex:
            self.logger.error(f"Erro ao executar ordem: {str(ex)}")
            raise

    async def sell(self, price: Decimal, quantity: Decimal) -> Order:
        await self.can_sell()

        try:
            order_id = await self.provider.sell(
                self.input_mint,
                self.output_mint,
                type_order="market",
                quantity=quantity,
            )
            order = Order(
                order_id=order_id,
                input_mint=str(self.input_mint),
                output_mint=str(self.output_mint),
                quantity=quantity,
                price=price,
                side=OrderSide.SELL,
                timestamp=datetime.now(),
            )
            self.logger.debug(
                f"ORDER PLACED: order={asdict(order)} position={asdict(self.current_position) if self.current_position else ''}",
                extra=asdict(order),
            )
            assert self.current_position
            self.current_position.exit_order = order
            self.total_pnl += self.current_position.realized_pnl
            self.current_position = None
            self.position_last_update = datetime.now()

            return order

        except Exception as ex:
            self.logger.error(f"Erro ao executar ordem: {str(ex)}")
            raise

    def get_total_realized_pnl(self) -> Decimal:
        return self.total_pnl

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Retorna o PnL não realizado da posição atual"""
        if self.current_position:
            return self.current_position.unrealized_pnl(current_price)
        return Decimal("0.0")
