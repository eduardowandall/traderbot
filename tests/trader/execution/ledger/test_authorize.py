"""R2: idempotência + política + registro são atômicos entre processos."""

import sqlite3
import threading
from decimal import Decimal

import pytest
from factories import make_intent

from trader.execution.ledger import Ledger
from trader.execution.models.intent import IntentStatus, PolicyDecision
from trader.execution.policy import Policy, evaluate

DAILY_20 = Policy(max_daily_notional_usd=Decimal("20"))


def _decide(intent):
    return lambda state: evaluate(intent, DAILY_20, state, real_mode=False)


def test_two_processes_cannot_both_pass_one_limit(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    outcome = {}
    other = make_intent(notional="15", spend_amount="15")

    def authorize_second():
        # outro "processo": conexão própria, aberta na própria thread
        with Ledger(path) as second:
            outcome["second"] = second.authorize(other, _decide(other))

    thread = threading.Thread(target=authorize_second)

    def decide_first(state):
        # com o lock tomado, o outro processo espera em vez de ler o estado
        thread.start()
        thread.join(timeout=0.3)
        outcome["waited"] = thread.is_alive()
        return evaluate(mine, DAILY_20, state, real_mode=False)

    mine = make_intent(notional="15", spend_amount="15")
    try:
        _, decision = first.authorize(mine, decide_first)
        thread.join(timeout=10)
    finally:
        first.close()

    assert decision and decision.allowed
    assert outcome["waited"]
    _, second_decision = outcome["second"]
    assert not second_decision.allowed  # viu os 15 USD do primeiro
    # a intenção do primeiro já conta: em execução e no limite de 24h
    assert any("24h" in r or "diário" in r for r in second_decision.reasons)


def test_a_used_key_returns_the_existing_intent_and_writes_nothing(ledger):
    intent = make_intent(key="k")
    ledger.authorize(intent, _decide(intent))

    existing, decision = ledger.authorize(make_intent(key="k"), _decide(intent))

    assert decision is None
    assert existing and existing.intent.intent_id == intent.intent_id
    assert len(ledger.list_intents()) == 1


def test_the_unique_index_blocks_a_second_moved_funds_intent(ledger):
    ledger.record_intent(make_intent(key="k"), PolicyDecision(True))

    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_intent(make_intent(key="k"), PolicyDecision(True))


def test_failed_and_denied_keys_do_not_count_for_the_index(ledger):
    first = make_intent(key="k")
    ledger.record_intent(first, PolicyDecision(True))
    ledger.mark_failed(first.intent_id, "quote falhou")

    ledger.record_intent(make_intent(key="k"), PolicyDecision(True))  # nova tentativa

    statuses = sorted(r.status for r in ledger.list_intents())
    assert statuses == sorted([IntentStatus.FAILED, IntentStatus.EXECUTING])
