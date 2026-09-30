import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec

from trader.backtest import Backtester, Tick
from trader.models import (
    SOLANA_MINTS,
    Order,
    OrderSide,
    OrderSignal,
    Position,
    PositionType,
    TickerData,
)
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.strategy import SpecStrategy
from trader.trading_strategy import TradingStrategy

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def _strategy(**overrides) -> tuple[SpecStrategy, Clock]:
    strategy = SpecStrategy(StrategySpec.model_validate(make_spec(**overrides)))
    clock = Clock()
    strategy.set_clock(clock)
    return strategy, clock


def _position(price, at=T0, order_id="buy-1", quantity="0.2") -> Position:
    order = Order(
        order_id, USDC, SOL, Decimal(quantity), Decimal(price), OrderSide.BUY, at
    )
    return Position(PositionType.LONG, order, None)


def _fire(strategy, clock, price, position=None, balance="100") -> OrderSignal:
    """Como `_tick`, mas exige que saia um sinal."""
    signal = _tick(strategy, clock, price, position, balance)
    assert signal is not None
    return signal


def _tick(strategy, clock, price, position=None, balance="100", minutes=1):
    signal = strategy.on_market_refresh(
        Decimal(str(price)), None, Decimal(balance), position
    )
    clock.advance(minutes=minutes)
    return signal


def _entry(*conditions, mode="all"):
    return {"mode": mode, "conditions": list(conditions)}


BELOW_100 = {"type": "price_below", "value": 100}
ABOVE_90 = {"type": "price_above", "value": 90}


class TestEntry:
    def test_waits_for_warm_up(self):
        rsi = {"type": "rsi_below", "period": 3, "value": 99}
        strategy, clock = _strategy(entry=_entry(BELOW_100, rsi, mode="any"))
        # RSI de Wilder (suavização 1/n): aquece com 10x o período (+1)
        assert strategy.warmup()[1] == 31
        # price_below já é verdade, mas o rsi precisa de 31 barras
        signals = [_tick(strategy, clock, 80) for _ in range(31)]
        assert signals[:30] == [None] * 30
        assert signals[30] is not None and signals[30].side == OrderSide.BUY

    @pytest.mark.parametrize(("mode", "fires"), [("all", False), ("any", True)])
    def test_all_vs_any(self, mode, fires):
        strategy, clock = _strategy(entry=_entry(BELOW_100, ABOVE_90, mode=mode))
        assert (_tick(strategy, clock, 80) is not None) is fires

    def test_sizing_is_fixed_usd_capped_by_balance(self):
        strategy, clock = _strategy(sizing={"type": "fixed_usd", "usd": 20})
        assert _fire(strategy, clock, 80, balance="100").quantity == Decimal("0.25")
        assert _fire(strategy, clock, 80, balance="5").quantity == Decimal(5) / 80
        assert _tick(strategy, clock, 80, balance="0") is None

    def test_rationale_names_the_conditions(self):
        strategy, clock = _strategy()
        signal = _fire(strategy, clock, 80)
        assert signal.rationale == f"spec {strategy.spec_id} buy: price<100"

    def test_no_entries_after_expiry(self):
        strategy, clock = _strategy(expires_at="2026-09-01T12:01:00Z")
        assert _tick(strategy, clock, 80) is not None  # 12:00
        assert _tick(strategy, clock, 80) is None  # 12:01, expirada

    def test_setup_seeds_the_bars(self):
        rsi = {"type": "rsi_below", "period": 3, "value": 99}
        strategy, clock = _strategy(entry=_entry(rsi))
        candles = [
            TickerData(
                buy=Decimal(0),
                timestamp=T0 - timedelta(minutes=32 - i),
                high=Decimal(0),
                last=Decimal(100 - i),
                low=Decimal(0),
                open=Decimal(0),
                pair="x",
                sell=Decimal(0),
                vol=Decimal(0),
            )
            for i in range(31)
        ]
        strategy.setup(candles)
        assert _tick(strategy, clock, 90) is not None


