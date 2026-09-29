"""Spec declarativa de estratégia (JSON, versão 1).

É o que um agente escreve: blocos prontos (condições, stop, sizing) com
parâmetros limitados, nunca código. `StrategySpec.model_json_schema()` é o
schema entregue ao agente (`main.py strategy schema`).

Cada condição tem um `type` (discriminador), `lookback()` (quantas barras
precisa para ter valor) e `label()` (texto curto usado no `rationale`). Uma
condição `expr` futura entra como mais um membro das uniões abaixo.
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trader.indicators import to_utc
from trader.models.public_data import Interval

SPEC_VERSION = 1

Window = Annotated[int, Field(ge=2, le=500)]
Pct = Annotated[Decimal, Field(gt=0, le=50)]
Margin = Annotated[Decimal, Field(ge=0, le=50)]
RsiLevel = Annotated[Decimal, Field(ge=1, le=99)]
Usd = Annotated[Decimal, Field(gt=0)]
MaKind = Literal["sma", "ema", "wma"]


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def lookback(self) -> int:
        """Barras necessárias para a condição ter valor."""
        return 1

    def label(self) -> str:
        return self.type  # type: ignore[attr-defined]


# --- condições de mercado (entrada e saída) -----------------------------------


class _Rsi(_Block):
    period: Window = 14
    value: RsiLevel

    def lookback(self) -> int:
        return self.period + 1


class RsiBelow(_Rsi):
    type: Literal["rsi_below"]

    def label(self) -> str:
        return f"rsi{self.period}<{self.value}"


class RsiAbove(_Rsi):
    type: Literal["rsi_above"]

    def label(self) -> str:
        return f"rsi{self.period}>{self.value}"


class _PriceVsMa(_Block):
    ma: MaKind = "sma"
    window: Window
    pct: Margin = Decimal(0)

    def lookback(self) -> int:
        return self.window


class PriceBelowMa(_PriceVsMa):
    type: Literal["price_below_ma"]

    def label(self) -> str:
        return f"price<{self.ma}{self.window}-{self.pct}%"


class PriceAboveMa(_PriceVsMa):
    type: Literal["price_above_ma"]

    def label(self) -> str:
        return f"price>{self.ma}{self.window}+{self.pct}%"


class _MaVsMa(_Block):
    ma: MaKind = "sma"
    fast: Window
    slow: Window

    @model_validator(mode="after")
    def _fast_before_slow(self):
        if self.fast >= self.slow:
            raise ValueError("fast precisa ser menor que slow")
        return self

    def lookback(self) -> int:
        return self.slow


class FastMaAboveSlow(_MaVsMa):
    type: Literal["fast_ma_above_slow"]

    def label(self) -> str:
        return f"{self.ma}{self.fast}>{self.ma}{self.slow}"


class FastMaBelowSlow(_MaVsMa):
    type: Literal["fast_ma_below_slow"]

    def label(self) -> str:
        return f"{self.ma}{self.fast}<{self.ma}{self.slow}"


class DipFromHigh(_Block):
    type: Literal["dip_from_high"]
    window: Window
    pct: Pct

    def lookback(self) -> int:
        return self.window

    def label(self) -> str:
        return f"dip{self.pct}%_from_high{self.window}"


class PriceBelow(_Block):
    type: Literal["price_below"]
    value: Usd

    def label(self) -> str:
        return f"price<{self.value}"


class PriceAbove(_Block):
    type: Literal["price_above"]
    value: Usd

    def label(self) -> str:
        return f"price>{self.value}"


class VolatilityBelow(_Block):
    type: Literal["volatility_below"]
    window: Window
    pct: Pct

    def lookback(self) -> int:
        return self.window + 1

    def label(self) -> str:
        return f"vol{self.window}<{self.pct}%"


# --- condições de posição (só saída) ------------------------------------------


class TakeProfit(_Block):
    type: Literal["take_profit"]
    pct: Pct

    def label(self) -> str:
        return f"take_profit{self.pct}%"


class MaxHold(_Block):
    type: Literal["max_hold"]
    minutes: Annotated[int, Field(ge=1, le=43200)]

    def label(self) -> str:
        return f"max_hold{self.minutes}m"


class StopLoss(_Block):
    type: Literal["stop_loss"]
    pct: Pct

    def label(self) -> str:
        return f"stop_loss{self.pct}%"


class TrailingStop(_Block):
    type: Literal["trailing_stop"]
    pct: Pct

    def label(self) -> str:
        return f"trailing_stop{self.pct}%"


_MARKET = (
    RsiBelow
    | RsiAbove
    | PriceBelowMa
    | PriceAboveMa
    | FastMaAboveSlow
    | FastMaBelowSlow
    | DipFromHigh
    | PriceBelow
    | PriceAbove
    | VolatilityBelow
)
MarketCondition = Annotated[_MARKET, Field(discriminator="type")]
ExitCondition = Annotated[_MARKET | TakeProfit | MaxHold, Field(discriminator="type")]
Stop = Annotated[StopLoss | TrailingStop, Field(discriminator="type")]
Mode = Literal["all", "any"]


class Entry(_Block):
    mode: Mode = "all"
    conditions: Annotated[list[MarketCondition], Field(min_length=1, max_length=8)]


class Exit(_Block):
    # o stop é obrigatório e sempre combinado com OU: `mode` vale só para
    # `conditions`, então `mode: all` nunca desliga a proteção
    stop: Stop
    mode: Mode = "any"
    conditions: Annotated[list[ExitCondition], Field(max_length=8)] = []


class FixedUsd(_Block):
    """Cada compra gasta `usd` (ou o saldo disponível, se menor)."""

    type: Literal["fixed_usd"]
    usd: Usd


class StrategySpec(_Block):
    version: Literal[1]
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    agent_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    rationale: Annotated[str, Field(max_length=2000)] = ""
    # OUTPUT-INPUT, como na CLI: SOL-USDC compra SOL gastando USDC
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,16}-[A-Z0-9]{1,16}$")]
    timeframe: Interval
    entry: Entry
    exit: Exit
    sizing: FixedUsd
    budget_usd: Usd
    max_loss_usd: Usd
    cooldown_minutes: Annotated[int, Field(ge=0, le=10080)] = 0
    expires_at: AwareDatetime
    supersedes: Annotated[str | None, Field(pattern=r"^[0-9a-f]{12}$")] = None

    @model_validator(mode="after")
    def _loss_within_budget(self):
        if self.max_loss_usd > self.budget_usd:
            raise ValueError("max_loss_usd não pode passar de budget_usd")
        return self

    def conditions(self) -> list[_Block]:
        return [*self.entry.conditions, self.exit.stop, *self.exit.conditions]

    def lookback(self) -> int:
        """Barras de aquecimento: o maior lookback entre as condições."""
        return max(c.lookback() for c in self.conditions())

    def canonical_json(self) -> str:
        """JSON canônico (chaves ordenadas, decimais normalizados, UTC)."""
        data = _canonical(self.model_dump(mode="python"))
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def spec_id(self) -> str:
        """Id pelo conteúdo: a mesma spec (mesmo com `3` vs `3.0`) gera o mesmo id."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:12]


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return _SCALARS.get(type(value), lambda v: v)(value)


_SCALARS = {
    Decimal: lambda d: format(d.normalize(), "f"),
    datetime: lambda d: to_utc(d).isoformat(),
    Interval: str,
}
