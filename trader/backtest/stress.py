"""Estresses de um backtest (A38): custos multiplicados e choques sintéticos.

Crashes de verdade são poucos: um estresse repete cada janela com a taxa e o
slippage x N (`--costs 2x`; a taxa de rede é fixa por transação e não
muda) ou com um choque no preço (`--shock -15%@50%`). Cada um é a base mais
uma mudança, nunca uma combinação. Uma spec que só ganha na base é frágil.
"""

import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from trader.backtest.ticks import Tick

ONE = Decimal(1)
HUNDRED = Decimal(100)
# os custos que um estresse multiplica (as chaves de `backtest_spec`)
COST_KEYS = ("fee_bps", "slippage_bps")

_DURATION = re.compile(r"(\d+)([mhd])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str) -> timedelta:
    """`30m`, `12h`, `7d`."""
    match = _DURATION.fullmatch(value.strip())
    if match is None or int(match[1]) == 0:
        raise ValueError(f"duração inválida: {value!r} (ex: 30m, 12h, 7d)")
    return timedelta(**{_UNITS[match[2]]: int(match[1])})


@dataclass(frozen=True)
class Shock:
    """O preço x (1 + pct/100) a partir de `at` (fração do tempo da janela);
    com `recover`, volta ao real em linha reta nesse tempo."""

    pct: Decimal
    at: Decimal = Decimal("0.5")
    recover: timedelta | None = None

    def apply(self, ticks: list[Tick], start: datetime, end: datetime) -> list[Tick]:
        """Só os ticks do choque são refeitos; antes e depois dele, os mesmos."""
        if not ticks:
            return ticks
        t0 = _like(ticks[0].timestamp, start + (end - start) * float(self.at))
        first = bisect_left(ticks, t0, key=lambda t: t.timestamp)
        if self.recover is None:
            factor = ONE + self.pct / HUNDRED
            return ticks[:first] + [_times(t, factor) for t in ticks[first:]]
        until = bisect_left(ticks, t0 + self.recover, key=lambda t: t.timestamp)
        moved = [_times(t, self._factor(t.timestamp - t0)) for t in ticks[first:until]]
        return ticks[:first] + moved + ticks[until:]

    def _factor(self, since: timedelta) -> Decimal:
        assert self.recover is not None
        left = Decimal(str(1 - since / self.recover))
        return ONE + self.pct / HUNDRED * left


def _like(sample: datetime, at: datetime) -> datetime:
    """`at` no fuso dos ticks: candles vêm em hora local sem fuso."""
    return at.astimezone().replace(tzinfo=None) if sample.tzinfo is None else at


def _times(tick: Tick, factor: Decimal) -> Tick:
    return Tick(tick.timestamp, tick.price * factor, tick.quote_usd)


@dataclass(frozen=True)
class Stress:
    name: str
    costs: Decimal = ONE  # multiplicador da taxa e do slippage
    shock: Shock | None = None

    def scaled(self, costs: dict) -> dict:
        if self.costs == ONE:
            return costs
        return {k: v * self.costs if k in COST_KEYS else v for k, v in costs.items()}

    def ticks(self, ticks: list[Tick], start: datetime, end: datetime) -> list[Tick]:
        return ticks if self.shock is None else self.shock.apply(ticks, start, end)


BASE = Stress("base")


def stresses(costs: str | None, shocks: list[str]) -> list[Stress]:
    """A base e um estresse por multiplicador de `--costs` e por `--shock`."""
    found = [BASE]
    for item in (costs or "").split(","):
        if item.strip() and item.strip() != "base":
            found.append(_costs(item.strip()))
    found += [Stress(f"shock {s.strip()}", shock=parse_shock(s)) for s in shocks]
    return found


def _costs(item: str) -> Stress:
    message = f"--costs {item!r}: use 2x, 1.5x"
    if not item.endswith("x"):
        raise ValueError(message)
    try:
        number = Decimal(item[:-1])
    except InvalidOperation:
        raise ValueError(message) from None
    if not number.is_finite() or number <= 0:
        raise ValueError(message)
    return Stress(item, costs=number)


_SHOCK = re.compile(
    r"(?P<pct>[+-]?\d+(?:\.\d+)?)%(?:~(?P<recover>[^@]+))?(?:@(?P<at>\d+(?:\.\d+)?)%)?"
)


def parse_shock(text: str) -> Shock:
    """`-15%@50%` (um degrau), `-10%~1h@50%` (cai e volta em 1 h); `@` padrão
    50%."""
    match = _SHOCK.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"--shock {text!r}: use -15%@50% ou -10%~1h@50%")
    at = Decimal(match["at"] or "50") / HUNDRED
    pct = Decimal(match["pct"])
    if not 0 <= at <= 1 or pct <= -100:
        raise ValueError(
            f"--shock {text!r}: o momento vai de 0% a 100%, a queda < 100%"
        )
    recover = match["recover"]
    return Shock(pct, at, None if recover is None else _recover(recover, text))


def _recover(value: str, text: str) -> timedelta:
    try:
        return parse_duration(value)
    except ValueError:
        raise ValueError(f"--shock {text!r}: a volta é uma duração, ex: 1h") from None
