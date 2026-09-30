"""`run` (o bot ao vivo) e `backtest` (o mesmo caminho, sobre ticks)."""

import asyncio
from contextlib import nullcontext
from pathlib import Path

import typer

from trader.agent_api.strategies import limits_from_policy
from trader.backtest import Backtester, TickRecorder, load_ticks, ticks_from_candles
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.cli.common import (
    get_notification_svc,
    load_spec_strategy,
    parse_decimal,
    warn,
)
from trader.market import JupiterMarketData
from trader.models import SOLANA_MINTS, Interval
from trader.models.mode import RunningMode
from trader.policy import load_policy
from trader.strategy_spec.strategy import SpecStrategy
from trader.strategy_spec.validate import validate
from trader.trading_service.local import LocalTradeClient
from trader.wiring import build_trade_service

SPEC_HELP = "Arquivo JSON da spec (o par vem dela), ex: docs/examples/spec-random.json"


def run(
    mode: RunningMode = typer.Argument(
        RunningMode.DRY, help="Modo de execucão do bot."
    ),
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    seed: str | None = typer.Option(None, help="Semente do `random_chance`"),
    notification_service: str = typer.Option(
        "null", help="Serviço de notificação: 'telegram' ou 'null'"
    ),
    notification_args: str | None = typer.Option(
        None, help="Argumentos do serviço de notificação"
    ),
    record_ticks: Path | None = typer.Option(
        None, help="Grava cada preço recebido neste CSV (para `backtest --ticks`)"
    ),
):
    """
    Executa o bot com uma spec, no bucket dela (`strategy:<spec_id>`).

    Exemplos:
        uv run main.py run paper docs/examples/spec-random.json --seed 1
        uv run main.py run dry docs/examples/spec-wma-composer.json
    """

    # estratégia primeiro: um erro nela não pode deixar ledger/conexões abertos
    strategy = load_spec_strategy(spec_file, seed)
    spec = strategy.spec
    token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    notification_svc = get_notification_svc(notification_service, notification_args)
    _check_limits(strategy, mode)

    # execução (modo, chave, ledger) de um lado; o bot só vê o bucket dele
    service = build_trade_service(mode, on_wallet_created=warn)
    trader = LocalTradeClient(
        service,
        f"strategy:{strategy.spec_id}",
        quote.mint,
        token.mint,
        budget_usd=spec.budget_usd,
        source="run-spec",
        owns_service=True,
        max_loss_usd=spec.max_loss_usd,
    )
    recorder = TickRecorder(record_ticks) if record_ticks else None
    config = BotConfig(
        name=f"{mode}-run-{spec.name}",
        symbol=spec.symbol,
        strategy=strategy,
        market=JupiterMarketData(),
        trader=trader,
        notifier=notification_svc,
        on_tick=recorder.record if recorder else None,
    )
    with service.gateway, recorder or nullcontext():
        _run_bot(AsyncWebsocketTradingBot(config))


def _check_limits(strategy: SpecStrategy, mode: RunningMode) -> None:
    """A spec precisa caber na política do modo antes de rodar."""
    errors = validate(strategy.spec, limits_from_policy(load_policy(mode=str(mode))))
    if errors:
        details = "; ".join(f"{e.path}: {e.msg}" for e in errors)
        raise typer.BadParameter(f"spec inválida para {mode}: {details}")


def _run_bot(bot: AsyncWebsocketTradingBot) -> None:
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()


def backtest(
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    ticks: Path | None = typer.Option(
        None, help="CSV de ticks gravado com `run --record-ticks`"
    ),
    candles: int = typer.Option(
        0, min=0, help="Sem --ticks: baixa N candles da Jupiter e usa o fechamento"
    ),
    interval: Interval | None = typer.Option(
        None, help="Intervalo dos candles (padrão: o `timeframe` da spec)"
    ),
    balance: str = typer.Option("100", help="Saldo inicial na stablecoin de entrada"),
    fee_bps: str = typer.Option("30", help="Custo por swap em bps"),
    slippage_bps: str = typer.Option("10", help="Desvio de preço por perna, em bps"),
    seed: str = typer.Option("0", help="Semente do `random_chance`"),
):
    """
    Reproduz ticks por uma spec, com carteira simulada.

    Exemplos:
        uv run main.py backtest docs/examples/spec-random.json --ticks ticks.csv
        uv run main.py backtest docs/examples/spec-wma-composer.json --candles 1000
    """
    if ticks is None and not candles:
        raise typer.BadParameter("informe --ticks ARQUIVO ou --candles N")
    strategy = load_spec_strategy(spec_file)
    symbol = strategy.spec.symbol
    if ticks is not None:
        tick_list = load_ticks(ticks)
    else:
        token, _ = SOLANA_MINTS.get_pair(symbol)
        interval = interval or strategy.spec.timeframe
        tick_list = ticks_from_candles(
            asyncio.run(_fetch_candles(token.mint, interval, candles)), interval
        )

    result = asyncio.run(
        Backtester(
            strategy,
            symbol,
            tick_list,
            initial_balance=parse_decimal(balance, "balance"),
            fee_bps=parse_decimal(fee_bps, "fee-bps"),
            slippage_bps=parse_decimal(slippage_bps, "slippage-bps"),
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
