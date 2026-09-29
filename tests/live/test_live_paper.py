"""Paper de ponta a ponta com quotes reais: swaps manuais e o bot no websocket."""

import asyncio
from decimal import Decimal

from live_helpers import LOOSE_PAPER_POLICY, invoke

from trader.backtest import Backtester, TickRecorder, load_ticks
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.ledger import Ledger, ledger_path, order_from_json
from trader.market import JupiterMarketData
from trader.models import SOLANA_MINTS
from trader.models.intent import IntentStatus
from trader.models.mode import RunningMode
from trader.notification.notification_service import NullNotificationService
from trader.paths import policy_file
from trader.strategies_registry import get_strategy_factory
from trader.trading_service.local import LocalTradeClient
from trader.wiring import build_trade_service

BOT_SECONDS = 30


def _records():
    with Ledger(ledger_path("paper")) as ledger:
        return ledger.list_intents(100), ledger.verify_chain()


def test_manual_swaps_record_fills_and_costs():
    # política padrão (25 USD por trade, valor desconhecido recusado): o
    # SOL -> JUP passa porque a Price API dá o valor em USD do SOL gasto
    bought = invoke("swap", "paper", "USDC", "SOL", "5")
    spent_sol = invoke("swap", "paper", "SOL", "JUP", "0.01")
    denied = invoke("swap", "paper", "USDC", "SOL", "50")

    assert bought.exit_code == 0, bought.output
    assert "gasto 5 USDC" in bought.stdout
    assert "custos [simulated]" in bought.stdout
    assert "~$" in bought.stdout  # par com stablecoin: custo em USD
    assert spent_sol.exit_code == 0, spent_sol.output
    assert "~$" in spent_sol.stdout  # sem stablecoin: custo em USD pela API
    assert denied.exit_code == 1
    assert "acima do limite" in denied.stderr

    records, broken = _records()
    assert broken is None
    executed = [r for r in records if r.status == IntentStatus.EXECUTED]
    assert len(executed) == 2
    assert all(r.intent.account == "paper:manual" for r in executed)
    assert all(r.order_json and r.fee_lamports for r in executed)
    # a primeira conta de JUP da carteira paga rent
    jup = next(r for r in executed if r.intent.spend_mint == _mint("SOL"))
    assert jup.rent_lamports and jup.rent_lamports > 0
    assert jup.intent.notional_usd and 0 < jup.intent.notional_usd < 25
    assert jup.order_json and order_from_json(jup.order_json).sol_usd

    pnl = invoke("pnl", "paper")
    assert pnl.exit_code == 0
    assert "paper:manual: 2 pernas" in pnl.stdout


def test_paper_bot_trades_on_the_live_feed(tmp_path):
    policy_file().write_text(LOOSE_PAPER_POLICY, encoding="utf-8")
    ticks_file = tmp_path / "ticks.csv"

    asyncio.run(_run_bot(ticks_file))

    records, broken = _records()
    assert broken is None
    executed = [r for r in records if r.status == IntentStatus.EXECUTED]
    assert executed, [(r.status, r.error, r.decision_reasons) for r in records]
    assert all(r.intent.account == "paper:SOL-USDC" for r in executed)
    assert not [r for r in records if r.status == IntentStatus.UNCONFIRMED]

    ticks = load_ticks(ticks_file)
    assert len(ticks) >= 5
    first, second = (asyncio.run(_replay(ticks)) for _ in range(2))
    assert first.summary() == second.summary()  # replay determinístico


async def _run_bot(ticks_file):
    """Como `main.py run paper SOL-USDC random`, parando por `stop()`.

    Cancelar a task no meio de um swap deixaria a intenção UNCONFIRMED; o
    `stop()` só vale entre ticks.
    """
    token, quote = SOLANA_MINTS.get_pair("SOL-USDC")
    strategy = get_strategy_factory("random")(sell_chance="50", buy_chance="50")
    service = build_trade_service(RunningMode.PAPER)
    trader = LocalTradeClient(
        service, "SOL-USDC", quote.mint, token.mint, owns_service=True
    )
    with service.gateway, TickRecorder(ticks_file) as recorder:
        bot = AsyncWebsocketTradingBot(
            BotConfig(
                name="live-test",
                symbol="SOL-USDC",
                strategy=strategy,
                market=JupiterMarketData(),
                trader=trader,
                notifier=NullNotificationService(),
                on_tick=recorder.record,
            )
        )
        task = asyncio.create_task(bot.arun())
        await asyncio.sleep(BOT_SECONDS)
        bot.stop()
        await asyncio.wait_for(task, timeout=60)


async def _replay(ticks):
    strategy = get_strategy_factory("random")(sell_chance="50", buy_chance="50")
    return await Backtester(
        strategy, "SOL-USDC", ticks, initial_balance=Decimal(100)
    ).run()


def _mint(symbol: str) -> str:
    return SOLANA_MINTS.get_by_symbol(symbol).mint
