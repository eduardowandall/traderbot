import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_intent

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import (
    IntentSide,
    IntentStatus,
    PolicyDecision,
)
from trader.execution.trade.ledger import Ledger
from trader.execution.trade.ledger.store import LedgerFormatError
from trader.shared.models import SOLANA_MINTS, Order, OrderSide
from trader.shared.models.order import order_from_json, order_to_json

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
ALLOW = PolicyDecision(True)


def _order(side=OrderSide.BUY, quantity="0.1", price="100"):
    return Order(
        order_id="sig",
        input_mint=USDC,
        output_mint=SOL,
        quantity=Decimal(quantity),
        price=Decimal(price),
        side=side,
        timestamp=datetime(2026, 9, 24, 12, 0),
        requested_quantity=Decimal("0.1"),
        requested_price=Decimal("100"),
        fill_price=Decimal("99.5"),
    )


def _executed(ledger, intent, order=None, pnl=None):
    ledger.record_intent(intent, ALLOW)
    ledger.mark_executed(intent.intent_id, ExecutionResult("sig", USDC, SOL, 1, 2))
    if order:
        ledger.attach_order(intent.intent_id, order, pnl)


def test_order_json_roundtrip():
    order = _order()
    assert order_from_json(order_to_json(order)) == order
    assert order_from_json(order_to_json(order)).fill_price == Decimal("99.5")


def test_records_intent_lifecycle(ledger):
    intent = make_intent()
    ledger.record_intent(intent, ALLOW)
    assert ledger.get(intent.intent_id).status == IntentStatus.EXECUTING

    ledger.mark_executed(intent.intent_id, ExecutionResult("sig", USDC, SOL, 10, 20))
    record = ledger.get(intent.intent_id)
    assert record.status == IntentStatus.EXECUTED
    assert (record.signature, record.in_amount, record.out_amount) == ("sig", 10, 20)
    assert record.intent == intent


def test_denied_intents_keep_reasons(ledger):
    intent = make_intent()
    ledger.record_intent(intent, PolicyDecision(False, ("limite",), "v1"))
    record = ledger.get(intent.intent_id)
    assert record.status == IntentStatus.DENIED
    assert record.decision_reasons == ("limite",)
    assert record.policy_version == "v1"


def test_update_unknown_intent_raises(ledger):
    with pytest.raises(KeyError):
        ledger.mark_failed("missing", "x")


class TestIdempotency:
    def test_executed_and_active_keys_are_found(self, ledger):
        for status_setter in ("mark_executed", "mark_unconfirmed", None):
            intent = make_intent(key=f"k-{status_setter}")
            ledger.record_intent(intent, ALLOW)
            if status_setter == "mark_executed":
                ledger.mark_executed(
                    intent.intent_id, ExecutionResult("s", "a", "b", 1, 1)
                )
            elif status_setter == "mark_unconfirmed":
                ledger.mark_unconfirmed(intent.intent_id, "timeout")
            assert ledger.find_by_idempotency_key(intent.idempotency_key)

    def test_failed_and_denied_keys_can_be_retried(self, ledger):
        failed = make_intent(key="k1")
        ledger.record_intent(failed, ALLOW)
        ledger.mark_failed(failed.intent_id, "sem rota")
        denied = make_intent(key="k2")
        ledger.record_intent(denied, PolicyDecision(False, ("x",)))

        assert ledger.find_by_idempotency_key("k1") is None
        assert ledger.find_by_idempotency_key("k2") is None


class TestPolicyState:
    def test_aggregates(self, ledger):
        _executed(ledger, make_intent(notional="10"), _order())
        _executed(ledger, make_intent(notional="5"), _order())
        sell = make_intent(side=IntentSide.SELL, notional="50")
        _executed(ledger, sell, _order(OrderSide.SELL), Decimal("-3"))
        denied = make_intent(notional="999")
        ledger.record_intent(denied, PolicyDecision(False, ("x",)))

        state = ledger.policy_state()
        # vendas e recusadas não consomem orçamento
        assert state.daily_notional_usd == Decimal("15")
        assert state.trades_last_hour == 2
        assert state.daily_realized_pnl_usd == Decimal("-3")
        assert state.consecutive_failures == 0
        assert state.unresolved_intent_ids == ()

    def test_windows_are_rolling(self, ledger):
        _executed(ledger, make_intent(notional="10"), _order())
        later = datetime.now(UTC) + timedelta(hours=2)
        assert ledger.policy_state(later).trades_last_hour == 0
        assert ledger.policy_state(later).daily_notional_usd == Decimal("10")
        next_day = datetime.now(UTC) + timedelta(hours=25)
        assert ledger.policy_state(next_day).daily_notional_usd == Decimal("0")

    def test_consecutive_failures_reset_by_success_and_restart(self, ledger):
        def fail():
            intent = make_intent()
            ledger.record_intent(intent, ALLOW)
            ledger.mark_failed(intent.intent_id, "erro")

        fail()
        _executed(ledger, make_intent())
        fail()
        fail()
        assert ledger.policy_state().consecutive_failures == 2

        # um processo novo só conta as falhas desde que começou
        started = datetime.now(UTC)
        assert ledger.policy_state(failures_since=started).consecutive_failures == 0

    def test_unresolved(self, ledger):
        executing, pending = make_intent(), make_intent()
        for intent in (executing, pending):
            ledger.record_intent(intent, ALLOW)
        ledger.mark_unconfirmed(pending.intent_id, "timeout")

        # executando agora em outra conta não bloqueia; sem confirmação, sim
        assert ledger.policy_state().unresolved_intent_ids == (pending.intent_id,)
        # na própria conta, executando bloqueia sempre (T1)
        own = ledger.policy_state(account=executing.account)
        assert set(own.unresolved_intent_ids) == {
            executing.intent_id,
            pending.intent_id,
        }
        later = datetime.now(UTC) + timedelta(minutes=6)
        state = ledger.policy_state(now=later)
        assert set(state.unresolved_intent_ids) == {
            executing.intent_id,
            pending.intent_id,
        }


