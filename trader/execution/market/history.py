"""Candles do passado em pedaços fixos, com cache em disco (A34).

A API devolve até 1000 candles que abrem antes de `to`; com `to` no limite
de uma barra, exatamente as barras de `[to - 1000 barras, to)` (conferido ao
vivo). A história é cortada em pedaços de `CHUNK_BARS` barras alinhados à
época, um pedido cada. Candles passados não mudam: um pedaço fechado e não
vazio fica em `.data/candles/<mint>/<intervalo>/`, e o próximo backtest da
mesma janela não vai à rede.
"""

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trader.execution.market.jupiter.candles import MAX_CANDLES, candles_to_tickers
from trader.shared.indicators import bar_index, to_utc
from trader.shared.models.public_data import Interval, TickerData
from trader.shared.paths import candles_dir

CHUNK_BARS = MAX_CANDLES
# um pedaço só é final quando a última barra fechou e a API teve tempo de tê-la
SETTLE = timedelta(seconds=60)
COLUMNS = ("time", "open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class Chunk:
    """`CHUNK_BARS` barras de `interval` que abrem em `[start, end)`."""

    interval: Interval
    start: datetime  # UTC

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.interval.seconds * CHUNK_BARS)

    def is_final(self, now: datetime) -> bool:
        bar = timedelta(seconds=self.interval.seconds)
        return self.end + bar + SETTLE <= to_utc(now)


def chunks(interval: Interval, start: datetime, end: datetime) -> list[Chunk]:
    """Os pedaços que cobrem `[start, end)`, em ordem."""
    span = interval.seconds * CHUNK_BARS
    first = bar_index(start, span)
    last = -(-int(to_utc(end).timestamp()) // span)  # teto
    return [
        Chunk(interval, datetime.fromtimestamp(k * span, UTC))
        for k in range(first, last)
    ]


class CandleCache:
    """Um CSV por pedaço fechado: `time` em segundos da época, decimais como a
    API os deu."""

    def __init__(self, root: Path | None = None):
        self.root = root

    def path(self, mint: str, chunk: Chunk) -> Path:
        root = self.root or candles_dir()
        name = f"{chunk.start:%Y%m%dT%H%M%S}.csv"
        return root / mint / str(chunk.interval) / name

    def read(self, mint: str, chunk: Chunk) -> list[TickerData] | None:
        path = self.path(mint, chunk)
        if not path.exists():
            return None
        with path.open(encoding="utf-8", newline="") as f:
            # as colunas são as chaves da API: o mesmo decodificador
            return candles_to_tickers([_raw(row) for row in csv.DictReader(f)])

    def write(self, mint: str, chunk: Chunk, candles: list[TickerData]) -> None:
        path = self.path(mint, chunk)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(COLUMNS)
            writer.writerows(_row(c) for c in candles)
        tmp.replace(path)  # nunca um pedaço pela metade


def _row(c: TickerData) -> list[str]:
    volume = "" if c.volume is None else str(c.volume)
    stamp = str(int(to_utc(c.timestamp).timestamp()))
    return [stamp, str(c.open), str(c.high), str(c.low), str(c.last), volume]


def _raw(row: dict[str, str]) -> dict:
    return {**row, "time": int(row["time"]), "volume": row["volume"] or None}
