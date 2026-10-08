"""Indicadores técnicos em `Decimal` (funções puras) e barras por timeframe.

Camada core: sem I/O e sem dependências do resto do bot. `INDICATORS` é o
que uma spec pode pedir (as condições tipadas e o `expr` de
`trader.strategy.spec` leem daqui), com as barras de cada um: `lookback`
para ter valor, `history` para convergir. Um indicador novo é uma entrada
em `INDICATORS` (e, se precisar de mais barras, uma regra em `lookback` ou
`history`).

Convenções:
- `values` é a série de fechamentos, do mais antigo para o mais recente;
- toda função devolve `None` quando não há dados suficientes (nunca um valor
  "aproximado"): quem usa trata `None` como "ainda não sei";
- percentuais são em pontos percentuais (`1` = 1%).
"""

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from trader.shared.models.public_data import Interval, TickerData

ZERO = Decimal(0)
HUNDRED = Decimal(100)
NEUTRAL_RSI = Decimal(50)
# barras de aquecimento por período: EMA e RSI são recursivos e só esquecem
# o valor inicial depois de vários períodos (o RSI de Wilder suaviza com 1/n,
# mais devagar que a EMA)
SEED_FACTOR = 5
RSI_SEED_FACTOR = 10


def _tail(values: Sequence[Decimal], n: int) -> Sequence[Decimal] | None:
    return values[-n:] if 0 < n <= len(values) else None


def sma(values: Sequence[Decimal], window: int) -> Decimal | None:
    tail = _tail(values, window)
    return None if tail is None else sum(tail, ZERO) / window


