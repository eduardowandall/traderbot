"""Limite de compras por hora de um bucket (B7), ao lado do limite da carteira."""

from datetime import UTC, datetime, timedelta

from factories import make_intent, open_ledger, record_executed

from trader.execution.models.intent import IntentSide
from trader.execution.policy import Policy, PolicyState, evaluate, load_policy
from trader.shared.paths import policy_file


def test_a_bucket_at_its_hourly_limit_cannot_buy_but_can_sell():
    policy = Policy(max_trades_per_hour_per_bucket=2)
    full = PolicyState(account_trades_last_hour=2)

    buy = evaluate(make_intent(), policy, full, real_mode=False)
    sell = evaluate(make_intent(IntentSide.SELL), policy, full, real_mode=False)

    assert buy.reasons == ("limite de 2 trades por hora do bucket atingido",)
    assert sell.allowed
    below = PolicyState(account_trades_last_hour=1)
    assert evaluate(make_intent(), policy, below, real_mode=False).allowed


def test_the_ledger_counts_only_this_buckets_recent_buys():
    ledger = open_ledger()
    now = datetime.now(UTC)
    for account, side, age in (
        ("paper:a", IntentSide.BUY, 10),
        ("paper:a", IntentSide.BUY, 50),
        ("paper:a", IntentSide.SELL, 5),  # vendas não contam
        ("paper:a", IntentSide.BUY, 70),  # mais de uma hora
        ("paper:b", IntentSide.BUY, 1),  # outro bucket
    ):
        created = now - timedelta(minutes=age)
        record_executed(ledger, make_intent(side, account=account, created_at=created))

    assert ledger.policy_state(account="paper:a").account_trades_last_hour == 2
    assert ledger.policy_state(account="paper:b").account_trades_last_hour == 1
    assert ledger.policy_state().account_trades_last_hour == 0
    assert ledger.policy_state().trades_last_hour == 3  # a carteira toda


def test_defaults_and_the_toml_key():
    assert Policy().max_trades_per_hour_per_bucket == 6
    assert load_policy(mode="paper").max_trades_per_hour_per_bucket == 60
    policy_file().write_text(
        "[real.limits]\nmax_trades_per_hour_per_bucket = 3\n", encoding="utf-8"
    )
    assert load_policy(mode="real").max_trades_per_hour_per_bucket == 3
    assert Policy.unlimited().max_trades_per_hour_per_bucket > 10**6
