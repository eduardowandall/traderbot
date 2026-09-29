import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_intent

import trader.ledger.store as ledger_module
from trader.ledger import Ledger
from trader.models import SOLANA_MINTS, Order, OrderSide, SwapResult
from trader.models.intent import (
    IntentSide,
    IntentStatus,
    PolicyDecision,
)
from trader.models.order import order_from_json, order_to_json

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
    ledger.mark_executed(intent.intent_id, SwapResult("sig", USDC, SOL, 1, 2))
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

    ledger.mark_executed(intent.intent_id, SwapResult("sig", USDC, SOL, 10, 20))
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
                ledger.mark_executed(intent.intent_id, SwapResult("s", "a", "b", 1, 1))
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


class TestHashChain:
    def test_intact_chain(self, ledger):
        _executed(ledger, make_intent(), _order())
        assert ledger.verify_chain() is None

    def test_detects_tampering(self, ledger):
        _executed(ledger, make_intent(), _order())
        ledger.conn.execute("UPDATE events SET payload = '{}' WHERE id = 2")
        assert ledger.verify_chain() == 2

    def test_detects_deleted_event(self, ledger):
        _executed(ledger, make_intent(), _order())
        ledger.conn.execute("DELETE FROM events WHERE id = 1")
        assert ledger.verify_chain() == 2

    def test_concurrent_writers_do_not_fork_the_chain(self, tmp_path, monkeypatch):
        # a CLI (`halt`) e os runners gravam no mesmo arquivo: um segundo
        # processo que grava entre a leitura do último hash e o INSERT do
        # primeiro não pode fazer os dois eventos apontarem para o mesmo
        # prev_hash
        path = tmp_path / "ledger.sqlite3"
        real_hash = ledger_module._event_hash
        other_writer: list[threading.Thread] = []

        def other_process_writes():
            with Ledger(path) as other:
                other.add_event("halt", {"reason": "cli"})

        def hash_then_let_other_write(*args):
            if not other_writer:
                other_writer.append(threading.Thread(target=other_process_writes))
                other_writer[0].start()
                # sem o lock, o outro escritor termina neste intervalo
                other_writer[0].join(timeout=0.5)
            return real_hash(*args)

        monkeypatch.setattr(ledger_module, "_event_hash", hash_then_let_other_write)
        with Ledger(path) as first:
            first.add_event("resume", {"note": "bot"})
            other_writer[0].join()
            types = [r["type"] for r in first.conn.execute("SELECT type FROM events")]
            assert sorted(types) == ["halt", "resume"]
            assert first.verify_chain() is None


class TestPositions:
    def test_last_executed_trade_and_pnl(self, ledger):
        _executed(ledger, make_intent(), _order())
        sell = make_intent(side=IntentSide.SELL)
        _executed(ledger, sell, _order(OrderSide.SELL, price="110"), Decimal("1"))
        other = make_intent(account="dry:JUP-USDC")
        _executed(ledger, other, _order())

        last = ledger.last_executed_trade("dry:SOL-USDC")
        assert last and last.intent.intent_id == sell.intent_id
        assert ledger.total_realized_pnl("dry:SOL-USDC") == Decimal("1")
        assert ledger.total_realized_pnl("dry:JUP-USDC") == Decimal("0")

    def test_swaps_are_not_positions(self, ledger):
        _executed(ledger, make_intent(side=IntentSide.SWAP), _order())
        assert ledger.last_executed_trade("dry:SOL-USDC") is None


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

    def test_consecutive_failures_reset_by_success_and_resume(self, ledger):
        def fail():
            intent = make_intent()
            ledger.record_intent(intent, ALLOW)
            ledger.mark_failed(intent.intent_id, "erro")

        fail()
        _executed(ledger, make_intent())
        fail()
        fail()
        assert ledger.policy_state().consecutive_failures == 2

        ledger.add_event("resume", {})
        assert ledger.policy_state().consecutive_failures == 0

    def test_unresolved(self, ledger):
        intent = make_intent()
        ledger.record_intent(intent, ALLOW)
        assert ledger.policy_state().unresolved_intent_ids == (intent.intent_id,)


class TestResolve:
    def test_resolves_unconfirmed(self, ledger):
        intent = make_intent()
        ledger.record_intent(intent, ALLOW)
        ledger.mark_unconfirmed(intent.intent_id, "timeout")

        ledger.resolve(intent.intent_id, IntentStatus.FAILED, "não está no explorer")

        record = ledger.get(intent.intent_id)
        assert record.status == IntentStatus.FAILED
        assert record.error == "não está no explorer"
        assert ledger.policy_state().unresolved_intent_ids == ()

    def test_rejects_invalid_resolutions(self, ledger):
        intent = make_intent()
        _executed(ledger, intent)
        with pytest.raises(ValueError):
            ledger.resolve(intent.intent_id, IntentStatus.FAILED, "x")
        with pytest.raises(ValueError):
            ledger.resolve(intent.intent_id, IntentStatus.DENIED, "x")
        with pytest.raises(KeyError):
            ledger.resolve("missing", IntentStatus.FAILED, "x")


def test_persists_across_connections(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    intent = make_intent()
    _executed(first, intent, _order())
    first.close()

    second = Ledger(path)
    record = second.get(intent.intent_id)
    assert record and record.status == IntentStatus.EXECUTED
    assert second.verify_chain() is None
    second.close()


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
        assert ledger.verify_chain() is None

    def test_other_reasons_sides_and_accounts_are_new_rows(self, ledger):
        other = PolicyDecision(False, ("limite de 10 trades por hora atingido",))
        ledger.record_intent(make_intent(), self.TOO_BIG)
        ledger.record_intent(make_intent(), other)
        ledger.record_intent(make_intent(side=IntentSide.SWAP), other)
        ledger.record_intent(make_intent(account="dry:JUP-USDC"), other)

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
