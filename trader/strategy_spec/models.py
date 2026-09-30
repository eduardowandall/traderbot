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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

from trader.indicators import to_utc
from trader.models.public_data import Interval

Window = Annotated[int, Field(ge=2, le=500)]
# o schema publica número com limites e unidade (a validação ainda aceita
# string, para decimais exatos): sem isso o ramo "string" não tinha limites
Pct = Annotated[
    Decimal,
    Field(gt=0, le=50),
    WithJsonSchema(
        {"type": "number", "exclusiveMinimum": 0, "maximum": 50, "description": "%"}
    ),
]
Margin = Annotated[
    Decimal,
    Field(ge=0, le=50),
    WithJsonSchema({"type": "number", "minimum": 0, "maximum": 50, "description": "%"}),
]
RsiLevel = Annotated[
    Decimal,
    Field(ge=1, le=99),
    WithJsonSchema(
        {"type": "number", "minimum": 1, "maximum": 99, "description": "nível do RSI"}
    ),
]
Usd = Annotated[
    Decimal,
    Field(gt=0),
    WithJsonSchema({"type": "number", "exclusiveMinimum": 0, "description": "USD"}),
]
Chance = Annotated[
    int,
    Field(ge=1, le=100),
    WithJsonSchema(
        {"type": "integer", "minimum": 1, "maximum": 100, "description": "%"}
    ),
]
MaKind = Literal["sma", "ema", "wma"]
# EMA/RSI são recursivos: com 5x o período, o valor inicial já não pesa
SEED_FACTOR = 5
# campos que não mudam o comportamento: fora do id da spec
METADATA_FIELDS = {"name", "agent_id", "rationale", "supersedes"}
# limite prático da API de candles para o aquecimento
MAX_HISTORY = 900  # abaixo do limite de candles (1000, menos a barra em formação)
# o RSI de Wilder suaviza com 1/n: converge mais devagar que a EMA
RSI_SEED_FACTOR = 10


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def lookback(self) -> int:
        """Barras necessárias para a condição ter valor."""
        return 1

    def history(self) -> int:
        """Barras para o valor convergir (EMA/RSI precisam de bem mais)."""
        return self.lookback()

    def label(self) -> str:
        return self.type  # type: ignore[attr-defined]


# --- condições de mercado (entrada e saída) -----------------------------------


class _Rsi(_Block):
    period: Window = 14
    value: RsiLevel

    def lookback(self) -> int:
        return self.period + 1

    def history(self) -> int:
        return self.period * RSI_SEED_FACTOR + 1  # Wilder: suavização 1/n


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

    def history(self) -> int:
        return self.window * (SEED_FACTOR if self.ma == "ema" else 1)


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

    def history(self) -> int:
        return self.slow * (SEED_FACTOR if self.ma == "ema" else 1)


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


class ReboundFromLow(_Block):
    """Preço ao menos `pct`% acima da mínima das últimas `window` barras."""

    type: Literal["rebound_from_low"]
    window: Window
    pct: Pct

    def lookback(self) -> int:
        return self.window

    def label(self) -> str:
        return f"rebound{self.pct}%_from_low{self.window}"


class RandomChance(_Block):
    """Vale com `pct`% de chance a cada avaliação (usa o `rng` da estratégia)."""

    type: Literal["random_chance"]
    pct: Chance

    def label(self) -> str:
        return f"chance{self.pct}%"


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


class TrailingTakeProfit(_Block):
    """Arma quando o pico chega a +`pct`% da entrada; vende se cair `trail_pct`%."""

    type: Literal["trailing_take_profit"]
    pct: Pct
    trail_pct: Pct

    def label(self) -> str:
        return f"trailing_take_profit{self.pct}%-{self.trail_pct}%"


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
    | ReboundFromLow
    | PriceBelow
    | PriceAbove
    | VolatilityBelow
    | RandomChance
)
MarketCondition = Annotated[_MARKET, Field(discriminator="type")]
ExitCondition = Annotated[
    _MARKET | TakeProfit | TrailingTakeProfit | MaxHold, Field(discriminator="type")
]
Stop = Annotated[StopLoss | TrailingStop, Field(discriminator="type")]
Mode = Literal["all", "any"]


class BelowLastExit(_Block):
    """Preço `pct`% abaixo do da última saída; falso sem saída (veja `NoLastExit`)."""

    type: Literal["below_last_exit"]
    pct: Margin

    def label(self) -> str:
        return f"below_last_exit-{self.pct}%"


