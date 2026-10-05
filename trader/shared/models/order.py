import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from trader.shared.models.costs import TradeCosts, costs_from_dict


class OrderSide(StrEnum):
    BUY = auto()
    SELL = auto()


@dataclass
class OrderSignal:
    side: OrderSide
    quantity: Decimal
    # por que a estratégia sinalizou (ex: condições que dispararam); vai para
    # o `rationale` da intenção no ledger
    rationale: str | None = None


@dataclass
class SwapResult:
    """Resultado de um swap executado (valores raw, conforme a quote)."""

    signature: str
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    # custos já conhecidos na execução (paper/backtest); em real são
    # buscados depois, por `fetch_swap_costs`
    costs: TradeCosts | None = field(default=None, compare=False)
    # quote usada (LP fees, impacto)
    quote: Any = field(default=None, compare=False, repr=False)
    # tentativas anteriores que a rede confirmou como falhas (taxa paga)
    failed_signatures: tuple[str, ...] = field(default=(), compare=False)


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
    # vendas: False quando a venda foi parcial e o resto segue aberto
    # (ordens antigas, sem o campo, sempre fecharam a posição)
    closes_position: bool = True

    @property
    def quote_price(self) -> Decimal:
        """Preço no token de cotação (o que a estratégia vê); `price` é USD.

        Iguais em pares USDC/USDT. Ordens sem `fill_price` usam `price`.
        """
        return self.price if self.fill_price is None else self.fill_price

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


# --- JSON (ledger, protocolo de fio) ----------------------------------------

_DECIMAL_FIELDS = (
    "quantity",
    "price",
    "requested_quantity",
    "requested_price",
    "fill_price",
    "quote_amount",
    "quote_usd",
    "sol_usd",
    "sol_in_quote",
)


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def order_to_json(order: Order) -> str:
    return json.dumps(asdict(order), default=_json_default, sort_keys=True)


def order_from_json(data: str) -> Order:
    raw = json.loads(data)
    for key in _DECIMAL_FIELDS:
        if raw.get(key) is not None:
            raw[key] = Decimal(raw[key])
    raw["costs"] = costs_from_dict(raw.get("costs"))
    raw["side"] = OrderSide(raw["side"])
    raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
    return Order(**raw)
