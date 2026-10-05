"""`backtest`: a spec sobre candles recentes ou ticks gravados.

Para operar, `serve <modo>` + `connect spec.json` (`runners.py`, B14).
"""

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import typer

from trader.api.cli.output import backtest_summary, emit, fail
from trader.backtest import load_ticks
from trader.backtest.costs import MeasuredCosts, resolve_costs
from trader.backtest.spec import (
    DEFAULT_BACKTEST_CANDLES,
    backtest_spec,
    fetch_ticks,
)
from trader.execution.market import JupiterMarketData
from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.market.jupiter.candles import MAX_CANDLES
from trader.shared.market import MarketData
from trader.strategy.spec.parse import parse_spec

SPEC_HELP = "Arquivo JSON da spec (o par vem dela), ex: docs/examples/spec-random.json"

# fonte de preços e candles (dados públicos, sem chave); os testes trocam
MARKET_DATA: Callable[[], MarketData] = JupiterMarketData
# quotes e preços para medir os custos do backtest (B9); os testes trocam
COST_QUOTES: Callable[[], Any] = AsyncJupiterClient


def backtest(
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    candles: int = typer.Option(
        DEFAULT_BACKTEST_CANDLES,
        min=2,
        max=MAX_CANDLES,
        help="Candles do timeframe da spec (sem --ticks)",
    ),
    ticks: Path | None = typer.Option(
        None, help="CSV de ticks gravado com `connect --record-ticks`"
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


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        parsed = Decimal("NaN")
    if not parsed.is_finite():
        raise ValueError(f"{name} inválido: {value!r}")
    return parsed
