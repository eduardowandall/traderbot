"""Ao vivo x backtest (B5): aquecimento antes dos ticks e a comparação."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import executed_leg, make_spec, memory_gateway

from trader.backtest import Tick
from trader.backtest.compare import compare_live, fetch_warmup
from trader.models import OrderSide, TickerData
from trader.strategy_spec.models import StrategySpec

T0 = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
ACCOUNT_PREFIX = "paper:strategy:"


def _minute(n: float) -> datetime:
    return T0 + timedelta(minutes=n)


def _candle(minute: int) -> TickerData:
    price = Decimal(100 + minute)
    return TickerData(
        timestamp=_minute(minute),
        high=price,
        last=price,
        low=price,
        open=price,
    )


class Candles:
    def __init__(self, minutes):
        self.candles = [_candle(m) for m in minutes]
        self.asked: list[int] = []

    async def get_candles(self, mint, interval, candle_qty):
        self.asked.append(candle_qty)
        return self.candles[-candle_qty:]

    async def get_price(self, mint):
        raise AssertionError("não usado")

    async def aclose(self):
        return None


def _sma_spec() -> StrategySpec:
    entry = {"conditions": [{"type": "price_below_ma", "ma": "sma", "window": 3}]}
    return StrategySpec.model_validate(make_spec(entry=entry))


def _dip_spec() -> StrategySpec:
    return StrategySpec.model_validate(make_spec())  # compra < 100, tp 10%


class TestWarmup:
    async def test_takes_the_candles_that_closed_before_the_first_tick(self):
        data = Candles(range(-5, 3))  # 09:55 .. 10:02
        before, now = _minute(0.5), _minute(3)

        warmup = await fetch_warmup(data, _sma_spec(), before, now)

        assert [c.timestamp for c in warmup] == [_minute(m) for m in (-3, -2, -1)]
        assert data.asked == [3 + 4]  # aquecimento + barras desde o 1º tick

    async def test_refuses_ticks_older_than_the_candle_api_reaches(self):
        with pytest.raises(ValueError, match="aquecimento"):
            await fetch_warmup(Candles([]), _sma_spec(), T0, _minute(2000))

    async def test_price_only_specs_take_the_last_closed_candle(self):
        data = Candles(range(-5, 1))
        warmup = await fetch_warmup(data, _dip_spec(), T0, _minute(1))
        assert [c.timestamp for c in warmup] == [_minute(-1)]


async def test_compares_the_replay_with_the_bucket_in_the_same_window():
    spec = _dip_spec()
    account = ACCOUNT_PREFIX + spec.spec_id()
    ticks = [
        Tick(_minute(i), Decimal(p)) for i, p in enumerate([101, 99, 105, 110, 111])
    ]
    ledger = memory_gateway().ledger
    # a ordem leva o relógio local da conta (sem fuso, aqui UTC+1); vale o
    # horário da intenção
    local = (_minute(1 + 2 / 60) + timedelta(hours=1)).replace(tzinfo=None)
    executed_leg(
        ledger, account, "buy", _minute(1 + 2 / 60), price="99.2", timestamp=local
    )
    executed_leg(ledger, account, "sell", _minute(3 + 1 / 60), "2.0", price="109.5")
    # fora da janela, ou de outro bucket: não contam
    executed_leg(ledger, account, "sell", _minute(-60), "9", price="1")
    executed_leg(ledger, ACCOUNT_PREFIX + "other", "buy", _minute(2))

    report = await compare_live(spec, ticks, ledger, "paper", warmup=[])

    replay = report["backtest"]["trades"]
    assert [t.side for t in replay] == [OrderSide.BUY, OrderSide.SELL]
    assert [t.side for t in report["live"]["trades"]] == [
        OrderSide.BUY,
        OrderSide.SELL,
    ]
    assert report["live"]["realized_pnl"] == Decimal("2.0")
    diff = report["diff"]
    assert diff["trades"] == 0
    assert diff["realized_pnl"] == Decimal("2.0") - report["backtest"]["realized_pnl"]
    buy = diff["fills"][0]
    assert buy["same_side"] and buy["seconds_apart"] == 2
    assert (
        buy["price_diff_bps"]
        == (Decimal("99.2") - replay[0].price) / replay[0].price * 10000
    )
    # o custo por ida e volta, medido igual dos dois lados (B9)
    assert report["backtest"]["round_trip_costs"]["count"] == 1
    assert report["live"]["round_trip_costs"]["count"] == 1
