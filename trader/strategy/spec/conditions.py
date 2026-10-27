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
    """Barras do timeframe da spec + indicadores calculados uma vez por tick.

    Os valores ficam por barra (`_values[número da barra]`): os da barra em
    formação recomeçam a cada tick, então uma barra fechada guarda os do seu
    último tick. `get(..., back)` lê esses (A22): `ema(9, 1)` é o `ema(9)` com
    que a barra anterior fechou, não um recálculo sobre uma janela que já
    andou (com a série cheia, a EMA e o RSI começam uma barra depois e mudam
    um pouco, o bastante para errar um cruzamento por um fio). Uma barra
    fechada sem valor (semeada, preenchida, ou não lida no último tick) é
    calculada sobre os fechamentos até ela.
    """

    def __init__(self, interval: Interval, maxlen: int):
        self.bars = ind.BarSeries(interval, maxlen)
        # cópia dos fechamentos, só quando uma conta inteira precisa (A37)
        self._closes: list[Decimal] | None = None
        self._values: dict[int, dict[tuple[str, int], Decimal | None]] = {}
        # `ind.PARTIAL`: o estado das barras fechadas, por (nome, janela); vale
        # até a barra mudar
        self._partial: dict[tuple[str, int], Any] = {}
        self._closed: list[Decimal] | None = None

    def update(self, ts: datetime, price: Decimal) -> None:
        if self.bars.update(ts, price):
            self._new_bar()
        self._refresh()

    def seed(
        self, candles: Sequence[TickerData], until: datetime | None = None
    ) -> None:
        self.bars.seed(candles, until)
        self._new_bar()
        self._refresh()

    def _new_bar(self) -> None:
        """Uma barra nova (ou um seed): esquece as que saíram da série (pela
        janela ou um recomeço) e os estados da barra anterior."""
        oldest = self.bars.bar - len(self.bars) + 1
        for bar in [bar for bar in self._values if bar < oldest]:
            del self._values[bar]
        self._partial = {}
        self._closed = None

    def _refresh(self) -> None:
        self._closes = None
        self._values[self.bars.bar] = {}  # a barra em formação mudou

    def get(self, name: str, window: int, back: int = 0) -> Decimal | None:
        """O indicador `back` barras antes da em formação (A22)."""
        values = self._values.setdefault(self.bars.bar - back, {})
        if (name, window) not in values:
            values[name, window] = self._compute(name, window, back)
        return values[name, window]

    def _compute(self, name: str, window: int, back: int) -> Decimal | None:
        if back == 0 and name in ind.PARTIAL and len(self.bars):
            state = self._state(name, window)
            if state is not None:
                return ind.PARTIAL[name][1](state, self.bars.last, window)
        if self._closes is None:
            self._closes = self.bars.closes
        closes = self._closes[:-back] if back else self._closes
        return ind.INDICATORS[name](closes, window)

    def _state(self, name: str, window: int) -> Any:
        """O estado das barras fechadas: uma vez por barra."""
        if (name, window) not in self._partial:
            if self._closed is None:
                self._closed = self.bars.closed
            self._partial[name, window] = ind.PARTIAL[name][0](self._closed, window)
        return self._partial[name, window]

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


def _le(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and a <= b


def _crossed(ctx: TickContext, ma: str, a: int, b: int) -> bool:
    """MA(a) passou para cima de MA(b): acima agora, não acima uma barra
    fechada atrás (A22)."""
    get = ctx.bank.get
    return _lt(get(ma, b), get(ma, a)) and _le(get(ma, a, 1), get(ma, b, 1))


def _ma_crosses_above(c, ctx: TickContext) -> bool:
    return _crossed(ctx, c.ma, c.fast, c.slow)


def _ma_crosses_below(c, ctx: TickContext) -> bool:
    return _crossed(ctx, c.ma, c.slow, c.fast)


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
    "ma_crosses_above": _ma_crosses_above,
    "ma_crosses_below": _ma_crosses_below,
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
