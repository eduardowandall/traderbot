"""`run` (o bot ao vivo, num processo só) e `backtest` (a spec sobre o passado).

Os comandos só leem argumentos; a montagem por modo fica em `trader/wiring.py`.
`serve`/`connect` (várias specs, a chave num processo) estão em `runners.py`.
"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import typer

from trader.backtest import TickRecorder, load_ticks
from trader.backtest.costs import MeasuredCosts, resolve_costs
from trader.backtest.spec import (
    DEFAULT_BACKTEST_CANDLES,
    backtest_spec,
    fetch_ticks,
)
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.cli.output import backtest_summary, emit, fail
from trader.market import JupiterMarketData, MarketData
from trader.market.hub import HubMarketData, PriceHub
from trader.market.pair import market_for
from trader.market.prices import price_fn
from trader.models import SOLANA_MINTS
from trader.models.mode import RunningMode
from trader.notification import notifier_from_env
from trader.notification.daily_report import DailyReporter
from trader.policy import Policy, load_policy
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.runners.lock import ModeBusyError, ModeLock
from trader.strategy_spec.strategy import SpecStrategy
from trader.strategy_spec.validate import SpecLimits, parse_spec, validate
from trader.trading_service.local import LocalTradeClient
from trader.wiring import build_trade_service

SPEC_HELP = "Arquivo JSON da spec (o par vem dela), ex: docs/examples/spec-random.json"

# fonte de preços e candles (dados públicos, sem chave); os testes trocam
MARKET_DATA: Callable[[], MarketData] = JupiterMarketData
# quotes e preços para medir os custos do backtest (B9); os testes trocam
COST_QUOTES: Callable[[], Any] = AsyncJupiterClient


def run(
    mode: RunningMode = typer.Argument(..., help="real ou paper"),
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    seed: str | None = typer.Option(None, help="Semente do `random_chance`"),
    record_ticks: Path | None = typer.Option(
        None, help="Grava cada preço recebido neste CSV (para `backtest --ticks`)"
    ),
):
    """
    Executa o bot com uma spec, no bucket dela (`strategy:<spec_id>`).

    Notifica no Telegram se TELEGRAM_CHAT_ID e TELEGRAM_BOT_TOKEN existirem.

    Exemplo:
        uv run main.py run paper docs/examples/spec-random.json --seed 1
    """
    # estratégia primeiro: um erro nela não pode deixar ledger/conexões abertos
    strategy = _load_strategy(spec_file, seed)
    notifier = notifier_from_env()
    _check_limits(strategy, mode)
    with mode_lock(mode):
        _run_bot(mode, strategy, notifier, record_ticks)


def _run_bot(mode, strategy: SpecStrategy, notifier, record_ticks: Path | None):
    spec = strategy.spec
    token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    # execução (modo, chave, ledger) de um lado; o bot só vê o bucket dele
    # preços do hub (websocket + Price API, nunca velhos), para o bot e o serviço
    hub = PriceHub()
    service = build_trade_service(mode, on_wallet_created=_warn, prices=hub)
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
    reporter = DailyReporter(service.gateway, str(mode), notifier, price_fn(hub))
    # candles diretos (só no aquecimento)
    market = market_for(spec.symbol, lambda: HubMarketData(hub.get, MARKET_DATA()))
    config = BotConfig(
        name=f"{mode}-run-{spec.name}",
        symbol=spec.symbol,
        strategy=strategy,
        market=market,
        trader=trader,
        notifier=notifier,
        on_tick=recorder.record if recorder else None,
        background=(reporter.run_forever, hub.run),
    )
    bot = AsyncWebsocketTradingBot(config)
    with service.gateway, recorder or nullcontext():
        try:
            bot.run()
        except KeyboardInterrupt:
            bot.stop()


def backtest(
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    candles: int = typer.Option(
        DEFAULT_BACKTEST_CANDLES,
        min=2,
        max=1000,
        help="Candles do timeframe da spec (sem --ticks)",
    ),
    ticks: Path | None = typer.Option(
        None, help="CSV de ticks gravado com `run --record-ticks`"
    ),
    fee_bps: str | None = typer.Option(
        None,
        help="Custo por swap em bps (padrão: medido agora na Jupiter, metade da "
        "ida e volta no tamanho de trade da spec)",
    ),
    slippage_bps: str = typer.Option("10", help="Desvio de preço por perna, em bps"),
    network_fee_usd: str | None = typer.Option(
        None,
        help="Taxa de rede por perna, em USD (padrão: 5000 lamports + "
        "max_priority_fee_lamports da política real, ao preço do SOL agora)",
    ),
    seed: str = typer.Option("0", help="Semente do `random_chance`"),
    as_json: bool = typer.Option(
        False, "--json", help="Um objeto JSON no stdout, com todos os trades"
    ),
):
    """
    Reproduz a spec em candles recentes (ou ticks gravados) e resume o resultado.

    Usa o orçamento e a perda máxima da spec, como o bucket ao vivo.

    Exemplos:
        uv run main.py backtest docs/examples/spec-sol-dip.json
        uv run main.py backtest docs/examples/spec-random.json --ticks ticks.csv
        uv run main.py backtest docs/examples/spec-sol-dip.json --json
    """
    costs = {
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "network_fee_usd": network_fee_usd,
    }
    try:
        result = _backtest(spec_file, candles, ticks, costs, seed)
    except Exception as ex:
        fail(ex, as_json)
        return
    if as_json:
        emit(result)
    else:
        typer.echo(backtest_summary(result))


def _backtest(
    spec_file: Path,
    candles: int,
    ticks: Path | None,
    costs: dict[str, str | None],
    seed: str,
) -> dict:
    """`costs`: fee_bps, slippage_bps e network_fee_usd, como digitados.

    Sem `fee_bps` ou `network_fee_usd`, os dois são medidos agora (B9);
    o resultado traz a medição em `measured_costs`.
    """
    spec = parse_spec(spec_file.read_text(encoding="utf-8"))
    tick_list = (
        load_ticks(ticks) if ticks is not None else asyncio.run(_candles(spec, candles))
    )
    fee, slippage, network = (
        None if value is None else _decimal(value, name.replace("_", "-"))
        for name, value in costs.items()
    )
    assert slippage is not None
    fee, network, measured = asyncio.run(_costs(spec, fee, network))
    result = asyncio.run(
        backtest_spec(
            spec,
            tick_list,
            fee_bps=fee,
            seed=seed,
            slippage_bps=slippage,
            network_fee_usd=network,
        )
    )
    return {**result, "measured_costs": measured and asdict(measured)}


async def _costs(
    spec, fee: Decimal | None, network: Decimal | None
) -> tuple[Decimal, Decimal, MeasuredCosts | None]:
    try:
        return await resolve_costs(COST_QUOTES, spec, fee, network)
    except Exception as ex:
        raise ValueError(
            f"medir os custos na Jupiter falhou ({type(ex).__name__}: {ex}); "
            "passe --fee-bps e --network-fee-usd para rodar sem medir"
        ) from ex


async def _candles(spec, n: int):
    # só leitura de dados públicos: sem chave, sem RPC
    data = MARKET_DATA()
    try:
        return await fetch_ticks(data, spec, n)
    finally:
        await data.aclose()


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
        max_trade_usd=policy.max_trade_usd, allowed_symbols=policy.allowed_symbols
    )


def _check_limits(strategy: SpecStrategy, mode: RunningMode) -> None:
    """A spec precisa caber na política do modo antes de rodar."""
    errors = validate(strategy.spec, limits_from_policy(load_policy(mode=str(mode))))
    if errors:
        details = "; ".join(f"{e.path}: {e.msg}" for e in errors)
        raise typer.BadParameter(f"spec inválida para {mode}: {details}")


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        parsed = Decimal("NaN")
    if not parsed.is_finite():
        raise ValueError(f"{name} inválido: {value!r}")
    return parsed


def _warn(message: str) -> None:
    typer.echo(message, err=True)


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