def test_persists_across_connections(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    intent = make_intent()
    _executed(first, intent, _order())
    first.close()

    second = Ledger(path)
    record = second.get(intent.intent_id)
    assert record and record.status == IntentStatus.EXECUTED
    second.close()


class TestPositions:
    def test_realized_pnl_is_per_account(self, ledger):
        _executed(ledger, make_intent(), _order())
        sell = make_intent(side=IntentSide.SELL)
        _executed(ledger, sell, _order(OrderSide.SELL, price="110"), Decimal("1"))
        _executed(ledger, make_intent(account="paper:JUP-USDC"), _order())

        assert ledger.pnl_totals("paper:SOL-USDC").net_usd == Decimal("1")
        assert ledger.pnl_totals("paper:JUP-USDC").net_usd == Decimal("0")

    def test_a_late_mark_never_overwrites_a_final_status(self, ledger, caplog):
        intent = make_intent()
        ledger.record_intent(intent, ALLOW)
        ledger.mark_failed(intent.intent_id, "não saiu")

        assert not ledger.mark_executed(
            intent.intent_id, ExecutionResult("sig", USDC, SOL, 1, 2)
        )

        record = ledger.get(intent.intent_id)
        assert record and record.status == IntentStatus.FAILED
        assert "já está failed" in caplog.text
        # e a ordem (com PnL) de um swap que não conta não é gravada nela (T4)
        with pytest.raises(ValueError, match="não executed"):
            ledger.attach_order(intent.intent_id, _order(), Decimal("5"))
        assert ledger.pnl_totals(intent.account).net_usd == Decimal("0")


class TestEvents:
    def test_every_step_is_logged(self, ledger):
        _executed(ledger, make_intent(), _order())
        rows = ledger.conn.execute("SELECT type FROM events ORDER BY id")
        types = [r["type"] for r in rows]
        assert types == ["intent_executing", "intent_executed", "order_recorded"]

    def test_two_connections_write_to_the_same_log(self, tmp_path):
        path = tmp_path / "ledger.sqlite3"
        with Ledger(path) as first, Ledger(path) as second:
            first.add_event("a", {})
            second.add_event("b", {})
            types = [r["type"] for r in first.conn.execute("SELECT type FROM events")]
        assert types == ["a", "b"]


def test_an_old_format_file_is_refused(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE intents (intent_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(LedgerFormatError, match="mova ou apague"):
        Ledger(path)


class TestRepeatedDenials:
    TOO_BIG = PolicyDecision(False, ("trade de 50.00 USD acima do limite 25 USD",))

    def _events(self, ledger):
        return ledger.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    def test_identical_denials_collapse_into_one_row(self, ledger):
        ledger.record_intent(make_intent(), self.TOO_BIG)
        events = self._events(ledger)

        ledger.record_intent(make_intent(), self.TOO_BIG)
        ledger.record_intent(make_intent(), self.TOO_BIG)

        (record,) = ledger.list_intents()
        assert record.status == IntentStatus.DENIED
        assert record.repeat_count == 2
        assert self._events(ledger) == events  # repetições não viram eventos

    def test_other_reasons_sides_and_accounts_are_new_rows(self, ledger):
        other = PolicyDecision(False, ("limite de 10 trades por hora atingido",))
        ledger.record_intent(make_intent(), self.TOO_BIG)
        ledger.record_intent(make_intent(), other)
        ledger.record_intent(make_intent(side=IntentSide.SELL), other)
        ledger.record_intent(make_intent(account="paper:JUP-USDC"), other)

        records = ledger.list_intents()
        assert len(records) == 4
        assert all(r.repeat_count == 0 for r in records)

    def test_any_other_outcome_ends_the_streak(self, ledger):
        ledger.record_intent(make_intent(), self.TOO_BIG)
        _executed(ledger, make_intent())
        ledger.record_intent(make_intent(), self.TOO_BIG)

        denied = [r for r in ledger.list_intents() if r.status == IntentStatus.DENIED]
        assert len(denied) == 2
        assert all(r.repeat_count == 0 for r in denied)


def test_a_perp_send_round_trips_through_the_send_log():
    # A12: o envio gravado traz o pedido da perp de volta, tipado
    from factories import make_intent, open_ledger

    from trader.execution.models.intent import (
        PerpSend,
        PerpSendKind,
        PolicyDecision,
        SentTx,
    )

    ledger = open_ledger()
    intent = make_intent()
    ledger.record_intent(intent, PolicyDecision(True))
    send = PerpSend(PerpSendKind.CLOSE, "request", "position", before="30")
    sent = SentTx("sig", intent.spend_mint, intent.receive_mint, 1, 0, 9, perp=send)
    ledger.record_send(intent.intent_id, sent)
    assert ledger.sends_of(intent.intent_id) == [sent]
