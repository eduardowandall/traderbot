"""Avaliação das condições da spec: predicados puros sobre um `TickContext`.

`PREDICATES` mapeia cada `type` da spec para uma função `(condição, contexto)
-> bool`. As condições não guardam estado: os indicadores vêm do
`IndicatorBank` (atualizado uma vez por tick pela estratégia, com cache por
nome e parâmetros), e posição/pico vêm do contexto. Indicador sem dados
(`None`) torna a condição falsa.
"""

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trader import indicators as ind
from trader.models.public_data import Interval, TickerData

HUNDRED = Decimal(100)

_INDICATORS: dict[str, Callable[..., Decimal | None]] = {
    "sma": ind.sma,
    "ema": ind.ema,
    "wma": ind.wma,
    "rsi": ind.rsi,
    "volatility": ind.volatility,
    "high": ind.rolling_high,
    "low": ind.rolling_low,
}


class IndicatorBank:
    """Barras do timeframe da spec + indicadores calculados uma vez por tick."""

    def __init__(self, interval: Interval, maxlen: int):
        self.bars = ind.BarSeries(interval, maxlen)
        self._closes: list[Decimal] = []
        self._cache: dict[tuple, Decimal | None] = {}

    def update(self, ts: datetime, price: Decimal) -> None:
        self.bars.update(ts, price)
        self._refresh()

    def seed(self, candles: Sequence[TickerData]) -> None:
        self.bars.seed(candles)
        self._refresh()

    def _refresh(self) -> None:
        self._closes = self.bars.closes
        self._cache.clear()

    def get(self, name: str, *params: int) -> Decimal | None:
        key = (name, *params)
        if key not in self._cache:
            self._cache[key] = _INDICATORS[name](self._closes, *params)
        return self._cache[key]

    def __len__(self) -> int:
        return len(self.bars)


@dataclass(frozen=True)
class TickContext:
    price: Decimal
    now: datetime  # UTC
    bank: IndicatorBank
    # sorteios de `random_chance` (semente fixa no backtest)
    rng: random.Random = field(default_factory=random.Random)
    # só com posição aberta
    entry_price: Decimal | None = None
    entry_time: datetime | None = None
    peak: Decimal | None = None
    # última saída do bucket (None: nunca saiu, ou preço desconhecido)
    last_exit_at: datetime | None = None
    last_exit_price: Decimal | None = None


def _lt(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and a < b


def _scaled(base: Decimal | None, pct: Decimal) -> Decimal | None:
    """`base * (1 + pct/100)`; None se `base` for None."""
    return None if base is None else base * (1 + pct / HUNDRED)


def _rsi_below(c, ctx: TickContext) -> bool:
    return _lt(ctx.bank.get("rsi", c.period), c.value)


def _rsi_above(c, ctx: TickContext) -> bool:
    return _lt(c.value, ctx.bank.get("rsi", c.period))


def _price_below_ma(c, ctx: TickContext) -> bool:
    return _lt(ctx.price, _scaled(ctx.bank.get(c.ma, c.window), -c.pct))


def _price_above_ma(c, ctx: TickContext) -> bool:
    return _lt(_scaled(ctx.bank.get(c.ma, c.window), c.pct), ctx.price)


def _fast_ma_above_slow(c, ctx: TickContext) -> bool:
    return _lt(ctx.bank.get(c.ma, c.slow), ctx.bank.get(c.ma, c.fast))


def _fast_ma_below_slow(c, ctx: TickContext) -> bool:
    return _lt(ctx.bank.get(c.ma, c.fast), ctx.bank.get(c.ma, c.slow))


def _dip_from_high(c, ctx: TickContext) -> bool:
    limit = _scaled(ctx.bank.get("high", c.window), -c.pct)
    return limit is not None and ctx.price <= limit


def _rebound_from_low(c, ctx: TickContext) -> bool:
    floor = _scaled(ctx.bank.get("low", c.window), c.pct)
    return floor is not None and ctx.price >= floor


def _random_chance(c, ctx: TickContext) -> bool:
    return ctx.rng.randint(1, 100) <= c.pct


def _price_below(c, ctx: TickContext) -> bool:
    return ctx.price < c.value


def _price_above(c, ctx: TickContext) -> bool:
    return ctx.price > c.value


def _volatility_below(c, ctx: TickContext) -> bool:
    return _lt(ctx.bank.get("volatility", c.window), c.pct)


def _below_last_exit(c, ctx: TickContext) -> bool:
    limit = _scaled(ctx.last_exit_price, -c.pct)
    return limit is not None and ctx.price <= limit


def _no_last_exit(c, ctx: TickContext) -> bool:
    return ctx.last_exit_at is None


def _take_profit(c, ctx: TickContext) -> bool:
    target = _scaled(ctx.entry_price, c.pct)
    return target is not None and ctx.price >= target


def _trailing_take_profit(c, ctx: TickContext) -> bool:
    # arma quando o pico desde a entrada chega ao alvo; segue armado até vender
    target = _scaled(ctx.entry_price, c.pct)
    armed = target is not None and ctx.peak is not None and ctx.peak >= target
    limit = _scaled(ctx.peak, -c.trail_pct)
    return armed and limit is not None and ctx.price <= limit


def _max_hold(c, ctx: TickContext) -> bool:
    if ctx.entry_time is None:
        return False
    return ctx.now - ctx.entry_time >= timedelta(minutes=c.minutes)


def _stop_loss(c, ctx: TickContext) -> bool:
    limit = _scaled(ctx.entry_price, -c.pct)
    return limit is not None and ctx.price <= limit


def _trailing_stop(c, ctx: TickContext) -> bool:
    limit = _scaled(ctx.peak, -c.pct)
    return limit is not None and ctx.price <= limit


PREDICATES: dict[str, Callable[[Any, TickContext], bool]] = {
    "rsi_below": _rsi_below,
    "rsi_above": _rsi_above,
    "price_below_ma": _price_below_ma,
    "price_above_ma": _price_above_ma,
    "fast_ma_above_slow": _fast_ma_above_slow,
    "fast_ma_below_slow": _fast_ma_below_slow,
    "dip_from_high": _dip_from_high,
    "rebound_from_low": _rebound_from_low,
    "random_chance": _random_chance,
    "price_below": _price_below,
    "price_above": _price_above,
    "volatility_below": _volatility_below,
    "below_last_exit": _below_last_exit,
    "no_last_exit": _no_last_exit,
    "take_profit": _take_profit,
    "trailing_take_profit": _trailing_take_profit,
    "max_hold": _max_hold,
    "stop_loss": _stop_loss,
    "trailing_stop": _trailing_stop,
}


def holds(condition, ctx: TickContext) -> bool:
    return PREDICATES[condition.type](condition, ctx)


def fired(mode: str, conditions: Sequence, ctx: TickContext) -> list[str] | None:
    """Rótulos das condições verdadeiras, se o grupo dispara; senão None.

    Grupo vazio nunca dispara (nem com `all`, que seria verdadeiro por vacuidade).
    """
    labels = [c.label() for c in conditions if holds(c, ctx)]
    satisfied = len(labels) == len(conditions) if mode == "all" else bool(labels)
    return labels if conditions and satisfied else None
