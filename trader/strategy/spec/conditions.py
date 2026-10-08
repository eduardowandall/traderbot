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

from trader.shared import indicators as ind
from trader.shared.models.direction import Direction
from trader.shared.models.public_data import Interval, TickerData
from trader.strategy.spec import expr as expr_lang

HUNDRED = Decimal(100)


class IndicatorBank:
    """Barras do timeframe da spec + indicadores calculados uma vez por tick."""

    def __init__(self, interval: Interval, maxlen: int):
        self.bars = ind.BarSeries(interval, maxlen)
        self._closes: list[Decimal] = []
        self._cache: dict[tuple, Decimal | None] = {}

    def update(self, ts: datetime, price: Decimal) -> None:
        self.bars.update(ts, price)
        self._refresh()

    def seed(
        self, candles: Sequence[TickerData], until: datetime | None = None
    ) -> None:
        self.bars.seed(candles, until)
        self._refresh()

    def _refresh(self) -> None:
        self._closes = self.bars.closes
        self._cache.clear()

    def get(self, name: str, *params: int) -> Decimal | None:
        key = (name, *params)
        if key not in self._cache:
            self._cache[key] = ind.INDICATORS[name](self._closes, *params)
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
    # o melhor preço desde a entrada, no lado da posição (D3)
    peak: Decimal | None = None
    direction: Direction = Direction.LONG
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


def _expr(c, ctx: TickContext) -> bool:
    return expr_lang.holds(c.tree(), ctx)


def _toward(ctx: TickContext, base: Decimal | None, pct: Decimal) -> Decimal | None:
    """`base` movido `pct`% a favor da posição (contra, com `pct` negativo)."""
    return _scaled(base, ctx.direction.sign * pct)


def _beyond(ctx: TickContext, price: Decimal | None, level: Decimal | None) -> bool:
    """`price` está em `level` ou além dele a favor da posição."""
    if price is None or level is None:
        return False
    return ctx.direction.sign * (price - level) >= 0


def _crossed_back(ctx: TickContext, level: Decimal | None) -> bool:
    """O preço voltou até `level` contra a posição (caiu comprado, subiu vendido)."""
    return _beyond(ctx, level, ctx.price)


def _take_profit(c, ctx: TickContext) -> bool:
    return _beyond(ctx, ctx.price, _toward(ctx, ctx.entry_price, c.pct))


def _trailing_take_profit(c, ctx: TickContext) -> bool:
    # arma quando o pico desde a entrada chega ao alvo; segue armado até vender
    armed = _beyond(ctx, ctx.peak, _toward(ctx, ctx.entry_price, c.pct))
    return armed and _crossed_back(ctx, _toward(ctx, ctx.peak, -c.trail_pct))


def _max_hold(c, ctx: TickContext) -> bool:
    if ctx.entry_time is None:
        return False
    return ctx.now - ctx.entry_time >= timedelta(minutes=c.minutes)


def _stop_loss(c, ctx: TickContext) -> bool:
    return _crossed_back(ctx, _toward(ctx, ctx.entry_price, -c.pct))


def _trailing_stop(c, ctx: TickContext) -> bool:
    return _crossed_back(ctx, _toward(ctx, ctx.peak, -c.pct))


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
    "expr": _expr,
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
