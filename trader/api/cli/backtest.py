"""`backtest`: a spec sobre candles recentes ou ticks gravados.

Para operar, `serve <modo>` + `connect spec.json` (`runners.py`, B14).
"""

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import cached_property
from pathlib import Path
from typing import Any

import typer

from trader.api.cli.output import (
    backtest_summary,
    emit,
    fail,
    matrix_summary,
    scenarios_summary,
)
from trader.backtest import load_ticks
from trader.backtest.costs import MeasuredCosts, resolve_costs
from trader.backtest.scenarios import (
    Scenario,
    load_catalog,
    rolling,
    run_matrix,
    select,
    single_view,
)
from trader.backtest.spec import (
    DEFAULT_BACKTEST_CANDLES,
    ReplayCosts,
    backtest_spec,
    fetch_ticks,
)
from trader.backtest.stress import Stress, parse_duration, stresses
from trader.backtest.ticks import Tick
from trader.backtest.variants import Variant, parse_vary, variants
from trader.backtest.window import MAX_HOLE, VolumeScales, fetch_window, volume_scales
from trader.execution.market import JupiterCandles
from trader.execution.market.jupiter.candles import MAX_CANDLES
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.market import CandleHistory
from trader.shared.models import TickerData

SPEC_HELP = "Arquivo JSON da spec (o par vem dela), ex: docs/examples/spec-random.json"


# fonte dos candles (dados públicos, sem chave; o passado em cache em
# `.data/candles/`); os testes trocam
CANDLES: Callable[[], CandleHistory] = JupiterCandles
# quotes e preços para medir os custos do backtest (B9); os testes trocam
COST_QUOTES: Callable[[], Any] = AsyncJupiterClient


