"""S3: um bot em execução segue o ledger (resoluções à mão, sobras, restore)."""

from datetime import datetime
from decimal import Decimal

from factories import open_ledger

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution import KillSwitch, TradeGateway
from trader.execution.gateway import open_entry_of
from trader.models import SOLANA_MINTS, OrderSide
from trader.models.intent import IntentSide, IntentStatus
from trader.paper import SimulatedWallet, paper_provider
from trader.policy import Policy
from trader.trading_service.protocol import (
    BucketStatus,
    OrderRequest,
    ReplyStatus,
    SwapRequest,
)
from trader.trading_service.service import TradeService

USDC = SOLANA_MINTS.get_by_symbol("USDC")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 9, 1, 12, 0)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
    max_daily_loss_usd=Decimal(100000),
)


def _service(tmp_path, ledger=None):
    client = ReplayQuoteClient(USDC, Decimal(0))
    client.tick = Tick(T0, Decimal("100"))
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    gateway = TradeGateway(
        ledger or open_ledger(), LOOSE, KillSwitch(tmp_path / "HALT"), False
    )
    provider = paper_provider(wallet, jupiter_client=client)
    return TradeService(provider, gateway, mode="paper", clock=lambda: T0), client


def _buy(qty="0.2", price="100"):
    return OrderRequest(OrderSide.BUY, Decimal(qty), Decimal(price))


def _sell(qty, price):
    return OrderRequest(OrderSide.SELL, Decimal(qty), Decimal(price))


async def test_a_buy_resolved_by_hand_is_seen_before_the_next_order(tmp_path):
    service, _ = _service(tmp_path)
    await service.open_bucket("b", USDC.mint, JUP.mint)
    ledger = service.gateway.ledger
    # o bot comprou, mas a intenção ficou sem confirmação (o livro segue vazio)
    first = await service.submit_order("b", _buy())
    assert first.order is not None
    record = ledger.list_intents()[0]
    ledger.conn.execute(
        "UPDATE intents SET status = 'unconfirmed' WHERE intent_id = ?",
        (record.intent.intent_id,),
    )
    ledger.conn.commit()
    service._bucket("b").account.book.position = None

    # o dono resolve como executada; o próximo sinal de compra não pode comprar
    ledger.resolve(record.intent.intent_id, IntentStatus.EXECUTED, "explorer")
    again = await service.submit_order("b", _buy())

    assert again.status == ReplyStatus.REJECTED  # já existe posição
    buys = [r for r in ledger.list_intents() if r.intent.side == IntentSide.BUY]
    assert len(buys) == 1


async def test_max_loss_on_a_partial_sell_closes_the_rest(tmp_path):
    service, client = _service(tmp_path)
    await service.open_bucket(
        "b", USDC.mint, JUP.mint, budget_usd=Decimal(50), max_loss_usd=Decimal(3)
    )
    bought = await service.submit_order("b", _buy())
    assert bought.order is not None
    client.tick = Tick(T0, Decimal("60"))

    half = bought.order.quantity / 2
    await service.submit_order("b", _sell(half, "60"))  # perde 4 USD: encerra

    snapshot = await service.get_bucket("b")
    assert snapshot.status == BucketStatus.RETIRING
    assert snapshot.position is None  # a sobra foi vendida, não abandonada


def test_restore_applies_partial_sells_one_at_a_time():
    """Compra 100, vende 60, vende 39.5: sobra 0.5 (> 1% dos 40 restantes)."""
    from factories import make_intent

    from trader.execution.fills import Fill
    from trader.execution.orders import order_from_fill
    from trader.models.intent import PolicyDecision
    from trader.models.order import SwapResult

    ledger = open_ledger()
    account = "paper:b"

    def leg(side, qty, closes=True):
        spend, receive = (USDC, JUP) if side == IntentSide.BUY else (JUP, USDC)
        intent = make_intent(
            account=account, side=side, spend_mint=spend.mint, receive_mint=receive.mint
        )
        ledger.record_intent(intent, PolicyDecision(True))
        raw_jup = JUP.ui_to_raw(qty)
        result = (
            SwapResult("s", USDC.mint, JUP.mint, USDC.ui_to_raw(qty), raw_jup)
            if side == IntentSide.BUY
            else SwapResult("s", JUP.mint, USDC.mint, raw_jup, USDC.ui_to_raw(qty))
        )
        ledger.mark_executed(intent.intent_id, result)
        order_side = OrderSide.BUY if side == IntentSide.BUY else OrderSide.SELL
        order = order_from_fill(Fill(result), USDC, JUP, order_side, T0)
        order.closes_position = closes
        ledger.attach_order(intent.intent_id, order)

    leg(IntentSide.BUY, "100")
    leg(IntentSide.SELL, "60", closes=False)
    leg(IntentSide.SELL, "39.5", closes=False)

    entry = open_entry_of(ledger, account)
    assert entry is not None and entry.quantity == Decimal("0.5")


async def test_a_manual_swap_whose_record_fails_still_reports_the_fill(tmp_path):
    service, _ = _service(tmp_path)
    service.gateway.record_fill = lambda *a, **k: (_ for _ in ()).throw(
        OSError("database is locked")
    )

    reply = await service.swap(SwapRequest(USDC.mint, JUP.mint, Decimal("10")))

    assert reply.status == ReplyStatus.FILLED  # senão o dono repetiria o swap
