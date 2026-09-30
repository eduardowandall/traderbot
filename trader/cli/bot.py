"""Os dois comandos: `run` (o bot ao vivo) e `backtest` (a spec sobre o passado).

Os comandos só leem argumentos; a montagem por modo fica em `trader/wiring.py`.
"""

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer

from trader.backtest import TickRecorder, load_ticks
from trader.backtest.spec import DEFAULT_BACKTEST_CANDLES, backtest_spec, fetch_ticks
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.cli.output import emit, json_errors
from trader.market import JupiterMarketData, MarketData
from trader.models import SOLANA_MINTS
from trader.models.mode import RunningMode
from trader.notification import notifier_from_env
from trader.policy import Policy, load_policy
from trader.strategy_spec.strategy import SpecStrategy
from trader.strategy_spec.validate import SpecLimits, parse_spec, validate
from trader.trading_service.local import LocalTradeClient
from trader.wiring import build_trade_service

SPEC_HELP = "Arquivo JSON da spec (o par vem dela), ex: docs/examples/spec-random.json"

# fonte de preços e candles (dados públicos, sem chave); os testes trocam
MARKET_DATA: Callable[[], MarketData] = JupiterMarketData


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
    spec = strategy.spec
    token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    notifier = notifier_from_env()
    _check_limits(strategy, mode)

    # execução (modo, chave, ledger) de um lado; o bot só vê o bucket dele
    service = build_trade_service(mode, on_wallet_created=_warn)
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
        market=MARKET_DATA(),
        trader=trader,
        notifier=notifier,
        on_tick=recorder.record if recorder else None,
    )
    bot = AsyncWebsocketTradingBot(config)
    with service.gateway, recorder or nullcontext():
        try:
            bot.run()
        except KeyboardInterrupt:
            bot.stop()


@json_errors
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
    fee_bps: str = typer.Option("30", help="Custo por swap em bps"),
    slippage_bps: str = typer.Option("10", help="Desvio de preço por perna, em bps"),
    seed: str = typer.Option("0", help="Semente do `random_chance`"),
):
    """
    Reproduz a spec em candles recentes (ou ticks gravados); saída em JSON.

    Usa o orçamento e a perda máxima da spec, como o bucket ao vivo.

    Exemplos:
        uv run main.py backtest docs/examples/spec-sol-dip.json
        uv run main.py backtest docs/examples/spec-random.json --ticks ticks.csv
    """
    spec = parse_spec(spec_file.read_text(encoding="utf-8"))
    tick_list = (
        load_ticks(ticks) if ticks is not None else asyncio.run(_candles(spec, candles))
    )
    emit(
        asyncio.run(
            backtest_spec(
                spec,
                tick_list,
                fee_bps=_decimal(fee_bps, "fee-bps"),
                seed=seed,
                slippage_bps=_decimal(slippage_bps, "slippage-bps"),
            )
        )
    )


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
