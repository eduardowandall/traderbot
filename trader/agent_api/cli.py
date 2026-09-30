"""Comandos de agente (`main.py market ...` e `main.py strategy ...`).

Sempre imprimem um único objeto JSON (ver `output.py`). Só leem dados
públicos e arquivos de spec: não precisam de chave e não escrevem nada.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import typer

from trader.agent_api import market, strategies
from trader.agent_api.output import emit, fail
from trader.backtest import load_ticks
from trader.market import JupiterMarketData, MarketData
from trader.models import Interval
from trader.policy import load_policy
from trader.strategy_spec.validate import SpecLimits, SpecParseError, parse_spec

market_app = typer.Typer(help="Dados de mercado (JSON), só leitura.")
strategy_app = typer.Typer(
    help="Specs de estratégia (JSON): schema, validar, backtest."
)

# fonte de dados de mercado; os testes trocam por uma falsa
MARKET_DATA: Callable[[], MarketData] = JupiterMarketData

MODE_HELP = "Seção da política usada nos limites (paper, dry ou real)"


def _errors_of(ex: Exception) -> list[dict]:
    """O erro como lista `[{"path", "msg"}]` para o JSON."""
    if isinstance(ex, SpecParseError):
        return [{"path": e.path, "msg": e.msg} for e in ex.errors]
    if isinstance(ex, ValueError | OSError):
        return [{"path": "", "msg": str(ex)}]
    # rede, API, qualquer outra coisa: o agente sempre recebe JSON
    return [{"path": "", "msg": f"{type(ex).__name__}: {ex}"}]


def json_errors(command: Callable[..., None]) -> Callable[..., None]:
    """Qualquer erro vira `{"ok": false, ...}` em vez de traceback."""

    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        try:
            command(*args, **kwargs)
        except typer.Exit:
            raise
        except Exception as ex:
            fail(_errors_of(ex))

    return wrapper


def _with_market(use: Callable[[MarketData], Awaitable[Any]]) -> Any:
    async def run():
        data = MARKET_DATA()
        try:
            return await use(data)
        finally:
            await data.aclose()

    return asyncio.run(run())


def _limits(mode: str) -> SpecLimits:
    return strategies.limits_from_policy(load_policy(mode=mode))


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        parsed = Decimal("NaN")
    if not parsed.is_finite():
        raise ValueError(f"{name} inválido: {value!r}")
    return parsed


# --- market ------------------------------------------------------------------


@market_app.command("symbols")
@json_errors
def market_symbols(mode: str = typer.Option("paper", help=MODE_HELP)):
    """Tokens do registro e quais podem ser operados."""
    emit({"symbols": market.symbols(_limits(mode))})


@market_app.command("price")
@json_errors
def market_price(symbol: str = typer.Argument(..., help="Token, ex: SOL")):
    """Preço atual em USD (websocket da Jupiter)."""
    emit(_with_market(lambda data: market.price(data, symbol)))


@market_app.command("candles")
@json_errors
def market_candles(
    symbol: str = typer.Argument(..., help="Token, ex: SOL"),
    interval: Interval = typer.Option(Interval.MINUTE_1),
    n: int = typer.Option(100, min=1, max=1000, help="Quantidade de candles"),
):
    """Candles em USD (mais antigo primeiro)."""
    rows = _with_market(lambda data: market.candles(data, symbol, interval, n))
    emit({"symbol": symbol, "interval": str(interval), "candles": rows})


@market_app.command("summary")
@json_errors
def market_summary(
    symbol: str = typer.Argument(..., help="Token, ex: SOL"),
    interval: Interval = typer.Option(Interval.MINUTE_1),
    n: int = typer.Option(200, min=2, max=1000, help="Barras analisadas"),
):
    """Variação, volatilidade, RSI e médias (mesmos cálculos das specs)."""
    emit(_with_market(lambda data: market.summary(data, symbol, interval, n)))


# --- strategy ----------------------------------------------------------------


@strategy_app.command("schema")
@json_errors
def strategy_schema():
    """JSON Schema da spec de estratégia."""
    emit({"schema": strategies.schema()})


@strategy_app.command("validate")
@json_errors
def strategy_validate(
    file: Path = typer.Argument(..., help="Arquivo JSON da spec"),
    mode: str = typer.Option("paper", help=MODE_HELP),
):
    """Confere formato e limites; não grava nada."""
    report = strategies.check(file.read_text(encoding="utf-8"), _limits(mode))
    if not report["valid"]:
        fail(report.pop("errors"), **report)
    emit(report)


@strategy_app.command("backtest")
@json_errors
def strategy_backtest(
    file: Path = typer.Argument(..., help="Arquivo JSON da spec"),
    candles: int = typer.Option(
        strategies.DEFAULT_BACKTEST_CANDLES,
        min=2,
        max=1000,
        help="Candles do timeframe da spec (sem --ticks)",
    ),
    ticks: Path | None = typer.Option(None, help="CSV de ticks (run --record-ticks)"),
    fee_bps: str = typer.Option("30", help="Custo por swap em bps"),
    slippage_bps: str = typer.Option("10", help="Desvio de preço por perna, em bps"),
    seed: str = typer.Option("0", help="Semente"),
):
    """Reproduz a spec em candles recentes (ou ticks gravados)."""
    spec = parse_spec(file.read_text(encoding="utf-8"))
    tick_list = (
        load_ticks(ticks)
        if ticks is not None
        else _with_market(lambda data: strategies.fetch_ticks(data, spec, candles))
    )
    result = asyncio.run(
        strategies.backtest(
            spec,
            tick_list,
            _decimal(fee_bps, "fee-bps"),
            seed,
            _decimal(slippage_bps, "slippage-bps"),
        )
    )
    emit(result)
