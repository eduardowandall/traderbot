"""Ticks de preço: gravação durante o bot e leitura para replay.

Formato CSV simples, uma linha por tick: `timestamp_iso,preço` (preço em USD,
como vem do feed da Jupiter).
"""

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.indicators import to_utc
from trader.models import TickerData
from trader.models.public_data import Interval


@dataclass(frozen=True, slots=True)
class Tick:
    timestamp: datetime
    price: Decimal


class TickRecorder:
    """Acrescenta ticks num CSV (flush a cada linha: sobrevive a um kill)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file)

    def record(self, timestamp: datetime, price: Decimal) -> None:
        self._writer.writerow((timestamp.isoformat(), str(price)))
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> TickRecorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def load_ticks(path: str | Path) -> list[Tick]:
    ticks: list[Tick] = []
    with Path(path).open(encoding="utf-8", newline="") as f:
        for line_no, row in enumerate(csv.reader(f), start=1):
            if not row or row[0].startswith("#"):
                continue
            try:
                ticks.append(Tick(datetime.fromisoformat(row[0]), Decimal(row[1])))
            except (ValueError, IndexError, ArithmeticError) as ex:
                raise ValueError(f"{path}:{line_no}: linha inválida {row!r}") from ex
    ticks.sort(key=lambda t: t.timestamp)
    return ticks


def ticks_from_candles(
    candles: list[TickerData],
    interval: Interval,
    now: datetime | None = None,
) -> list[Tick]:
    """Ticks de candles para o replay.

    Cada candle fechado vira um caminho dentro da barra (o `time` da Jupiter
    é a abertura; conferido ao vivo), e o candle ainda em formação é
    descartado. Barra de alta: abertura -> mínima -> máxima -> fechamento;
    barra de baixa (fechamento < abertura): abertura -> máxima -> mínima ->
    fechamento. É a heurística usual: uma compra na queda seguida de take
    profit na mesma barra de baixa não sai de graça.
    """
    ordered = sorted(candles, key=lambda c: c.timestamp)
    bar = timedelta(seconds=interval.seconds)
    cutoff = to_utc(now or datetime.now(UTC))
    return [
        tick
        for c in ordered
        if to_utc(c.timestamp) + bar <= cutoff
        for tick in _bar_path(c, bar)
    ]


# passos interpolados por trecho (abertura->extremo->extremo->fech): uma
# condição de nível dispara perto de onde o preço cruzou, não no extremo
PATH_STEPS = 8


def _bar_path(candle: TickerData, bar: timedelta) -> list[Tick]:
    start, close = candle.timestamp, candle.last
    # candles sem OHLC (só fechamento) viram um trecho plano no fechamento
    open_, low, high = candle.open or close, candle.low or close, candle.high or close
    points = [open_, high, low, close] if close < open_ else [open_, low, high, close]
    times = [timedelta(0), bar / 4, bar / 2, bar - timedelta(milliseconds=1)]
    ticks = [Tick(start, points[0])]
    for i in range(3):
        a, b = points[i], points[i + 1]
        steps = PATH_STEPS if a != b else 1
        for k in range(1, steps + 1):
            dt = times[i] + (times[i + 1] - times[i]) * k / steps
            ticks.append(Tick(start + dt, a + (b - a) * k / steps))
    return ticks