def backtest(
    spec_file: Path = typer.Argument(..., help=SPEC_HELP),
    candles: int | None = typer.Option(
        None,
        min=2,
        max=MAX_CANDLES,
        help=f"Os últimos N candles do timeframe da spec (padrão: "
        f"{DEFAULT_BACKTEST_CANDLES})",
    ),
    ticks: Path | None = typer.Option(
        None, help="CSV de ticks gravado com `connect --record-ticks`"
    ),
    start: str | None = typer.Option(
        None,
        "--from",
        help="Início de uma janela do passado, ISO (sem fuso: UTC), ex: "
        "2025-10-10T18:00; o aquecimento vem de antes dela",
    ),
    end: str | None = typer.Option(
        None, "--to", help="Fim da janela (com --from; padrão: agora)"
    ),
    scenario: list[str] | None = typer.Option(
        None,
        "--scenario",
        help="Janelas do catálogo: um id, tag:NOME ou all (repetível)",
    ),
    catalog: Path | None = typer.Option(
        None, help="Catálogo de cenários (padrão: docs/scenarios.json)"
    ),
    rolling_: str | None = typer.Option(
        None,
        "--rolling",
        help="Janelas iguais de --from a --to, ex: 7d, 12h, 30m",
    ),
    step: str | None = typer.Option(
        None, help="Com --rolling: o passo entre janelas (padrão: o tamanho)"
    ),
    max_hole: str | None = typer.Option(
        None,
        help="Janelas: o maior trecho sem candles aceito (padrão: 1h; num token "
        "fino, horas sem negócio são reais)",
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
    borrow_bps_hour: str | None = typer.Option(
        None,
        help="Perps: empréstimo por hora em bps do tamanho (padrão: 1, o do paper)",
    ),
    vary: list[str] | None = typer.Option(
        None,
        "--vary",
        help="Variantes (com janelas): CAMINHO=V1,V2 de um campo da spec, ex: "
        "exit.stop.pct=3,4,5 (repetível: o produto)",
    ),
    write_best: Path | None = typer.Option(
        None, help="Com --vary: grava o JSON da melhor variante nesse arquivo"
    ),
    seed: str = typer.Option(
        "0", help="Semente do `random_chance` (várias, com janelas: 0,1,2)"
    ),
    jobs: int | None = typer.Option(
        None,
        min=1,
        help="Janelas: processos para as células (padrão: os núcleos da máquina; "
        "1: no processo)",
    ),
    costs_x: str | None = typer.Option(
        None,
        "--costs",
        help="Janelas: estresses de custo, ex: 2x,3x (taxa, slippage, rede e "
        "empréstimo multiplicados); a base sempre roda",
    ),
    shock: list[str] | None = typer.Option(
        None,
        "--shock",
        help="Janelas: um choque no preço, ex: -15%@50% (degrau) ou -10%~1h@50% "
        "(cai e volta em 1 h); repetível",
    ),
    volume_slippage: bool = typer.Option(
        False,
        "--volume-slippage",
        help="Janelas: slippage x (volume mediano / volume da barra), de 1x a 10x",
    ),
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
        uv run main.py backtest docs/examples/spec-sol-dip.json \
            --from 2025-10-10T12:00 --to 2025-10-11T12:00
        uv run main.py backtest docs/examples/spec-sol-dip.json \
            --scenario tag:black_swan --scenario quiet-2026-04-22
        uv run main.py backtest docs/examples/spec-sol-dip.json \
            --rolling 1d --from 2026-09-01 --to 2026-10-01
        uv run main.py backtest docs/examples/spec-sol-dip.json --scenario all \
            --vary exit.stop.pct=3,5 --vary exit.conditions.0.pct=2,4
    """
    costs = {
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "network_fee_usd": network_fee_usd,
        "borrow_bps_hour": borrow_bps_hour,
    }
    args = _Args(
        candles,
        ticks,
        start,
        end,
        scenario or [],
        catalog,
        rolling_,
        step,
        max_hole,
        vary or [],
        write_best,
        seed,
        jobs,
        costs_x,
        shock or [],
        volume_slippage,
    )
    try:
        source = _source(args)
        result = _backtest(spec_file, source, costs, args)
    except Exception as ex:
        fail(ex, as_json)
        return
    if as_json:
        emit(result)
    elif "ranking" in result:
        typer.echo(matrix_summary(result))
    elif isinstance(source, _Scenarios):
        typer.echo(scenarios_summary(result))
    else:
        typer.echo(backtest_summary(result))


@dataclass(frozen=True)
class _Args:
    """As opções que dizem de onde vêm os ticks, como digitadas."""

    candles: int | None
    ticks: Path | None
    start: str | None
    end: str | None
    scenario: list[str]
    catalog: Path | None
    rolling: str | None
    step: str | None
    max_hole: str | None
    vary: list[str]
    write_best: Path | None
    seed: str
    jobs: int | None
    costs: str | None
    shock: list[str]
    volume_slippage: bool

    @cached_property
    def seeds(self) -> list[str]:
        """As sementes, sem vazias nem repetidas (cada uma é um replay a mais)."""
        found = (s.strip() for s in self.seed.split(","))
        return list(dict.fromkeys(s for s in found if s)) or ["0"]

    @property
    def matrix(self) -> bool:
        """Variantes ou várias sementes em cada janela (A36)."""
        return bool(self.vary) or len(self.seeds) > 1 or len(self.stresses) > 1

    @cached_property
    def stresses(self) -> list[Stress]:
        """A base e os estresses de `--costs` e `--shock` (A38)."""
        return stresses(self.costs, self.shock)

    @property
    def history(self) -> bool:
        """Janelas do passado: `--from`, `--scenario` ou `--rolling`."""
        return bool(self.scenario) or self.start is not None or self.rolling is not None

    def refuse_mixes(self) -> None:
        recent = self.candles is not None or self.ticks is not None
        problems = [
            (
                self.candles is not None and self.ticks is not None,
                "use --candles ou --ticks, não os dois",
            ),
            (self.history and recent, "janelas do passado: sem --candles nem --ticks"),
            (
                bool(self.scenario)
                and (self.start or self.end or self.rolling) is not None,
                "--scenario usa as janelas do catálogo: sem --from/--to/--rolling",
            ),
            (
                not self.scenario
                and self.start is None
                and (self.end or self.rolling) is not None,
                "--to e --rolling precisam de --from",
            ),
            (
                self.step is not None and self.rolling is None,
                "--step precisa de --rolling",
            ),
            (
                self.max_hole is not None and not self.history,
                "--max-hole é das janelas: com --from, --scenario ou --rolling",
            ),
            (
                self.matrix and not self.history,
                "--vary, várias sementes e estresses são por janela: com --from, --scenario "
                "ou --rolling",
            ),
            (
                self.write_best is not None and not self.vary,
                "--write-best precisa de --vary",
            ),
            (
                self.jobs is not None and not self.history,
                "--jobs é das janelas: com --from, --scenario ou --rolling",
            ),
            (
                self.volume_slippage and not self.history,
                "--volume-slippage é das janelas: com --from, --scenario ou --rolling",
            ),
        ]
        for bad, msg in problems:
            if bad:
                raise ValueError(msg)


@dataclass(frozen=True)
class _Window:
    """`--from/--to` (A34)."""

    start: datetime
    end: datetime
    max_hole: timedelta


@dataclass(frozen=True)
class _Scenarios:
    """`--scenario` ou `--rolling` (A35)."""

    windows: list[Scenario]
    max_hole: timedelta


# de onde vêm os ticks: os últimos N candles, um arquivo, uma janela ou várias
_Source = int | Path | _Window | _Scenarios


def _source(args: _Args) -> _Source:
    args.refuse_mixes()
    if not args.history:
        return args.ticks or args.candles or DEFAULT_BACKTEST_CANDLES
    hole = MAX_HOLE if args.max_hole is None else parse_duration(args.max_hole)
    if args.scenario:
        return _Scenarios(select(load_catalog(args.catalog), args.scenario), hole)
    assert args.start is not None
    start = _when(args.start, "--from")
    end = datetime.now(UTC) if args.end is None else _when(args.end, "--to")
    if args.rolling is None and args.matrix:
        return _Scenarios([Scenario("window", start, end)], hole)
    if args.rolling is None:
        return _Window(start, end, hole)
    return _Scenarios(_rolling(start, end, args.rolling, args.step), hole)


def _rolling(
    start: datetime, end: datetime, length: str, step: str | None
) -> list[Scenario]:
    every = None if step is None else parse_duration(step)
    return rolling(start, end, parse_duration(length), every)


def _when(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{name} inválido: {value!r} (use ISO, ex: 2025-10-10T18:00)"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _backtest(
    spec_file: Path, source: _Source, costs: dict[str, str | None], args: _Args
) -> dict:
    """`costs`: fee_bps, slippage_bps, network_fee_usd e borrow_bps_hour, como
    digitados.

    Sem `fee_bps` ou `network_fee_usd`, os dois são medidos agora (B9), uma
    vez para todas as janelas; o resultado traz a medição em
    `measured_costs`. Numa perp (A9) a taxa e o slippage do par não se
    aplicam (o motor de perps cobra as dele).
    """
    plan = variants(
        json.loads(spec_file.read_text(encoding="utf-8")), parse_vary(args.vary)
    )
    spec = plan[0].spec
    _refuse_perp_stress(spec, args)
    resolved, measured = _resolved_costs(spec, costs)
    if isinstance(source, _Scenarios):
        run = _read(
            run_matrix,
            plan,
            source.windows,
            resolved,
            args.seeds,
            max_hole=source.max_hole,
            trades=not args.matrix,
            jobs=args.jobs or os.process_cpu_count() or 1,
            stresses=args.stresses,
            volume_slippage=args.volume_slippage,
        )
        matrix = asyncio.run(run)
        if not args.matrix:
            return {**single_view(matrix), "measured_costs": measured}
        _write_best(args.write_best, plan, matrix["best"])
        return {**matrix, "measured_costs": measured}
    ticks, warmup, extra, until, scale = _data(spec, source, args.volume_slippage)
    result = asyncio.run(
        backtest_spec(
            spec,
            ticks,
            seed=args.seeds[0],
            warmup=warmup,
            until=until,
            slippage_scale=scale,
            **resolved,
        )
    )
    return {**result, **extra, "measured_costs": measured}


def _write_best(path: Path | None, plan: list[Variant], best: str) -> None:
    if path is None:
        return
    [variant] = [v for v in plan if v.id == best]
    path.write_text(json.dumps(variant.data, indent=2) + "\n", encoding="utf-8")


def _resolved_costs(spec, typed: dict[str, str | None]) -> tuple[dict, dict | None]:
    """Os custos de `backtest_spec` (dos digitados ou medidos agora) e a
    medição, se houve."""
    value = {
        name: None if v is None else _decimal(v, name.replace("_", "-"))
        for name, v in typed.items()
    }
    fee, network, measured = asyncio.run(
        _costs(spec, value["fee_bps"], value["network_fee_usd"])
    )
    borrow = value["borrow_bps_hour"]
    resolved = {
        "fee_bps": fee,
        "slippage_bps": value["slippage_bps"],
        "network_fee_usd": network,
        "borrow_bps_hour": ReplayCosts.borrow_bps_hour if borrow is None else borrow,
    }
    return resolved, measured and asdict(measured)


def _refuse_perp_stress(spec, args: _Args) -> None:
    """Numa perp a taxa e o slippage do par não se aplicam (o motor de perps
    cobra as dele): `--costs` e `--volume-slippage` não testariam nada."""
    if spec.market is not None and (args.costs or args.volume_slippage):
        raise ValueError(
            "--costs e --volume-slippage mudam a taxa e o slippage do par, que "
            "uma perp não usa (o motor de perps cobra os dele): use --shock"
        )


def _data(
    spec, source: int | Path | _Window, volume_slippage: bool = False
) -> tuple[list[Tick], list[TickerData], dict, datetime | None, VolumeScales | None]:
    """Os ticks, os candles de aquecimento, o que o resultado ganha, até
    quando a spec vale (o fim de uma janela) e o slippage pelo volume (A38)."""
    if isinstance(source, Path):
        return load_ticks(source), [], {}, None, None
    if isinstance(source, int):
        return asyncio.run(_read(fetch_ticks, spec, source)), [], {}, None, None
    window = asyncio.run(
        _read(
            fetch_window,
            spec,
            source.start,
            source.end,
            max_hole=source.max_hole,
        )
    )
    extra = {
        "window": {"from": window.start, "to": window.end},
        "coverage": window.coverage.as_dict(),
    }
    scale = volume_scales(window.candles, spec.timeframe) if volume_slippage else None
    return window.ticks, window.warmup, extra, window.end, scale


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


async def _read(fetch, *args, **kwargs):
    # só leitura de dados públicos: sem chave, sem RPC
    async with aclosing(CANDLES()) as data:
        return await fetch(data, *args, **kwargs)


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        parsed = Decimal("NaN")
    if not parsed.is_finite():
        raise ValueError(f"{name} inválido: {value!r}")
    return parsed
