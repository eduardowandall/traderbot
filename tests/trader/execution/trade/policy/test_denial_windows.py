"""A29: uma recusa por janela móvel diz quando a janela libera (`retry_after`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import make_intent, open_ledger, record_executed, served

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.models.intent import IntentSide
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import Policy, PolicyState, evaluate
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import (
    OrderReply,
    OrderRequest,
    ReplyStatus,
)
from trader.shared.trading_service.wire import reply_from_dict, reply_to_dict

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
USDC = SOLANA_MINTS.get_by_symbol("USDC")
JUP = SOLANA_MINTS.get_by_symbol("JUP")


def _ago(**delta) -> datetime:
    return NOW - timedelta(**delta)


def _wait(policy: Policy, state: PolicyState, side=IntentSide.BUY) -> float | None:
    decision = evaluate(make_intent(side), policy, state, real_mode=False)
    assert not decision.allowed
    return decision.retry_after


def test_daily_notional_frees_when_the_oldest_buy_leaves_the_24h():
    state = PolicyState(
        daily_notional_usd=Decimal(95),
        as_of=NOW,
        daily_notional_since=_ago(hours=20),
    )
    assert _wait(Policy(max_daily_notional_usd=Decimal(100)), state) == 4 * 3600


def test_trades_per_hour_wallet_and_bucket():
    wallet = PolicyState(
        trades_last_hour=2, as_of=NOW, trades_last_hour_since=_ago(minutes=50)
    )
    assert _wait(Policy(max_trades_per_hour=2), wallet) == 600

    bucket = PolicyState(
        account_trades_last_hour=1,
        as_of=NOW,
        account_trades_last_hour_since=_ago(minutes=59, seconds=30),
    )
    assert _wait(Policy(max_trades_per_hour_per_bucket=1), bucket) == 30


def test_daily_loss_frees_when_the_oldest_loss_leaves_the_24h():
    state = PolicyState(
        daily_realized_pnl_usd=Decimal(-25),
        as_of=NOW,
        daily_loss_since=_ago(hours=23, minutes=30),
    )
    assert _wait(Policy(max_daily_loss_usd=Decimal(20)), state) == 1800


def test_two_windows_wait_for_the_longer_and_never_below_a_second():
    state = PolicyState(
        trades_last_hour=10,
        daily_realized_pnl_usd=Decimal(-25),
        as_of=NOW,
        trades_last_hour_since=_ago(minutes=55),
        daily_loss_since=_ago(hours=24, seconds=5),  # já saiu: o piso de 1 s
    )
    policy = Policy(max_trades_per_hour=10, max_daily_loss_usd=Decimal(20))
    assert _wait(policy, state) == 300
    alone = PolicyState(
        daily_realized_pnl_usd=Decimal(-25),
        as_of=NOW,
        daily_loss_since=_ago(hours=24, seconds=5),
    )
    assert _wait(Policy(max_daily_loss_usd=Decimal(20)), alone) == 1


def test_denials_without_a_window_say_nothing():
    windows = PolicyState(as_of=NOW, trades_last_hour_since=_ago(minutes=1))
    # valor máximo: esperar não muda nada
    assert _wait(Policy(max_trade_usd=Decimal(5)), windows) is None
    # janela atingida, mas sem quando (um estado sem os timestamps)
    assert _wait(Policy(max_trades_per_hour=0), PolicyState()) is None
    # vendas não sofrem as janelas; o breaker não tem janela
    breaker = PolicyState(consecutive_failures=3, as_of=NOW)
    assert _wait(Policy(), breaker, IntentSide.SELL) is None


def test_the_ledger_reads_the_oldest_record_each_window_counts():
    ledger = open_ledger()
    now = datetime.now(UTC)
    for account, side, age in (
        ("paper:a", IntentSide.BUY, 50),
        ("paper:b", IntentSide.BUY, 30),
        ("paper:a", IntentSide.BUY, 10),
        ("paper:a", IntentSide.BUY, 300),  # fora da hora, dentro do dia
        ("paper:a", IntentSide.SELL, 55),  # vendas não contam
    ):
        intent = make_intent(
            side, account=account, created_at=now - timedelta(minutes=age)
        )
        record_executed(ledger, intent)

    state = ledger.policy_state(now=now, account="paper:b")

    assert state.as_of == now
    assert state.daily_notional_since == now - timedelta(minutes=300)
    assert state.trades_last_hour_since == now - timedelta(minutes=50)
    assert state.account_trades_last_hour_since == now - timedelta(minutes=30)
    assert ledger.policy_state(now=now).account_trades_last_hour_since is None


def test_the_ledger_dates_the_oldest_loss_not_the_oldest_win():
    ledger = open_ledger()
    now = datetime.now(UTC)
    for intent_id, pnl, age in (
        ("win", "3", 120),
        ("loss", "-5", 90),
        ("late", "-1", 10),
    ):
        sell = make_intent(IntentSide.SELL, intent_id=intent_id)
        record_executed(ledger, sell)
        ledger.conn.execute(
            "UPDATE intents SET realized_pnl_usd = ?, updated_at = ? WHERE intent_id = ?",
            (pnl, (now - timedelta(minutes=age)).isoformat(), intent_id),
        )
        ledger.conn.commit()

    state = ledger.policy_state(now=now)

    assert state.daily_realized_pnl_usd == Decimal(-3)
    assert state.daily_loss_since == now - timedelta(minutes=90)


async def test_a_connect_gets_the_wait_of_its_buckets_hour():
    # serve -> fio -> connect: a segunda compra da hora (depois de uma venda)
    # volta com a espera
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(datetime(2026, 9, 1, 12, 0), Decimal(1))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    policy = Policy(max_trade_usd=Decimal(1000), max_trades_per_hour_per_bucket=1)
    gateway = TradeGateway(open_ledger(), policy, False)
    service = TradeService(
        SpotVenue(paper_provider(wallet, jupiter_client=quotes)), gateway, mode="paper"
    )
    buy = OrderRequest(OrderSide.BUY, Decimal(5), Decimal(1), "teste")

    async with served(service, symbol="JUP-USDC", budget_usd=50) as trader:
        await trader.open()
        try:
            bought = (await trader.submit(buy)).order
            assert bought is not None
            sell = OrderRequest(OrderSide.SELL, bought.quantity, Decimal(1), "teste")
            assert (await trader.submit(sell)).filled
            denied = await trader.submit(buy)
        finally:
            await trader.aclose()

    assert denied.status == ReplyStatus.DENIED, denied.reasons
    assert "por hora do bucket" in denied.reasons[0]
    assert denied.retry_after is not None and 3590 <= denied.retry_after <= 3600


def test_the_wire_carries_retry_after_and_an_older_reply_has_none():
    reply = OrderReply.of_denial(("limite diário",), 7200.0)
    assert reply_from_dict(reply_to_dict(reply)) == reply

    older = {"status": "denied", "order": None, "reasons": ["x"], "error": None}
    assert reply_from_dict(older).retry_after is None
    assert OrderReply.of_denial(("x",)).retry_after is None
