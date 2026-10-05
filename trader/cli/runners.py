"""`serve` (o trade-runner) e `connect` (um strategy-runner por spec).

Para várias specs ao mesmo tempo com a chave num processo só: um
`serve <modo>` e um `connect spec.json` por spec. `connect` não precisa de
chave nem de RPC (`docs/plan.md` B3).
"""

import asyncio
import json
from pathlib import Path

import typer

from trader.cli.bot import MARKET_DATA, SPEC_HELP, limits_from_policy, mode_lock
from trader.market.hub import PriceHub
from trader.market.prices import price_fn
from trader.models.mode import RunningMode
from trader.notification import notifier_from_env
from trader.notification.daily_report import DailyReporter
from trader.policy import load_policy
from trader.runners.strategy_runner import strategy_bot
from trader.runners.trade_runner import TradeRunner
from trader.strategy_spec.strategy import SpecStrategy
from trader.trading_service.service import TradeService
from trader.wiring import build_trade_service


def serve(mode: RunningMode = typer.Argument(..., help="real ou paper")):
    """
    O trade-runner do modo: a chave, a carteira e o ledger num processo só.

    Strategy-runners (`connect`) se ligam por 127.0.0.1; o endereço e o token
    ficam em `.data/trader-<modo>.json` enquanto ele roda. Ctrl+C para.
    """
    with mode_lock(mode):
        # um feed de preços para todos os `connect` e para o próprio serviço
        hub = PriceHub()
        service = build_trade_service(mode, prices=hub)
        # vender sobras sem o strategy-runner, marcar posições
        price_of = price_fn(hub)
        reporter = DailyReporter(
            service.gateway, str(mode), notifier_from_env(), price_of
        )
        runner = TradeRunner(
            service,
            str(mode),
            limits_from_policy(load_policy(mode=str(mode))),
            price_of=price_of,
            hub=hub,
            background=(hub.run, reporter.run_forever),
        )
        with service.gateway:
            try:
                asyncio.run(_serve(runner, service, reporter))
            except KeyboardInterrupt:
                typer.echo(f"Trade-runner {mode} parado", err=True)


async def _serve(
    runner: TradeRunner, service: TradeService, reporter: DailyReporter
) -> None:
    try:
        await runner.serve()
    finally:
        await reporter.notifier.aclose()
        await service.aclose()


def connect(
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    trader: Path | None = typer.Option(
        None, help="Arquivo de conexão (padrão: o único .data/trader-*.json)"
    ),
    seed: str | None = typer.Option(None, help="Semente do `random_chance`"),
):
    """
    Roda uma spec ligada ao trade-runner (`serve`), sem chave e sem RPC.

    O trade-runner valida a spec com a política dele e executa as ordens.
    """
    text = spec_file.read_text(encoding="utf-8")
    strategy = SpecStrategy.from_file(str(spec_file))
    if seed is not None:
        strategy.seed(seed)
    bot = strategy_bot(
        strategy,
        json.loads(text),
        trader,
        MARKET_DATA,  # candles do aquecimento; os preços vêm do trade-runner
        notifier_from_env(),
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
