"""A12: `no_last_exit` (primeira compra) e `below_last_exit` (recompra)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from factories import example_spec, make_intent, make_spec, open_ledger

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentSide, PolicyDecision
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import Policy
from trader.shared.models import SOLANA_MINTS, Order, OrderSide, Position
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.strategy import SpecStrategy

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint


def _strategy() -> SpecStrategy:
    # a spec de exemplo com números fixos (o dono ajusta os do arquivo):
    # take_profit 0.12%, stop_loss 0.08%, recompra -0.05%
    spec = json.loads(Path(example_spec("scalp-test")).read_text(encoding="utf-8"))
    spec["exit"]["conditions"] = [{"type": "take_profit", "pct": 0.12}]
    spec["exit"]["stop"] = {"type": "stop_loss", "pct": 0.08}
    spec["entry"]["conditions"][1] = {"type": "below_last_exit", "pct": 0.05}
    strategy = SpecStrategy(StrategySpec.model_validate(spec))
    ticks = iter(T0 + timedelta(seconds=15 * i) for i in range(1000))
    strategy.set_clock(lambda: next(ticks))
    return strategy


def _position(price) -> Position:
    order = Order("buy-1", USDC, SOL, Decimal(1), Decimal(price), OrderSide.BUY, T0)
    return Position(order, None)


def _tick(strategy, price, position=None):
    return strategy.on_market_refresh(
        Decimal(str(price)), Decimal(100), position, Decimal(1)
    )


def test_the_first_buy_comes_from_the_explicit_no_last_exit():
    signal = _tick(_strategy(), 100)
    assert signal and signal.side == OrderSide.BUY
    # só `no_last_exit` vale: `below_last_exit` é falso sem saída anterior
    assert signal.rationale and signal.rationale.endswith(": no_last_exit")


def test_below_last_exit_alone_never_buys_without_an_exit():
    spec = make_spec(entry={"conditions": [{"type": "below_last_exit", "pct": 0.05}]})
    strategy = SpecStrategy(StrategySpec.model_validate(spec))
    strategy.set_clock(lambda: T0)
    assert _tick(strategy, 1) is None


def test_an_exit_with_an_unknown_price_waits_instead_of_buying():
    # venda no ledger sem ordem: saiu (no_last_exit falso), preço desconhecido
    strategy = _strategy()
    strategy.resume(T0 - timedelta(minutes=1), None, last_exit_price=None)
    assert _tick(strategy, 1) is None


def test_sells_at_the_target_and_the_stop():
    target = _tick(_strategy(), "100.12", _position(100))
    stop = _tick(_strategy(), "99.92", _position(100))
    held = _tick(_strategy(), "100.11", _position(100))
    assert target and target.side == OrderSide.SELL
    assert "take_profit0.12%" in (target.rationale or "")
    assert stop and stop.side == OrderSide.SELL
    assert "stop_loss0.08%" in (stop.rationale or "")
    assert held is None


def test_rebuys_only_below_the_last_sell_price():
    strategy = _strategy()
    assert _tick(strategy, "100.12", _position(100))  # vende a 100.12
    # a posição fechou: recompra a 100.12 * (1 - 0.05%) = 100.06994
    assert _tick(strategy, "100.12") is None
    assert _tick(strategy, "100.07") is None
    signal = _tick(strategy, "100.06")
    assert signal and signal.side == OrderSide.BUY


def test_a_restart_keeps_the_last_exit_price():
    strategy = _strategy()
    strategy.resume(T0 - timedelta(minutes=1), None, last_exit_price=Decimal(100))

    assert _tick(strategy, "99.96") is None  # rearma: ainda acima de 99.95
    signal = _tick(strategy, "99.95")
    assert signal and signal.side == OrderSide.BUY


def test_the_ledger_restores_the_last_sell_price():
    ledger = open_ledger()
    account = "paper:strategy:x"
    buy = make_intent(account=account)
    sell = make_intent(account=account, side=IntentSide.SELL)
    for intent in (buy, sell):
        ledger.record_intent(intent, PolicyDecision(True))
        ledger.mark_executed(intent.intent_id, ExecutionResult("s", "a", "b", 1, 1))
    assert ledger.last_exit_price(account) is None  # venda sem ordem
    order = Order("sell-1", SOL, USDC, Decimal(1), Decimal("101.5"), OrderSide.SELL, T0)
    ledger.attach_order(sell.intent_id, order)

    state = TradeGateway(ledger, Policy(), False).restore(account)

    assert ledger.last_exit_price(account) == Decimal("101.5")
    assert state.last_exit_price == Decimal("101.5")
    assert ledger.last_exit_price("paper:none") is None
