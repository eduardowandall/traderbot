"""O feed de um par sem stablecoin (B6): o token no token de cotação."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.market.pair import PairMarketData, market_for, ratio_candles
from trader.models import SOLANA_MINTS, TickerData
from trader.models.public_data import Interval

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
JUP = SOLANA_MINTS.get_by_symbol("JUP").mint
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def candle(minute, open_, high, low, close):
    return TickerData(
        timestamp=T0 + timedelta(minutes=minute),
        high=Decimal(high),
        last=Decimal(close),
        low=Decimal(low),
        open=Decimal(open_),
    )


class Feed:
    def __init__(self, prices=None, candles=None):
        self.prices = prices or {}
        self.candles = candles or {}
        self.closed = False

    async def get_price(self, mint):
        return self.prices[mint]

    async def get_candles(self, mint, interval, candle_qty):
        return self.candles[mint][-candle_qty:]

    async def aclose(self):
        self.closed = True


async def test_the_price_is_token_usd_over_quote_usd():
    pair = PairMarketData(Feed({JUP: Decimal("0.8")}), Feed({SOL: Decimal("160")}), SOL)
    assert await pair.get_price(JUP) == Decimal("0.005")
    await pair.aclose()
    assert pair.token_feed.closed and pair.quote_feed.closed  # type: ignore[attr-defined]


async def test_candles_are_divided_bar_by_bar():
    jup = [candle(0, "1", "1.2", "0.9", "1.1"), candle(1, "1.1", "1.1", "1", "1")]
    sol = [candle(1, "200", "210", "190", "200"), candle(2, "200", "200", "200", "200")]
    pair = PairMarketData(Feed(candles={JUP: jup}), Feed(candles={SOL: sol}), SOL)

    (bar,) = await pair.get_candles(JUP, Interval.MINUTE_1, 10)  # só o minuto 1

    assert bar.timestamp == T0 + timedelta(minutes=1)
    assert (bar.open, bar.last) == (Decimal("0.0055"), Decimal("0.005"))
    # extremos: os do token sobre o fechamento da cotação, cobrindo abertura
    assert bar.high == Decimal("0.0055")
    assert bar.low == Decimal("0.005")


def test_ratio_high_and_low_always_cover_open_and_close():
    jup = [candle(0, "1", "1", "1", "2")]  # máxima informada abaixo do fech.
    sol = [candle(0, "1", "1", "1", "1")]
    (bar,) = ratio_candles(jup, sol)
    assert bar.low <= min(bar.open, bar.last) <= max(bar.open, bar.last) <= bar.high


def test_stable_pairs_keep_the_plain_feed():
    made = []

    def factory():
        made.append(Feed())
        return made[-1]

    assert market_for("SOL-USDC", factory) is made[0]
    pair = market_for("JUP-SOL", factory)
    assert isinstance(pair, PairMarketData) and pair.quote == SOL
    assert len(made) == 3  # um feed por mint: o websocket assina um ativo
