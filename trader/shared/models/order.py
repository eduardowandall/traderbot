import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from trader.shared.models.costs import TradeCosts, costs_from_dict
from trader.shared.models.perp import PerpFill, perp_from_dict


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
    # perna de perp (A8): o lado, o tamanho e o colateral (ver `perp.py`)
    perp: PerpFill | None = None

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


# --- dict e JSON (o fio usa o dict; o ledger guarda o JSON) ------------------

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


def _jsonable(value: Any) -> Any:
    """Tipos JSON: decimais (e o resto) em string, datas em ISO."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return str(value)  # um `StrEnum` vira o valor dele
    return _json_default(value)


def order_to_dict(order: Order) -> dict:
    return _jsonable(asdict(order))


def order_to_json(order: Order) -> str:
    return json.dumps(order_to_dict(order), sort_keys=True)


def order_from_json(data: str) -> Order:
    return order_from_dict(json.loads(data))


def order_from_dict(data: dict) -> Order:
    raw = dict(data)
    for key in _DECIMAL_FIELDS:
        if raw.get(key) is not None:
            raw[key] = Decimal(raw[key])
    raw["costs"] = costs_from_dict(raw.get("costs"))
    raw["side"] = OrderSide(raw["side"])
    raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
    raw["perp"] = perp_from_dict(raw.get("perp"))
    return Order(**raw)