class NoLastExit(_Block):
    """Vale enquanto o bucket nunca saiu: a primeira compra, explícita na spec."""

    type: Literal["no_last_exit"]


EntryCondition = Annotated[
    _MARKET | BelowLastExit | NoLastExit, Field(discriminator="type")
]


class Entry(_Block):
    mode: Mode = "all"
    conditions: Annotated[list[EntryCondition], Field(min_length=1, max_length=8)]


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
    version: Annotated[Literal[1], Field(description="Versão do formato: 1")]
    name: Annotated[
        str,
        Field(
            pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
            description="Nome curto (minúsculas, dígitos, hífen); não muda o id",
        ),
    ]
    agent_id: Annotated[
        str,
        Field(
            pattern=r"^[A-Za-z0-9_.-]{1,64}$",
            description="Quem escreveu a spec (agente ou dono); não muda o id",
        ),
    ]
    rationale: Annotated[
        str, Field(max_length=2000, description="Por que a estratégia deve funcionar")
    ] = ""
    # OUTPUT-INPUT, como na CLI: SOL-USDC compra SOL gastando USDC
    symbol: Annotated[
        str,
        Field(
            pattern=r"^[A-Z0-9]{1,16}-[A-Z0-9]{1,16}$",
            description="Par SAÍDA-ENTRADA: SOL-USDC compra SOL gastando USDC",
        ),
    ]
    timeframe: Annotated[
        Interval,
        Field(description="Barras dos indicadores (15_SECOND, 1_MINUTE, 1_HOUR)"),
    ]
    entry: Annotated[Entry, Field(description="Condições de entrada (all/any)")]
    exit: Annotated[
        Exit, Field(description="Stop obrigatório (sempre OU) + condições de saída")
    ]
    sizing: Annotated[FixedUsd, Field(description="Quanto gastar por compra")]
    budget_usd: Annotated[
        Usd, Field(description="Teto do bucket em USD; prejuízo realizado reduz")
    ]
    max_loss_usd: Annotated[
        Usd, Field(description="Prejuízo realizado (USD) que encerra o bucket")
    ]
    cooldown_minutes: Annotated[
        int, Field(ge=0, le=10080, description="Minutos sem entrar após uma saída")
    ] = 0
    # exatamente um: data fixa, ou dias a partir do primeiro tick que roda
    expires_at: Annotated[
        AwareDatetime | None,
        Field(description="Data fixa de expiração (UTC); ou use ttl_days"),
    ] = None
    ttl_days: Annotated[
        int | None,
        Field(ge=1, le=365, description="Dias de validade a partir do 1º tick"),
    ] = None
    supersedes: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{12}$", description="Id da spec que esta substitui"),
    ] = None

    @model_validator(mode="after")
    def _loss_within_budget(self):
        if self.max_loss_usd > self.budget_usd:
            raise ValueError("max_loss_usd não pode passar de budget_usd")
        return self

    @model_validator(mode="after")
    def _one_expiry(self):
        if (self.expires_at is None) == (self.ttl_days is None):
            raise ValueError("informe expires_at ou ttl_days (exatamente um)")
        return self

    def expiry(self, start: datetime) -> datetime:
        """Quando a spec expira, para uma execução que começou em `start`."""
        if self.expires_at is not None:
            return self.expires_at
        return to_utc(start) + timedelta(days=self.ttl_days or 0)

    def conditions(self) -> list[_Block]:
        return [*self.entry.conditions, self.exit.stop, *self.exit.conditions]

    def lookback(self) -> int:
        """Barras mínimas para todas as condições terem valor."""
        return max(c.lookback() for c in self.conditions())

    def history(self) -> int:
        """Barras de aquecimento: o bastante para EMA/RSI convergirem."""
        return min(max(c.history() for c in self.conditions()), MAX_HISTORY)

    def canonical_json(self) -> str:
        """JSON canônico do comportamento (chaves ordenadas, decimais normais, UTC).

        Metadados (nome, autor, justificativa, `supersedes`) ficam de fora: duas
        specs que operam igual têm o mesmo id, e reescrever a justificativa
        não quebra o `supersedes`.
        """
        data = _canonical(self.model_dump(mode="python", exclude=METADATA_FIELDS))
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
