"""Uma janela do passado para o backtest (A34): aquecimento, ticks e cobertura.

Uma barra sem candle é uma barra sem negócio (A4) e vira um tick parado; um
trecho longo sem candles é a API que não tem aquele período (o piso da
história, ou buracos dela), e reproduzi-lo como preço parado inventaria o
mercado. Por isso uma janela com um buraco maior que `max_hole` (padrão
`MAX_HOLE`; num token fino, horas sem negócio são reais: suba-o) (as bordas
contam) é recusada, com o trecho no erro.
"""

import asyncio
import math
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.backtest.ticks import Tick, ticks_from_candles
from trader.shared.indicators import bar_index, to_utc
from trader.shared.market import CandleRange
from trader.shared.market.pair import ratio_candles
from trader.shared.models import SOLANA_MINTS, TickerData
from trader.shared.models.public_data import Interval
from trader.strategy.spec.models import StrategySpec

MAX_HOLE = timedelta(hours=1)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class Coverage:
    bars: int  # barras com candle na janela
    expected: int  # barras do timeframe na janela

    @property
    def pct(self) -> float:
        return 100.0 * self.bars / self.expected if self.expected else 0.0

    def as_dict(self) -> dict:
        return {"bars": self.bars, "expected": self.expected, "pct": self.pct}


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC, a primeira barra avaliada
    end: datetime  # UTC, exclusivo
    warmup: list[TickerData]  # antes de `start`, no token de cotação
    ticks: list[Tick]
    coverage: Coverage
    candles: list[TickerData]  # os da janela, no token de cotação (A35)


def _at(index: int, seconds: int) -> datetime:
    return datetime.fromtimestamp(index * seconds, UTC)


def _present(
    candles: list[TickerData], seconds: int, first: int, stop: int
) -> list[int]:
    """As barras de `[first, stop)` que têm candle, em ordem."""
    bars = {bar_index(c.timestamp, seconds) for c in candles}
    return sorted(b for b in bars if first <= b < stop)


def holes(
    candles: list[TickerData],
    interval: Interval,
    start: datetime,
    end: datetime,
    max_hole: timedelta = MAX_HOLE,
) -> list[tuple[datetime, datetime]]:
    """Os trechos `[de, até)` sem candles mais longos que `max_hole` (`start`
    e `end` no limite de uma barra)."""
    seconds = interval.seconds
    first, stop = bar_index(start, seconds), bar_index(end, seconds)
    found: list[tuple[datetime, datetime]] = []
    prev = first - 1
    for i in [*_present(candles, seconds, first, stop), stop]:
        if (i - prev - 1) * seconds > max_hole.total_seconds():
            found.append((_at(prev + 1, seconds), _at(i, seconds)))
        prev = i
    return found


def coverage(
    candles: list[TickerData], interval: Interval, start: datetime, end: datetime
) -> Coverage:
    seconds = interval.seconds
    first, stop = bar_index(start, seconds), bar_index(end, seconds)
    return Coverage(len(_present(candles, seconds, first, stop)), stop - first)


async def fetch_window(
    source: CandleRange,
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
    max_hole: timedelta = MAX_HOLE,
) -> Window:
    """A janela `[start, end)` da spec, com os `spec.history()` candles de
    antes dela para aquecer. As bordas vão para o limite das barras: `start`
    para a primeira que abre nele ou depois, `end` para o fim da última que
    fechou (até agora)."""
    interval = spec.timeframe
    seconds = interval.seconds
    start = _at(math.ceil(to_utc(start).timestamp() / seconds), seconds)
    end = _at(
        bar_index(min(to_utc(end), to_utc(now or datetime.now(UTC))), seconds), seconds
    )
    if end <= start:
        raise ValueError(f"janela vazia: {start.isoformat()} .. {end.isoformat()}")
    warm_start = start - timedelta(seconds=seconds * spec.history())
    pair, quote = await _series(source, spec, warm_start, end)
    _refuse_holes(holes(pair, interval, warm_start, end, max_hole), interval, max_hole)
    first = bar_index(start, seconds)
    cut = bisect_left(pair, first, key=lambda c: bar_index(c.timestamp, seconds))
    warmup, inside = pair[:cut], pair[cut:]
    ticks = ticks_from_candles(inside, interval, now=end, quote=quote)
    cover = coverage(inside, interval, start, end)
    return Window(start, end, warmup, ticks, cover, inside)


async def _series(
    source: CandleRange, spec: StrategySpec, start: datetime, end: datetime
) -> tuple[list[TickerData], list[TickerData] | None]:
    """Os candles do par em cotação e, sem stablecoin, os USD da cotação."""
    token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    if quote.is_usd_stable:
        return await source.between(token.mint, spec.timeframe, start, end), None
    candles, quote_candles = await asyncio.gather(
        source.between(token.mint, spec.timeframe, start, end),
        source.between(quote.mint, spec.timeframe, start, end),
    )
    return ratio_candles(candles, quote_candles), quote_candles


# trechos listados no erro de uma janela com buracos
SHOWN_HOLES = 3


def _refuse_holes(
    found: list[tuple[datetime, datetime]], interval: Interval, max_hole: timedelta
) -> None:
    if not found:
        return
    listed = ", ".join(
        f"{a.isoformat()} .. {b.isoformat()}" for a, b in found[:SHOWN_HOLES]
    )
    more = len(found) - SHOWN_HOLES
    if more > 0:
        listed += f" e mais {more}"
    raise ValueError(
        f"a API não tem candles de {interval} em {listed} (buraco > {max_hole}; "
        "o aquecimento conta): escolha outra janela, ou --max-hole num token fino"
    )


# A38: uma barra com metade do volume mediano da janela paga o dobro do
# slippage; nunca menos que o dado (a mediana é o "normal"), no máximo 10x
MAX_SLIPPAGE_SCALE = Decimal(10)


@dataclass(frozen=True)
class VolumeScales:
    """O multiplicador do slippage na hora de um tick: a mediana do volume da
    janela / o volume da barra dele; uma barra sem candle (sem negócio) paga o
    máximo. Uma consulta por cotação, não por tick."""

    by_bar: dict[int, Decimal]
    seconds: int

    def __call__(self, ts: datetime) -> Decimal:
        return self.by_bar.get(bar_index(ts, self.seconds), MAX_SLIPPAGE_SCALE)


def volume_scales(candles: list[TickerData], interval: Interval) -> VolumeScales | None:
    """Os multiplicadores da janela; None se os candles não têm volume."""
    volumes = [c.volume for c in candles if c.volume is not None]
    if not volumes:
        return None
    median = statistics.median(volumes)
    seconds = interval.seconds
    by_bar = {
        bar_index(c.timestamp, seconds): _scale(median, c.volume) for c in candles
    }
    return VolumeScales(by_bar, seconds)


def _scale(median: Decimal, volume: Decimal | None) -> Decimal:
    if volume is None:
        return ONE  # sem dado: o slippage dado
    if volume <= 0:
        return MAX_SLIPPAGE_SCALE
    return min(max(median / volume, ONE), MAX_SLIPPAGE_SCALE)
