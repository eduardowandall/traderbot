"""S3: sobras de vendas parciais e o restore a partir do ledger."""

from datetime import datetime
from decimal import Decimal

from factories import open_ledger

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.models.intent import IntentSide
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import (
    BucketStatus,
    OrderRequest,
)

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
    gateway = TradeGateway(ledger or open_ledger(), LOOSE, False)
    provider = paper_provider(wallet, jupiter_client=client)
    return TradeService(provider, gateway, mode="paper", clock=lambda: T0), client


def _buy(qty="0.2", price="100"):
    return OrderRequest(OrderSide.BUY, Decimal(qty), Decimal(price))


def _sell(qty, price):
    return OrderRequest(OrderSide.SELL, Decimal(qty), Decimal(price))


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


async def test_a_failing_remainder_sell_is_tried_once_per_order(tmp_path):
    # T2: a venda da sobra passava por submit_order -> _close_if_retiring de
    # novo e, falhando, recursava até o RecursionError
    service, client = _service(tmp_path)
    await service.open_bucket(
        "b", USDC.mint, JUP.mint, budget_usd=Decimal(50), max_loss_usd=Decimal(3)
    )
    bought = await service.submit_order("b", _buy())
    assert bought.order is not None
    client.tick = Tick(T0, Decimal("60"))
    provider = service.provider
    real_sell = provider.sell
    calls = []

    async def failing_sell(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:  # a primeira (a parcial) passa; a da sobra falha
            raise ValueError("abaixo do tamanho mínimo")
        return await real_sell(*args, **kwargs)

    provider.sell = failing_sell  # type: ignore[method-assign]
    half = bought.order.quantity / 2
    reply = await service.submit_order("b", _sell(half, "60"))

    assert reply.order is not None  # a venda pedida saiu
    assert len(calls) == 2  # e a sobra foi tentada uma vez só
    snapshot = await service.get_bucket("b")
    assert snapshot.status == BucketStatus.RETIRING
    assert snapshot.position is not None  # fica para a próxima ordem


def test_restore_applies_partial_sells_one_at_a_time():
    """Compra 100, vende 60, vende 39.5: sobra 0.5 (> 1% dos 40 restantes)."""
    from factories import make_intent

    from trader.execution.models.intent import PolicyDecision
    from trader.execution.trade.gateway.fills import Fill
    from trader.execution.trade.gateway.orders import order_from_fill
    from trader.shared.models.order import SwapResult

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

    entry = TradeGateway(ledger, LOOSE, False).restore(account).open_entry
    assert entry is not None and entry.quantity == Decimal("0.5")


async def test_a_bucket_that_never_traded_keeps_its_opening_time(tmp_path):
    # T4: o `ttl_days` contava da primeira intenção; sem trades, recomeçava a
    # cada reinício
    ledger = open_ledger()
    first, _ = _service(tmp_path, ledger)
    await first.open_bucket("b", USDC.mint, JUP.mint)
    opened = (await first.get_bucket("b")).opened_at
    assert opened is not None

    restarted, _ = _service(tmp_path, ledger)
    await restarted.open_bucket("b", USDC.mint, JUP.mint)

    assert (await restarted.get_bucket("b")).opened_at == opened
    events = ledger.conn.execute("SELECT type FROM events").fetchall()
    assert [r["type"] for r in events] == ["bucket_opened"]


def test_a_capped_sell_without_its_order_still_closes_on_restore():
    # T4: a venda limitada ao saldo fecha a posição na memória; sem a ordem
    # gravada, o restore reabria a sobra (impossível de vender) pela quantidade
    from factories import make_intent

    from trader.execution.models.intent import PolicyDecision
    from trader.execution.trade.gateway.fills import Fill
    from trader.execution.trade.gateway.orders import order_from_fill
    from trader.shared.models.order import SwapResult

    ledger = open_ledger()
    account = "paper:b"
    buy = make_intent(account=account, spend_mint=USDC.mint, receive_mint=JUP.mint)
    ledger.record_intent(buy, PolicyDecision(True))
    result = SwapResult(
        "s", USDC.mint, JUP.mint, USDC.ui_to_raw("100"), JUP.ui_to_raw("100")
    )
    ledger.mark_executed(buy.intent_id, result)
    ledger.attach_order(
        buy.intent_id, order_from_fill(Fill(result), USDC, JUP, OrderSide.BUY, T0)
    )

    def sell(closes):
        intent = make_intent(
            account=account,
            side=IntentSide.SELL,
            spend_mint=JUP.mint,
            receive_mint=USDC.mint,
            quantity=Decimal("90"),  # o saldo tinha só 90 dos 100
            closes_position=closes,
        )
        ledger.record_intent(intent, PolicyDecision(True))
        ledger.mark_executed(
            intent.intent_id, SwapResult("t", JUP.mint, USDC.mint, 1, 1)
        )
        # sem attach_order: o record_fill falhou

    restore = TradeGateway(ledger, LOOSE, False).restore
    sell(closes=True)
    assert restore(account).open_entry is None
