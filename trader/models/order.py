from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from trader.models.costs import TradeCosts


class OrderSide(StrEnum):
    BUY = auto()
    SELL = auto()


@dataclass
class OrderSignal:
    side: OrderSide
    quantity: Decimal


@dataclass
class SwapResult:
    """Resultado de um swap executado (valores raw, conforme a quote)."""

    signature: str
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    # custos já conhecidos na execução (paper/backtest); em real/dry são
    # buscados depois, por `fetch_swap_costs`
    costs: TradeCosts | None = field(default=None, compare=False)
    # quote usada (LP fees, impacto) e a mensagem assinada (taxa em dry run)
    quote: Any = field(default=None, compare=False, repr=False)
    message: Any = field(default=None, compare=False, repr=False)


@dataclass
class Order:
    order_id: str
    input_mint: str
    output_mint: str
    quantity: Decimal
    price: Decimal
    side: OrderSide
    timestamp: datetime
    # o que a estratégia pediu (price do feed, em USD)
    requested_quantity: Decimal | None = None
    requested_price: Decimal | None = None
    # preço do fill em unidades do input_mint por token (ex: SOL por USDC)
    fill_price: Decimal | None = None
    # unidades nativas (fonte da verdade do PnL): token de cotação (input_mint)
    # efetivamente gasto (compra) ou recebido (venda)
    quote_amount: Decimal | None = None
    # taxas do próprio trade para as estimativas
    quote_usd: Decimal | None = None  # USD por unidade do token de cotação
    sol_usd: Decimal | None = None  # USD por SOL
    sol_in_quote: Decimal | None = None  # token de cotação por SOL
    costs: TradeCosts | None = None

    def __eq__(self, value):
        return (
            value
            and self.order_id == value.order_id
            and self.input_mint == value.input_mint
            and self.output_mint == value.output_mint
            and self.quantity == value.quantity
            and self.price == value.price
            and self.side == value.side
            and self.timestamp == value.timestamp
        )
