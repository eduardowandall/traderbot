"""Paper de ponta a ponta com quotes reais: um bucket e o bot no websocket."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from factories import example_spec
from live_helpers import LOOSE_PAPER_POLICY

from trader.backtest import Backtester, TickRecorder, load_ticks
from trader.backtest.spec import result_to_dict
from trader.execution.ledger import Ledger, ledger_path
from trader.execution.models.intent import IntentStatus
from trader.execution.models.mode import RunningMode
from trader.execution.trading_service.local import LocalTradeClient
from trader.execution.wiring import build_trade_service
from trader.shared.market import JupiterMarketData
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.models.order import order_from_json
from trader.shared.notification import NotificationService
from trader.shared.paths import policy_file
from trader.shared.spec.models import StrategySpec
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus
from trader.strategy.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.strategy.bot.config import BotConfig
from trader.strategy.spec.strategy import SpecStrategy

BOT_SECONDS = 30
TOKEN, QUOTE = SOLANA_MINTS.get_pair("SOL-USDC")


def _busy_random() -> SpecStrategy:
    """O exemplo aleatório, mas comprando com 40% e vendendo com 20% por tick.

    O exemplo em si opera ~0.5% dos ticks: em 30s, muitas vezes nada.
    """
    spec = json.loads(Path(example_spec("random")).read_text(encoding="utf-8"))
    spec["entry"]["conditions"] = [{"type": "random_chance", "pct": 40}]
    spec["exit"]["conditions"] = [{"type": "random_chance", "pct": 20}]
    strategy = SpecStrategy(StrategySpec.model_validate(spec))
    strategy.seed(1)
    return strategy


def _records():
    with Ledger(ledger_path("paper")) as ledger:
        return ledger.list_intents(100)


def test_a_paper_round_trip_records_fills_and_costs():
    # política padrão do paper: uma compra de 5 USD e a venda dela
    asyncio.run(_round_trip())

    executed = [r for r in _records() if r.status == IntentStatus.EXECUTED]
    assert len(executed) == 2, [(r.status, r.error) for r in _records()]
    assert all(r.intent.account == "paper:live" for r in executed)
    assert all(r.order_json and r.fee_lamports for r in executed)
    sell = next(r for r in executed if r.intent.spend_mint == TOKEN.mint)
    assert sell.realized_pnl_usd is not None
    assert sell.order_json and order_from_json(sell.order_json).sol_usd
    # o fill de paper sai abaixo da quote real (B5), dentro da tolerância dela
    buy = next(r for r in executed if r.intent.spend_mint == QUOTE.mint)
    costs = order_from_json(buy.order_json or "").costs
    assert costs is not None and (costs.slippage_raw or 0) > 0, costs


async def _round_trip():
    service = build_trade_service(RunningMode.PAPER)
    with service.gateway:
        try:
            await service.open_bucket("live", QUOTE.mint, TOKEN.mint)
            data = JupiterMarketData()
            try:
                price = await data.get_price(TOKEN.mint)
            finally:
                await data.aclose()
            buy = await service.submit_order(
                "live", OrderRequest(OrderSide.BUY, Decimal(5) / price, price)
            )
            assert buy.status == ReplyStatus.FILLED, buy
            assert buy.order
            sell = await service.submit_order(
                "live", OrderRequest(OrderSide.SELL, buy.order.quantity, price)
            )
            assert sell.status == ReplyStatus.FILLED, sell
        finally:
            await service.aclose()


def test_paper_bot_trades_on_the_live_feed(tmp_path):
    policy_file().write_text(LOOSE_PAPER_POLICY, encoding="utf-8")
    ticks_file = tmp_path / "ticks.csv"

    asyncio.run(_run_bot(ticks_file))

    records = _records()
    executed = [r for r in records if r.status == IntentStatus.EXECUTED]
    assert executed, [(r.status, r.error, r.decision_reasons) for r in records]
    assert all(r.intent.account == "paper:SOL-USDC" for r in executed)
    assert not [r for r in records if r.status == IntentStatus.UNCONFIRMED]

    ticks = load_ticks(ticks_file)
    assert len(ticks) >= 5
    first, second = (result_to_dict(asyncio.run(_replay(ticks))) for _ in range(2))
    assert first == second  # replay determinístico


async def _run_bot(ticks_file):
    """Como `main.py run paper spec-random.json`, parando por `stop()`.

    Cancelar a task no meio de um swap deixaria a intenção UNCONFIRMED; o
    `stop()` só vale entre ticks.
    """
    strategy = _busy_random()
    service = build_trade_service(RunningMode.PAPER)
    trader = LocalTradeClient(
        service, "SOL-USDC", QUOTE.mint, TOKEN.mint, owns_service=True
    )
    with service.gateway, TickRecorder(ticks_file) as recorder:
        bot = AsyncWebsocketTradingBot(
            BotConfig(
                name="live-test",
                symbol="SOL-USDC",
                strategy=strategy,
                market=JupiterMarketData(),
                trader=trader,
                notifier=NotificationService(),
                on_tick=recorder.record,
            )
        )
        task = asyncio.create_task(bot.arun())
        await asyncio.sleep(BOT_SECONDS)
        bot.stop()
        await asyncio.wait_for(task, timeout=60)


async def _replay(ticks):
    strategy = _busy_random()
    return await Backtester(
        strategy, "SOL-USDC", ticks, initial_balance=Decimal(100)
    ).run()
