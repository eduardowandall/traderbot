"""Ticks de preço: gravação durante o bot e leitura para replay.

Formato CSV simples, uma linha por tick: `timestamp_iso,preço` (preço em USD,
como vem do feed da Jupiter).
"""

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trader.models import TickerData


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


def ticks_from_candles(candles: list[TickerData]) -> list[Tick]:
    """Um tick por candle, no preço de fechamento."""
    return sorted(
        (Tick(c.timestamp, c.last) for c in candles), key=lambda t: t.timestamp
    )