class TestExit:
    def test_stop_does_not_wait_for_warm_up(self):
        # posição restaurada logo após reinício: sem barras suficientes para
        # a entrada, mas o stop precisa proteger desde o primeiro tick
        slow = {"type": "rsi_below", "period": 50, "value": 30}
        strategy, clock = _strategy(entry=_entry(slow))
        assert _tick(strategy, clock, 94, _position(100)) is not None

    def test_stop_fires_even_when_exit_mode_is_all(self):
        exit_ = {
            "stop": {"type": "stop_loss", "pct": 5},
            "mode": "all",
            "conditions": [
                {"type": "take_profit", "pct": 10},
                {"type": "rsi_above", "period": 2, "value": 70},
            ],
        }
        strategy, clock = _strategy(exit=exit_)
        position = _position(100)
        assert _tick(strategy, clock, 96, position) is None
        signal = _fire(strategy, clock, 95, position)
        assert signal.side == OrderSide.SELL
        assert signal.quantity == Decimal("0.2")
        assert "stop_loss5%" in str(signal.rationale)

    def test_trailing_stop_follows_the_peak_from_entry(self):
        exit_ = {"stop": {"type": "trailing_stop", "pct": 3}}
        strategy, clock = _strategy(exit=exit_)
        position = _position(100)
        assert _tick(strategy, clock, 110, position) is None  # pico 110
        assert _tick(strategy, clock, 107, position) is None  # 110*0.97=106.7
        assert _tick(strategy, clock, "106.7", position) is not None

    def test_trailing_stop_peak_starts_at_entry(self):
        # preço já abaixo da entrada no primeiro tick: a perda respeita o stop
        exit_ = {"stop": {"type": "trailing_stop", "pct": 3}}
        strategy, clock = _strategy(exit=exit_)
        assert _tick(strategy, clock, 97, _position(100)) is not None

    def test_take_profit(self):
        strategy, clock = _strategy()
        position = _position(100)
        assert _tick(strategy, clock, 109, position) is None
        assert "take_profit10%" in str(_fire(strategy, clock, 110, position).rationale)

    def test_max_hold_uses_the_entry_order_time(self):
        exit_ = {
            "stop": {"type": "stop_loss", "pct": 50},
            "conditions": [{"type": "max_hold", "minutes": 30}],
        }
        strategy, clock = _strategy(exit=exit_)
        # posição restaurada após reinício: aberta 29 min antes do 1o tick
        position = _position(100, at=T0 - timedelta(minutes=29))
        assert _tick(strategy, clock, 100, position) is None
        assert _tick(strategy, clock, 100, position) is not None

    def test_naive_local_order_time_is_understood(self):
        exit_ = {
            "stop": {"type": "stop_loss", "pct": 50},
            "conditions": [{"type": "max_hold", "minutes": 1}],
        }
        strategy, clock = _strategy(exit=exit_)
        naive_local = (T0 - timedelta(minutes=5)).astimezone().replace(tzinfo=None)
        assert _tick(strategy, clock, 100, _position(100, at=naive_local))


class TestCooldown:
    def test_no_new_entry_until_cooldown_passes(self):
        strategy, clock = _strategy(cooldown_minutes=10)
        position = _position(100)
        _tick(strategy, clock, 99, position)  # posição aberta
        assert _tick(strategy, clock, 80) is None  # acabou de sair
        clock.advance(minutes=8)
        assert _tick(strategy, clock, 80) is None
        # cooldown vencido, mas a entrada nunca deixou de valer: não rearmou
        assert _tick(strategy, clock, 80) is None
        assert _tick(strategy, clock, 120) is None  # entrada falsa: rearma
        assert _tick(strategy, clock, 80) is not None

    def test_an_exit_needs_the_entry_to_rearm(self):
        strategy, clock = _strategy(cooldown_minutes=0)
        _tick(strategy, clock, 99, _position(100))
        assert _tick(strategy, clock, 80) is None  # recompra imediata: não
        assert _tick(strategy, clock, 120) is None
        assert _tick(strategy, clock, 80) is not None


def test_from_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(make_spec()), encoding="utf-8")
    strategy = SpecStrategy.from_file(file=str(path))
    assert strategy.symbol == "SOL-USDC"
    assert isinstance(strategy, TradingStrategy)


def _dip_and_recover_ticks():
    prices = [100, 99, 97, 95, 96, 98, 101, 104, 103, 99, 96, 94, 97, 100, 102]
    return [
        Tick(T0 + timedelta(minutes=i), Decimal(p)) for i, p in enumerate(prices * 3)
    ]


async def _backtest(spec_overrides, seed=0):
    spec = StrategySpec.model_validate(make_spec(**spec_overrides))
    return await Backtester(
        SpecStrategy(spec),
        "SOL-USDC",
        _dip_and_recover_ticks(),
        initial_balance=spec.budget_usd,
        seed=seed,
    ).run()


SWING = {
    "entry": _entry({"type": "dip_from_high", "window": 5, "pct": 3}),
    "exit": {
        "stop": {"type": "trailing_stop", "pct": 3},
        "conditions": [{"type": "take_profit", "pct": 4}],
    },
}


class TestBacktest:
    async def test_is_deterministic(self):
        first = await _backtest(SWING)
        second = await _backtest(SWING)
        assert first.trades and first == second

    async def test_max_hold_follows_the_replay_clock(self):
        # sem o relógio do replay na conta (backlog 2.3) a ordem teria o
        # horário de parede e o max_hold nunca bateria nos ticks de 2026-09-01
        overrides = {
            "entry": _entry(BELOW_100),
            "exit": {
                "stop": {"type": "stop_loss", "pct": 50},
                "conditions": [{"type": "max_hold", "minutes": 5}],
            },
        }
        result = await _backtest(overrides)
        buy, sell = result.trades[0], result.trades[1]
        assert buy.side == OrderSide.BUY and sell.side == OrderSide.SELL
        assert sell.timestamp - buy.timestamp == timedelta(minutes=5)

    async def test_strategy_logs_are_silenced_during_replay(self, caplog):
        caplog.set_level(logging.DEBUG)
        spec_logger = logging.getLogger("trader.strategy_spec")
        before = spec_logger.level
        result = await _backtest(SWING)
        assert result.trades  # houve sinais, que fora do replay logariam
        spec_logs = [r for r in caplog.records if r.name.startswith("trader.strategy")]
        assert spec_logs == []
        assert spec_logger.level == before  # o nível volta depois do replay
