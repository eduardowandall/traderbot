"""S2: cooldown, rearme e validade sobrevivem a um reinício; barras sem tick."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import make_intent, make_spec, open_ledger

from trader.execution.models.intent import IntentSide, PolicyDecision
from trader.shared.indicators import BarSeries
from trader.shared.models import Interval
from trader.shared.models.order import SwapResult
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.strategy import SpecStrategy

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _strategy(**overrides):
    strategy = SpecStrategy(StrategySpec.model_validate(make_spec(**overrides)))
    now = [T0]
    strategy.set_clock(lambda: now[0])
    strategy._warm = True
    return strategy, now


def _tick(strategy, price):
    return strategy.on_market_refresh(Decimal(price), Decimal(100), None, Decimal(1))


def test_the_ledger_reports_when_an_account_opened_and_last_exited():
    ledger = open_ledger()
    buy = make_intent(account="paper:strategy:x")
    sell = make_intent(account="paper:strategy:x", side=IntentSide.SELL)
    for intent in (buy, sell):
        ledger.record_intent(intent, PolicyDecision(True))
        ledger.mark_executed(intent.intent_id, SwapResult("s", "a", "b", 1, 1))

    opened, exited = ledger.account_times("paper:strategy:x")

    assert opened == buy.created_at
    assert opened is not None and exited is not None and exited >= opened
    assert ledger.account_times("paper:none") == (None, None)


def test_a_restart_keeps_the_cooldown_and_the_rearm():
    strategy, now = _strategy(cooldown_minutes=10)
    strategy.resume(last_exit_at=T0 - timedelta(minutes=2), opened_at=None)

    assert _tick(strategy, 80) is None  # ainda no cooldown
    now[0] = T0 + timedelta(minutes=20)
    assert _tick(strategy, 80) is None  # cooldown vencido, mas não rearmou
    assert _tick(strategy, 120) is None
    assert _tick(strategy, 80) is not None


def test_ttl_counts_from_when_the_bucket_opened_not_from_the_restart():
    data = make_spec()
    data.pop("expires_at")
    strategy = SpecStrategy(StrategySpec.model_validate({**data, "ttl_days": 1}))
    strategy.set_clock(lambda: T0)
    strategy._warm = True

    strategy.resume(last_exit_at=None, opened_at=T0 - timedelta(days=2))

    assert _tick(strategy, 80) is None  # expirou há um dia


def test_quiet_bars_repeat_the_previous_close():
    bars = BarSeries(Interval.MINUTE_1, maxlen=10)
    bars.update(T0, Decimal(100))
    bars.update(T0 + timedelta(minutes=4), Decimal(104))  # 3 barras sem tick

    assert bars.closes == [100, 100, 100, 100, 104]
