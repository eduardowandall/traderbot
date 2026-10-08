"""JSON do protocolo trade-runner <-> strategy-runner (camada core: só dados).

Cada mensagem é um objeto JSON numa linha (ver `docs/plan.md` §3.3). Aqui
ficam só as conversões de `BucketSnapshot`, `OrderRequest`, `OrderReply` e
candles (`TickerData`, op `candles`) para dicts JSON e de volta; decimais
viajam como string, datas em ISO, e ordens pelo codec do `Order`
(`order_to_dict`).
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from trader.shared.models import Order, OrderSide, Position
from trader.shared.models.order import order_from_dict, order_to_dict
from trader.shared.models.public_data import TickerData
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    OrderRequest,
    ReplyStatus,
)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _order(order: Order | None) -> dict | None:
    return None if order is None else order_to_dict(order)


def _order_back(data: dict | None) -> Order | None:
    return None if data is None else order_from_dict(data)


def position_to_dict(position: Position | None) -> dict | None:
    if position is None:
        return None
    # o lado vai na ordem de entrada (`Order.perp`, A8)
    return {"entry": _order(position.entry_order), "exit": _order(position.exit_order)}


def position_from_dict(data: dict | None) -> Position | None:
    if data is None:
        return None
    entry = _order_back(data["entry"])
    assert entry is not None
    return Position(entry, _order_back(data.get("exit")))


def snapshot_to_dict(s: BucketSnapshot) -> dict:
    return {
        "bucket": s.bucket,
        "available": str(s.available),
        "quote_usd": _str(s.quote_usd),
        "position": position_to_dict(s.position),
        "realized_usd": str(s.realized_usd),
        "budget_usd": _str(s.budget_usd),
        "status": str(s.status),
        "pnl_summary": s.pnl_summary,
        "opened_at": _iso(s.opened_at),
        "last_exit_at": _iso(s.last_exit_at),
        "last_exit_price": _str(s.last_exit_price),
    }


def snapshot_from_dict(d: dict) -> BucketSnapshot:
    return BucketSnapshot(
        bucket=d["bucket"],
        available=Decimal(d["available"]),
        quote_usd=_dec(d.get("quote_usd")),
        position=position_from_dict(d["position"]),
        realized_usd=Decimal(d["realized_usd"]),
        budget_usd=_dec(d["budget_usd"]),
        status=BucketStatus(d["status"]),
        pnl_summary=d["pnl_summary"],
        opened_at=_dt(d["opened_at"]),
        last_exit_at=_dt(d["last_exit_at"]),
        last_exit_price=_dec(d["last_exit_price"]),
    )


def request_to_dict(r: OrderRequest) -> dict:
    return {
        "side": str(r.side),
        "quantity": str(r.quantity),
        "price": str(r.price),
        "rationale": r.rationale,
        "idempotency_key": r.idempotency_key,
    }


def request_from_dict(d: dict) -> OrderRequest:
    return OrderRequest(
        side=OrderSide(d["side"]),
        quantity=Decimal(d["quantity"]),
        price=Decimal(d["price"]),
        rationale=d.get("rationale"),
        idempotency_key=d.get("idempotency_key"),
    )


def reply_to_dict(r: OrderReply) -> dict:
    return {
        "status": str(r.status),
        "order": _order(r.order),
        "reasons": list(r.reasons),
        "error": r.error,
    }


def reply_from_dict(d: dict) -> OrderReply:
    return OrderReply(
        status=ReplyStatus(d["status"]),
        order=_order_back(d.get("order")),
        reasons=tuple(d.get("reasons", ())),
        error=d.get("error"),
    )


def candles_to_list(candles: list[TickerData]) -> list[dict]:
    return [
        {
            "timestamp": c.timestamp.isoformat(),
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "last": str(c.last),
        }
        for c in candles
    ]


def candles_from_list(items: list[dict]) -> list[TickerData]:
    return [
        TickerData(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            open=Decimal(d["open"]),
            high=Decimal(d["high"]),
            low=Decimal(d["low"]),
            last=Decimal(d["last"]),
        )
        for d in items
    ]


def encode(message: dict) -> bytes:
    """Uma mensagem: um objeto JSON e uma quebra de linha."""
    return (json.dumps(message, sort_keys=True) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("mensagem deve ser um objeto JSON")
    return message
