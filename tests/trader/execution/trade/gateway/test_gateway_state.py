from datetime import datetime
from decimal import Decimal

from factories import make_intent, open_ledger

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentSide, PolicyDecision
from trader.execution.models.mode import RunningMode
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger import Ledger, ledger_path
from trader.execution.trade.policy import Policy
from trader.shared.models import SOLANA_MINTS, Order, OrderSide
from trader.shared.paths import policy_file

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint


def _gateway(tmp_path):
    return TradeGateway(open_ledger(), Policy(), False)


def _order(side):
    return Order("sig", USDC, SOL, Decimal("0.1"), Decimal("100"), side, datetime.now())


def _fill(gateway, side, account="paper:strategy:abc"):
    intent = make_intent(side=side, account=account)
    gateway.ledger.record_intent(intent, PolicyDecision(True))
    gateway.ledger.mark_executed(
        intent.intent_id, ExecutionResult("sig", USDC, SOL, 1, 2)
    )
    gateway.record_fill(intent.intent_id, _order(OrderSide(side)), Decimal("1"))
    return intent


def test_restore_reports_the_open_position(tmp_path):
    gateway = _gateway(tmp_path)
    buy = _fill(gateway, IntentSide.BUY)

    state = gateway.restore("paper:strategy:abc")
    assert state.open_entry is not None and state.open_entry.side == OrderSide.BUY
    assert state.entry_intent_id == buy.intent_id

    _fill(gateway, IntentSide.SELL)
    closed = gateway.restore("paper:strategy:abc")
    assert closed.open_entry is None and closed.entry_intent_id is None


def test_restore_of_an_unknown_account_is_empty(tmp_path):
    state = _gateway(tmp_path).restore("paper:nothing")
    assert state.open_entry is None
    assert state.realized_usd == Decimal("0")


def test_for_mode_uses_the_mode_ledger_and_policy():
    policy_file().write_text("[paper.limits]\nmax_trade_usd = 7\n", encoding="utf-8")
    with TradeGateway.for_mode(RunningMode.PAPER) as gateway:
        assert gateway.policy.max_trade_usd == Decimal("7")
        assert gateway.real_mode is False
        gateway.add_event("hello", {"x": 1})
    with Ledger(ledger_path(RunningMode.PAPER)) as ledger:
        types = [r["type"] for r in ledger.conn.execute("SELECT type FROM events")]
        assert types == ["hello"]


def test_for_mode_with_explicit_policy_ignores_a_broken_policy_file():
    # uma política explícita nunca lê o policy.toml
    policy_file().write_text("this is [not toml", encoding="utf-8")
    with TradeGateway.for_mode(RunningMode.REAL, policy=Policy()) as gateway:
        assert gateway.real_mode is True
        gateway.add_event("teste", {})