def wma(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Média ponderada linear: o preço mais recente pesa `window`."""
    tail = _tail(values, window)
    if tail is None:
        return None
    weights = range(1, window + 1)
    weighted = sum((v * w for v, w in zip(tail, weights, strict=True)), ZERO)
    return weighted / Decimal(sum(weights))


def ema(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Média exponencial (alpha = 2/(window+1)), semeada pela SMA inicial."""
    if not 0 < window <= len(values):
        return None
    alpha = Decimal(2) / (window + 1)
    result = sum(values[:window], ZERO) / window
    for value in values[window:]:
        result += alpha * (value - result)
    return result


def _changes(values: Sequence[Decimal]) -> list[Decimal]:
    return [b - a for a, b in zip(values[:-1], values[1:], strict=True)]


def _rsi_from(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        # sem quedas: 100 se subiu, neutro se ficou parado
        return HUNDRED if avg_gain > 0 else NEUTRAL_RSI
    return HUNDRED - HUNDRED / (1 + avg_gain / avg_loss)


def rsi(values: Sequence[Decimal], period: int) -> Decimal | None:
    """RSI de Wilder: médias iniciais simples, depois suavização (n-1)/n."""
    if period < 1 or len(values) < period + 1:
        return None
    changes = _changes(values)
    gains = [max(c, ZERO) for c in changes]
    losses = [max(-c, ZERO) for c in changes]
    avg_gain = sum(gains[:period], ZERO) / period
    avg_loss = sum(losses[:period], ZERO) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    return _rsi_from(avg_gain, avg_loss)


def _returns_pct(values: Sequence[Decimal]) -> list[Decimal] | None:
    if any(v == 0 for v in values[:-1]):
        return None
    return [(b / a - 1) * HUNDRED for a, b in zip(values[:-1], values[1:], strict=True)]


def volatility(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Desvio padrão (populacional) dos retornos % das últimas `window` barras."""
    tail = _tail(values, window + 1)
    returns = None if tail is None or window < 2 else _returns_pct(tail)
    if not returns:
        return None
    mean = sum(returns, ZERO) / len(returns)
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / len(returns)
    return variance.sqrt()


def rolling_high(values: Sequence[Decimal], window: int) -> Decimal | None:
    tail = _tail(values, window)
    return None if tail is None else max(tail)


def rolling_low(values: Sequence[Decimal], window: int) -> Decimal | None:
    tail = _tail(values, window)
    return None if tail is None else min(tail)


# --- o registro: o que uma spec pode pedir --------------------------------------

# nome -> `(fechamentos, janela) -> valor`; o nome é o do `expr` (`rsi(14)`)
INDICATORS: dict[str, Callable[[Sequence[Decimal], int], Decimal | None]] = {
    "sma": sma,
    "ema": ema,
    "wma": wma,
    "rsi": rsi,
    "volatility": volatility,
    "high": rolling_high,
    "low": rolling_low,
}
# calculados sobre as variações: uma barra a mais que a janela
_FROM_CHANGES = frozenset({"rsi", "volatility"})


def lookback(name: str, window: int) -> int:
    """Barras para o indicador `name` de `window` ter valor."""
    return window + (1 if name in _FROM_CHANGES else 0)


def history(name: str, window: int) -> int:
    """Barras para o valor convergir: EMA 5x, RSI 10x + 1 (Wilder suaviza 1/n)."""
    if name == "ema":
        return window * SEED_FACTOR
    if name == "rsi":
        return window * RSI_SEED_FACTOR + 1
    return lookback(name, window)


def to_utc(ts: datetime) -> datetime:
    """Normaliza para UTC; horário sem fuso é tratado como hora local.

    Candles da Jupiter e o relógio ao vivo são naive (hora local); ticks
    gravados são UTC-aware. Sem normalizar, comparar os dois dá TypeError.
    """
    return ts.astimezone(UTC)


def bar_index(ts: datetime, seconds: int) -> int:
    """O número da barra de `seconds` segundos em que `ts` cai (em UTC)."""
    return int(to_utc(ts).timestamp()) // seconds


# barras sem tick que ainda são preenchidas com o fechamento anterior; um
# buraco maior (notebook dormiu, backoff longo, emenda de arquivo de ticks)
# zera a série, senão a janela vira barras planas (RSI 0/100, volatilidade ~0)
MAX_GAP_BARS = 5


class BarSeries:
    """Fechamentos por barra de `interval`, limitados a `maxlen` barras.

    A barra em formação usa o último preço: a série sempre termina no preço
    atual. Um tick de uma barra anterior à atual (fora de ordem)
    também só atualiza a barra atual. Um buraco de mais de `MAX_GAP_BARS`
    entre ticks recomeça a série do zero (a estratégia volta a aquecer).

    Candles só existem para barras com negócio (A4): no `seed`, uma barra sem
    candle é "sem negócio", não "sem dados", e repete o fechamento anterior.
    """

    def __init__(self, interval: Interval, maxlen: int):
        self.interval = interval
        self._seconds = interval.seconds
        self._maxlen = maxlen
        self._closes: deque[Decimal] = deque(maxlen=maxlen)
        self._bucket: int | None = None

    def update(self, ts: datetime, price: Decimal) -> bool:
        """Registra um preço; retorna True se abriu uma barra nova."""
        return self._add(ts, price, quiet=False)

    def seed(
        self, candles: Iterable[TickerData], until: datetime | None = None
    ) -> None:
        """`until` (agora): do último candle até lá também não houve negócio, e
        a barra de `until` começa no último fechamento."""
        for candle in sorted(candles, key=lambda c: to_utc(c.timestamp)):
            self._add(candle.timestamp, candle.last, quiet=True)
        if until is not None and self._closes:
            self._add(until, self._closes[-1], quiet=True)

    def _add(self, ts: datetime, price: Decimal, quiet: bool) -> bool:
        """`quiet`: um buraco antes deste preço é falta de negócio, não do feed."""
        bucket = bar_index(ts, self._seconds)
        if self._bucket is not None and bucket <= self._bucket:
            self._closes[-1] = price
            return False
        missing = 0 if self._bucket is None else bucket - self._bucket - 1
        if missing > MAX_GAP_BARS and not quiet:
            self._closes.clear()
        elif missing:
            # barras sem tick (feed quieto): repetem o fechamento anterior,
            # como os candles, para os indicadores cobrirem o mesmo tempo
            self._closes.extend([self._closes[-1]] * min(missing, self._maxlen))
        self._closes.append(price)
        self._bucket = bucket
        return True

    @property
    def closes(self) -> list[Decimal]:
        return list(self._closes)

    def __len__(self) -> int:
        return len(self._closes)
