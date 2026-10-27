"""`serve` (o trade-runner), `connect` (um strategy-runner por spec) e `stop`.

A única forma de operar (B14): um `serve <modo>`, com a chave, a carteira e o
ledger, e um `connect spec.json` por spec. `connect` não precisa de chave nem
de RPC (`docs/history.md` B3); o trade-runner confere a spec com a política dele.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

import typer

from trader.api.cli.backtest import SPEC_HELP
from trader.api.cli.lock import ModeBusyError, ModeLock
from trader.backtest import TickRecorder
from trader.execution.market import JupiterCandles
from trader.execution.market.hub import PriceHub
from trader.execution.market.prices import price_fn
from trader.execution.models.mode import RunningMode
from trader.execution.notification.daily_report import DailyReporter
from trader.execution.runner import TradeRunner
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.wiring import build_trade_service
from trader.shared.notification import notifier_from_env
from trader.shared.spec.validate import SpecLimits
from trader.shared.trading_service.protocol import TradeServiceError
from trader.strategy.runner import find_connection, strategy_bot
from trader.strategy.spec.strategy import SpecStrategy
from trader.strategy.trading_service.remote import stop_specs


def serve(mode: RunningMode = typer.Argument(..., help="real ou paper")):
    """
    O trade-runner do modo: a chave, a carteira e o ledger num processo só.

    Strategy-runners (`connect`) se ligam por 127.0.0.1; o endereço e o token
    ficam em `.data/trader-<modo>.json` enquanto ele roda. Ctrl+C para.
    """
    with mode_lock(mode):
        # um feed de preços para todos os `connect` e para o próprio serviço
        hub = PriceHub()
        # um Telegram para o relatório diário, os avisos do serviço e as quedas
        notifier = notifier_from_env()
        service = build_trade_service(
            mode,
            on_wallet_created=lambda m: typer.echo(m, err=True),
            prices=hub,
            notifier=notifier,
        )
        # o relatório marca as posições a mercado pelo hub
        reporter = DailyReporter(service.gateway, str(mode), notifier, price_fn(hub))
        runner = TradeRunner(
            service,
            str(mode),
            limits_from_policy(service.gateway.policy),
            hub=hub,
            # candles do aquecimento dos `connect` (só este processo lê a Jupiter)
            candles=JupiterCandles(),
            background=(reporter.run_forever,),
            # quedas de preço/perps com posição aberta (A28)
            notifier=notifier,
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
    record_ticks: Path | None = typer.Option(
        None, help="Grava cada preço recebido neste CSV (para `backtest --ticks`)"
    ),
):
    """
    Roda uma spec ligada ao trade-runner (`serve`), sem chave e sem RPC.

    O trade-runner valida a spec com a política dele e executa as ordens.
    Notifica no Telegram se TELEGRAM_CHAT_ID e TELEGRAM_BOT_TOKEN existirem.

    Exemplo (com `uv run main.py serve paper` rodando em outro terminal):
        uv run main.py connect docs/examples/spec-random.json --seed 1
    """
    strategy = _load_strategy(spec_file, seed)
    recorder = TickRecorder(record_ticks) if record_ticks else None
    bot = strategy_bot(
        strategy,
        trader,
        notifier_from_env(),
        on_tick=recorder.record if recorder else None,
    )
    with recorder or nullcontext():
        try:
            bot.run()
        except KeyboardInterrupt:
            bot.stop()


def stop(
    spec_file: Path | None = typer.Argument(None, help=SPEC_HELP),
    all_specs: bool = typer.Option(
        False, "--all", help="Para todas as specs abertas no trade-runner"
    ),
    trader: Path | None = typer.Option(
        None, help="Arquivo de conexão (padrão: o único .data/trader-*.json)"
    ),
):
    """
    Para uma spec (ou todas, --all): vende a posição agora e não compra mais.

    A parada fica no ledger: um `serve` e um `connect` reiniciados não voltam a
    comprar; o `connect` da spec, se roda, para sozinho. Para operar a
    estratégia de novo, mude a spec (outro id, outro bucket).

    Exemplo:
        uv run main.py stop docs/examples/spec-random.json
    """
    if (spec_file is None) == (not all_specs):
        raise typer.BadParameter("informe uma spec ou --all (exatamente um)")
    terms = None if spec_file is None else _load_strategy(spec_file, None).spec.terms()
    try:
        stopped = asyncio.run(stop_specs(find_connection(trader), terms))
    except (OSError, ValueError, TradeServiceError) as ex:
        typer.echo(f"erro: {ex}", err=True)
        raise typer.Exit(1) from None
    for item in stopped:
        typer.echo(stop_line(item))
    if not stopped or any(i["outcome"] not in ("sold", "flat") for i in stopped):
        raise typer.Exit(1)


def stop_line(item: dict) -> str:
    """Uma linha por bucket parado, para o terminal."""
    what = {
        "sold": "posição vendida",
        "flat": "sem posição",
        "no_price": "sem preço agora: a varredura vende em 30 s",
    }.get(item["outcome"])
    if what is None:
        reasons = "; ".join(item.get("reasons", ())) or "?"
        what = f"venda {item['outcome']} ({reasons}): a varredura tenta de novo"
    return f"Parada {item['spec']} ({item['bucket']}): {what}"


def _load_strategy(spec_file: Path, seed: str | None) -> SpecStrategy:
    try:
        strategy = SpecStrategy.from_file(str(spec_file))
    except (OSError, ValueError) as ex:
        raise typer.BadParameter(f"spec {spec_file}: {ex}") from ex
    if seed is not None:
        strategy.seed(seed)
    return strategy


def limits_from_policy(policy: Policy) -> SpecLimits:
    return SpecLimits(
        max_trade_usd=policy.max_trade_usd,
        allowed_symbols=policy.allowed_symbols,
        perps_enabled=policy.perps_enabled,
        max_leverage=policy.max_leverage,
        allowed_perp_markets=policy.allowed_perp_markets,
    )


@contextmanager
def mode_lock(mode: RunningMode) -> Iterator[None]:
    """Um processo de execução por modo; outro rodando é erro de uso, não bug."""
    try:
        lock = ModeLock(mode)
        lock.acquire()
    except ModeBusyError as ex:
        typer.echo(f"erro: {ex}", err=True)
        raise typer.Exit(1) from None
    try:
        yield
    finally:
        lock.release()
