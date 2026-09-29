"""`run` (o bot ao vivo) e `backtest` (o mesmo caminho, sobre ticks)."""

import asyncio
from contextlib import nullcontext
from pathlib import Path

import typer

from trader.backtest import Backtester, TickRecorder, load_ticks, ticks_from_candles
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.cli.common import (
    check_symbol,
    get_notification_svc,
    get_strategy_obj,
    parse_decimal,
    warn,
)
from trader.market import JupiterMarketData
from trader.models import SOLANA_MINTS, Interval
from trader.models.mode import RunningMode
from trader.trading_service.local import LocalTradeClient
from trader.wiring import build_trade_service


def run(
    mode: RunningMode = typer.Argument(
        RunningMode.DRY, help="Modo de execucão do bot."
    ),
    currency: str = typer.Argument(
        "SOL-USDC", help="The trading symbol tuple (ex: SOL-USDC)"
    ),
    strategy: str = typer.Argument(..., help="The trading strategy to use"),
    notification_service: str = typer.Option(
        "null", help="Serviço de notificação: 'telegram' ou 'null'"
    ),
    notification_args: str | None = typer.Option(
        None, help="Argumentos do serviço de notificação"
    ),
    strategy_args: str | None = typer.Argument(None, help="Argumentos da estratégia"),
    record_ticks: Path | None = typer.Option(
        None, help="Grava cada preço recebido neste CSV (para `backtest --ticks`)"
    ),
):
    """
    Executa o bot em modo produção.

    Exemplos:
        uv run main.py run dry SOL-USDC random 'sell_chance=20 buy_chance=40'
        uv run main.py run dry SOL-USDC composer 'buy_mode=all sell_mode=any'
    """

    # estratégia primeiro: um erro nela não pode deixar ledger/conexões abertos
    strategy_obj = get_strategy_obj(strategy, strategy_args)
    check_symbol(strategy_obj, currency)
    token, quote = SOLANA_MINTS.get_pair(currency)
    notification_svc = get_notification_svc(notification_service, notification_args)

    # execução (modo, chave, ledger) de um lado; o bot só vê o bucket do par
    service = build_trade_service(mode, on_wallet_created=warn)
    trader = LocalTradeClient(
        service,
        # o nome do bucket é o par: a conta no ledger segue "<modo>:<par>"
        currency,
        quote.mint,
        token.mint,
        source=f"run-{strategy}",
        owns_service=True,
    )
    recorder = TickRecorder(record_ticks) if record_ticks else None
    config = BotConfig(
        name=f"{mode}-run-{strategy}",
        symbol=currency,
        strategy=strategy_obj,
        market=JupiterMarketData(),
        trader=trader,
        notifier=notification_svc,
        on_tick=recorder.record if recorder else None,
    )
    with service.gateway, recorder or nullcontext():
        _run_bot(AsyncWebsocketTradingBot(config))


def _run_bot(bot: AsyncWebsocketTradingBot) -> None:
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()


def backtest(
    symbol: str = typer.Argument(..., help="Par OUTPUT-INPUT, ex: SOL-USDC"),
    strategy: str = typer.Argument(..., help="Estratégia (random, composer, ...)"),
    strategy_args: str | None = typer.Argument(None, help="Argumentos da estratégia"),
    ticks: Path | None = typer.Option(
        None, help="CSV de ticks gravado com `run --record-ticks`"
    ),
    candles: int = typer.Option(
        0, min=0, help="Sem --ticks: baixa N candles da Jupiter e usa o fechamento"
    ),
    interval: Interval = typer.Option(Interval.MINUTE_1, help="Intervalo dos candles"),
    balance: str = typer.Option("100", help="Saldo inicial na stablecoin de entrada"),
    fee_bps: str = typer.Option("30", help="Custo por swap em bps (taxa + slippage)"),
    seed: str = typer.Option("0", help="Semente das estratégias aleatórias"),
):
    """
    Reproduz ticks por uma estratégia, com carteira simulada.

    Exemplos:
        uv run main.py backtest SOL-USDC random 'sell_chance=5 buy_chance=5' --ticks ticks.csv
        uv run main.py backtest SOL-USDC composer --candles 1000 --interval 1_MINUTE
    """
    if ticks is None and not candles:
        raise typer.BadParameter("informe --ticks ARQUIVO ou --candles N")
    strategy_obj = get_strategy_obj(strategy, strategy_args)
    if ticks is not None:
        tick_list = load_ticks(ticks)
    else:
        token, _ = SOLANA_MINTS.get_pair(symbol)
        tick_list = ticks_from_candles(
            asyncio.run(_fetch_candles(token.mint, interval, candles))
        )

    result = asyncio.run(
        Backtester(
            strategy_obj,
            symbol,
            tick_list,
            initial_balance=parse_decimal(balance, "balance"),
            fee_bps=parse_decimal(fee_bps, "fee-bps"),
            seed=seed,
        ).run()
    )
    typer.echo(result.summary())


async def _fetch_candles(mint: str, interval: Interval, qty: int):
    # só leitura de dados públicos: sem chave, sem RPC
    data = JupiterMarketData()
    try:
        return await data.get_candles(mint, interval, qty)
    finally:
        await data.aclose()
