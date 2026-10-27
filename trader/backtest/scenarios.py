"""Cenários do backtest (A35): uma spec em várias janelas do passado.

Um catálogo (`docs/scenarios.json`) nomeia janelas em UTC com rótulos
(`bullish`, `bearish`, `choppy`, `quiet`, `black_swan`, ...); `rolling` corta
um período em janelas iguais. Cada janela é um bucket novo com o orçamento e
a perda máxima da spec, valendo até o fim da janela (`backtest_spec(until=)`).

Um rótulo fala de um ativo (o catálogo foi escolhido no SOL): cada janela é
medida no par da própria spec, e um rótulo que o par desmente vira um aviso.
"""

import asyncio
import json
import math
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import aclosing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.backtest.spec import backtest_spec
from trader.backtest.stress import BASE, Stress
from trader.backtest.ticks import Tick
from trader.backtest.variants import Variant
from trader.backtest.window import (
    MAX_HOLE,
    VolumeScales,
    Window,
    fetch_window,
    volume_scales,
)
from trader.execution.market import JupiterCandles
from trader.execution.market.history import CandleCache, chunks
from trader.shared.indicators import to_utc, volatility
from trader.shared.market import CandleRange
from trader.shared.models import TickerData
from trader.shared.models.public_data import Interval
from trader.shared.paths import PROJECT_ROOT
from trader.strategy.spec.models import StrategySpec

CATALOG = PROJECT_ROOT / "docs" / "scenarios.json"
DAY_SECONDS = 86_400


@dataclass(frozen=True)
class Scenario:
    id: str
    start: datetime  # UTC
    end: datetime  # UTC, exclusivo
    tags: tuple[str, ...] = ()
    note: str = ""


def load_catalog(path: Path | None = None) -> list[Scenario]:
    data = json.loads((path or CATALOG).read_text(encoding="utf-8"))
    scenarios = [_scenario(item) for item in data["scenarios"]]
    repeated = sorted(i for i, n in Counter(s.id for s in scenarios).items() if n > 1)
    if repeated:
        raise ValueError(f"cenários repetidos no catálogo: {', '.join(repeated)}")
    return scenarios


def _scenario(item: dict) -> Scenario:
    start, end = _aware(item["start"]), _aware(item["end"])
    if end <= start:
        raise ValueError(f"cenário {item['id']}: o fim não vem depois do início")
    return Scenario(
        item["id"], start, end, tuple(item.get("tags", ())), item.get("note", "")
    )


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r}: um horário do catálogo precisa de fuso")
    return to_utc(parsed)


def select(catalog: list[Scenario], selectors: Sequence[str]) -> list[Scenario]:
    """`all`, `tag:NOME` ou um id; na ordem do catálogo, sem repetir."""
    chosen = {s.id for selector in selectors for s in _matching(catalog, selector)}
    return [s for s in catalog if s.id in chosen]


def _matching(catalog: list[Scenario], selector: str) -> list[Scenario]:
    if selector == "all":
        return catalog
    if selector.startswith("tag:"):
        found = [s for s in catalog if selector[4:] in s.tags]
    else:
        found = [s for s in catalog if s.id == selector]
    if not found:
        raise ValueError(
            f"nenhum cenário para {selector!r} (use um id, tag:NOME ou all)"
        )
    return found


def rolling(
    start: datetime, end: datetime, length: timedelta, step: timedelta | None = None
) -> list[Scenario]:
    """Janelas de `length` a cada `step` (padrão: `length`) dentro de
    `[start, end)`."""
    step = step or length
    found: list[Scenario] = []
    at = start
    while at + length <= end:
        found.append(
            Scenario(f"rolling-{at:%Y%m%dT%H%M}", at, at + length, ("rolling",))
        )
        at += step
    if not found:
        raise ValueError(f"nenhuma janela de {length} cabe em {start} .. {end}")
    return found


@dataclass(frozen=True)
class WindowStats:
    """A janela medida no par da spec (no token de cotação)."""

    return_pct: float  # abertura -> último fechamento
    max_drawdown_pct: float  # do maior preço até uma mínima depois dele
    range_pct: float  # máxima / mínima - 1
    daily_vol_pct: float  # `volatility` dos fechamentos, em um dia

    def describe(self) -> str:
        return (
            f"par {self.return_pct:+.1f}%, queda máx {self.max_drawdown_pct:.1f}%, "
            f"faixa {self.range_pct:.1f}%, vol/dia {self.daily_vol_pct:.1f}%"
        )


def window_stats(candles: list[TickerData], interval: Interval) -> WindowStats:
    if not candles:
        raise ValueError("janela sem candles")
    opened = float(candles[0].open or candles[0].last)
    high = max(float(c.high or c.last) for c in candles)
    low = min(float(c.low or c.last) for c in candles)
    return WindowStats(
        return_pct=(float(candles[-1].last) / opened - 1) * 100,
        max_drawdown_pct=_drawdown(candles, opened),
        range_pct=(high / low - 1) * 100,
        daily_vol_pct=_daily_vol([c.last for c in candles], interval),
    )


def _drawdown(candles: list[TickerData], peak: float) -> float:
    worst = 0.0
    for c in candles:
        worst = max(worst, 1 - float(c.low or c.last) / peak)
        peak = max(peak, float(c.high or c.last))
    return worst * 100


def _daily_vol(closes: list[Decimal], interval: Interval) -> float:
    """A `volatility` que as specs leem, sobre a janela toda, levada a um dia."""
    per_bar = volatility(closes, len(closes) - 1)
    if per_bar is None:
        return 0.0
    return float(per_bar) * math.sqrt(DAY_SECONDS / interval.seconds)


def hold_return_pct(ticks: list[Tick]) -> float:
    """Comprar e segurar na janela, em USD como o patrimônio do backtest."""
    first, last = ticks[0], ticks[-1]
    usd = last.price * (last.quote_usd or 1) / (first.price * (first.quote_usd or 1))
    return (float(usd) - 1) * 100


# o que cada rótulo promete do par; os outros (short, long, holdout, ...) não
# se medem
TAG_CHECKS: dict[str, Callable[[WindowStats], bool]] = {
    "bullish": lambda s: s.return_pct >= 5,
    "bearish": lambda s: s.return_pct <= -5,
    "black_swan": lambda s: s.max_drawdown_pct >= 10,
    "quiet": lambda s: s.daily_vol_pct <= 2.5,
    # a faixa engole o movimento: sobe e desce, sem sair do lugar
    "choppy": lambda s: s.range_pct >= max(10, 3 * abs(s.return_pct)),
}


def flags(tags: Sequence[str], stats: WindowStats) -> list[str]:
    """Um aviso por rótulo que o par desmente."""
    return [
        f"[!] {tag}: {stats.describe()}"
        for tag in tags
        if tag in TAG_CHECKS and not TAG_CHECKS[tag](stats)
    ]


# janelas com essa tag são a prova: aparecem à parte e nunca ranqueiam (A36)
HOLDOUT = "holdout"


async def run_scenarios(
    source: CandleRange,
    spec: StrategySpec,
    scenarios: Sequence[Scenario],
    costs: dict,
    seed: str = "0",
    now: datetime | None = None,
    max_hole: timedelta = MAX_HOLE,
) -> dict:
    """Uma spec em cada janela (A35), com os trades de cada uma."""
    matrix = await run_matrix(
        source, [Variant(spec, {})], scenarios, costs, (seed,), now, max_hole, True
    )
    return single_view(matrix)


def single_view(matrix: dict) -> dict:
    """A primeira variante de `run_matrix` como um run de uma spec (A35): as
    janelas e o resumo sobre todas, `holdout` incluído."""
    rows = matrix["variants"][0]["windows"]
    header = {k: matrix[k] for k in ("spec_id", "name", "symbol", "timeframe")}
    return {**header, "windows": rows, "summary": summarize(rows)}


@dataclass(frozen=True)
class _Run:
    """O que toda célula de um `run_matrix` usa (vai inteiro para um worker)."""

    widest: StrategySpec  # a variante de maior aquecimento: lê as janelas
    costs: dict
    seeds: tuple[str, ...]
    now: datetime
    max_hole: timedelta
    trades: bool
    stresses: tuple[Stress, ...] = (BASE,)  # A38: a base primeiro
    volume_slippage: bool = False  # A38

    async def window(self, source: CandleRange, scenario: Scenario) -> Window:
        return await fetch_window(
            source,
            self.widest,
            scenario.start,
            scenario.end,
            self.now,
            self.max_hole,
        )


async def run_matrix(
    source: CandleRange,
    variants: Sequence[Variant],
    scenarios: Sequence[Scenario],
    costs: dict,
    seeds: Sequence[str] = ("0",),
    now: datetime | None = None,
    max_hole: timedelta = MAX_HOLE,
    trades: bool = False,
    jobs: int = 1,
    stresses: Sequence[Stress] = (BASE,),
    volume_slippage: bool = False,
) -> dict:
    """Cada variante x semente em cada janela (A36); `costs` são os de
    `backtest_spec` (fee_bps, slippage_bps, network_fee_usd,
    borrow_bps_hour), os mesmos em todas.

    Cada janela é lida com o maior aquecimento das variantes (o excesso não
    muda nada; um buraco nele recusa a janela para todas) e medida uma vez.
    Uma janela que não roda (um buraco, poucas barras: `ValueError`) vira uma
    linha com o erro e as outras seguem; uma falha de rede encerra o run.
    Sem `trades`, cada célula guarda só os números (a lista de trades de
    variantes x janelas x sementes não cabe em memória). Com `jobs > 1` e
    uma fonte com cache em disco, as células rodam em processos (A37).
    """
    widest = max((v.spec for v in variants), key=lambda spec: spec.history())
    now = now or datetime.now(UTC)
    run = _Run(
        widest,
        costs,
        tuple(seeds),
        now,
        max_hole,
        trades,
        tuple(stresses),
        volume_slippage,
    )
    cells = len(variants) * len(scenarios)
    if jobs > 1 and cells > 1 and isinstance(source, JupiterCandles):
        rows = await _parallel_rows(source, variants, scenarios, run, jobs)
    else:
        rows = await _serial_rows(source, variants, scenarios, run)
    pairs = zip(variants, rows, strict=True)
    ranked = rank([_variant_result(v, r) for v, r in pairs])
    return {
        "spec_id": variants[0].id,
        "name": widest.name,
        "symbol": widest.symbol,
        "timeframe": str(widest.timeframe),
        "variants": ranked,
        "ranking": [r["spec_id"] for r in ranked],
        "best": ranked[0]["spec_id"],
    }


async def _serial_rows(
    source: CandleRange,
    variants: Sequence[Variant],
    scenarios: Sequence[Scenario],
    run: _Run,
) -> list[list[dict]]:
    """Janela por janela, no processo: só uma janela viva por vez."""
    rows: list[list[dict]] = [[] for _ in variants]
    for scenario in scenarios:
        base, window = await _base_row(source, scenario, run)
        if window is None:
            found = [[base] for _ in variants]
        else:
            found = await _window_cells([v.spec for v in variants], base, window, run)
        for cells, more in zip(rows, found, strict=True):
            cells.extend(more)
    return rows


async def _parallel_rows(
    source: JupiterCandles,
    variants: Sequence[Variant],
    scenarios: Sequence[Scenario],
    run: _Run,
    jobs: int,
) -> list[list[dict]]:
    """As variantes de cada janela em grupos, cada grupo num processo (A37).

    Aqui cada janela é lida uma vez (enche o cache, é medida, recusa a que
    não roda) e solta; o worker a relê do disco, uma vez por grupo. Uma
    janela cujo último pedaço ainda não fechou (não vai para o cache: o
    worker leria outra coisa) roda aqui. A ordem e os valores são os do
    serial.
    """
    per_window = max(1, -(-jobs // len(scenarios)))  # grupos por janela
    groups = [
        g for g in (list(variants[i::per_window]) for i in range(per_window)) if g
    ]
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        cells = _ParallelCells(source, groups, run, pool)
        bases = [await cells.dispatch(i, s) for i, s in enumerate(scenarios)]
        await cells.collect()
    return [
        [cell for i, base in enumerate(bases) for cell in cells.of(i, base, v)]
        for v in variants
    ]


class _ParallelCells:
    """As células de `_parallel_rows`: no processo ou em workers, por janela."""

    def __init__(
        self,
        source: JupiterCandles,
        groups: list[list[Variant]],
        run: _Run,
        pool: ProcessPoolExecutor,
    ):
        self.source, self.groups, self.run, self.pool = source, groups, run, pool
        self.pending: dict[tuple[int, int], asyncio.Future] = {}
        # janela -> id da variante -> suas células
        self.done: dict[int, dict[str, list[dict]]] = {}

    async def dispatch(self, i: int, scenario: Scenario) -> dict:
        """Lê a janela `i`, roda ou manda rodar as variantes; a linha dela."""
        base, window = await _base_row(self.source, scenario, self.run)
        if window is not None and not _cached(window, self.run):
            variants = [v for g in self.groups for v in g]
            found = await _window_cells(
                [v.spec for v in variants], base, window, self.run
            )
            self.done[i] = {v.id: c for v, c in zip(variants, found, strict=True)}
        elif window is not None:
            self._submit(i, scenario, base)
        return base

    def _submit(self, i: int, scenario: Scenario, base: dict) -> None:
        loop = asyncio.get_running_loop()
        root = self.source.cache.root
        for g, group in enumerate(self.groups):
            specs = [v.spec for v in group]
            self.pending[i, g] = loop.run_in_executor(
                self.pool, _cell_job, root, specs, scenario, base, self.run
            )

    async def collect(self) -> None:
        results = await asyncio.gather(*self.pending.values())
        for (i, g), per_spec in zip(self.pending, results, strict=True):
            cells = self.done.setdefault(i, {})
            cells.update(
                {v.id: c for v, c in zip(self.groups[g], per_spec, strict=True)}
            )

    def of(self, i: int, base: dict, variant: Variant) -> list[dict]:
        return [base] if "error" in base else self.done[i][variant.id]


def _cached(window: Window, run: _Run) -> bool:
    """O pedaço do fim da janela já fechou: está no cache como o pai o leu."""
    interval = run.widest.timeframe
    bar = timedelta(seconds=interval.seconds)
    last = chunks(interval, window.end - bar, window.end)[-1]
    return last.is_final(datetime.now(UTC))


async def _base_row(
    source: CandleRange, scenario: Scenario, run: _Run
) -> tuple[dict, Window | None]:
    """A linha da janela (ou do erro dela) e a janela lida."""
    try:
        window = await run.window(source, scenario)
    except ValueError as ex:
        return {**asdict(scenario), "error": str(ex)}, None
    return _window_row(scenario, window, run.widest.timeframe), window


def _cell_job(
    root: Path | None,
    specs: list[StrategySpec],
    scenario: Scenario,
    base: dict,
    run: _Run,
) -> list[list[dict]]:
    """Num worker: relê a janela do cache e roda cada spec nas sementes."""
    return asyncio.run(_cell_in_worker(root, specs, scenario, base, run))


async def _cell_in_worker(
    root: Path | None,
    specs: list[StrategySpec],
    scenario: Scenario,
    base: dict,
    run: _Run,
) -> list[list[dict]]:
    async with aclosing(JupiterCandles(cache=CandleCache(root))) as source:
        window = await run.window(source, scenario)
    return await _window_cells(specs, base, window, run)


def _window_row(scenario: Scenario, window: Window, interval: Interval) -> dict:
    """O que a janela é, igual para todas as variantes."""
    stats = window_stats(window.candles, interval)
    return {
        **asdict(scenario),
        "stats": asdict(stats),
        "flags": flags(scenario.tags, stats),
        "coverage": window.coverage.as_dict(),
        "hold_pct": hold_return_pct(window.ticks),
    }


async def _window_cells(
    specs: list[StrategySpec], base: dict, window: Window, run: _Run
) -> list[list[dict]]:
    """As células de cada spec na janela, por estresse x semente (a base
    primeiro). O estresse é o laço de fora: os ticks de um choque são feitos
    uma vez por janela, e só os de um estresse ficam vivos por vez (A38)."""
    interval = run.widest.timeframe
    scale = volume_scales(window.candles, interval) if run.volume_slippage else None
    found: list[list[dict]] = [[] for _ in specs]
    for stress in run.stresses:
        ticks = stress.ticks(window.ticks, window.start, window.end)
        row = {**base, "stress": stress.name, "hold_pct": hold_return_pct(ticks)}
        job = _Job(row, window, ticks, stress.scaled(run.costs), scale)
        for cells, spec in zip(found, specs, strict=True):
            cells.extend([await _cell(spec, job, seed, run) for seed in run.seeds])
    return found


@dataclass(frozen=True)
class _Job:
    """O que as células de um estresse numa janela têm em comum."""

    row: dict
    window: Window
    ticks: list[Tick]
    costs: dict
    scale: VolumeScales | None = None


async def _cell(spec: StrategySpec, job: _Job, seed: str, run: _Run) -> dict:
    row = job.row
    try:
        result = await backtest_spec(
            spec,
            job.ticks,
            seed=seed,
            warmup=job.window.warmup,
            until=job.window.end,
            slippage_scale=job.scale,
            **job.costs,
        )
    except ValueError as ex:
        return {**row, "seed": seed, "error": str(ex)}
    if not run.trades:
        result = {k: v for k, v in result.items() if k != "trades"}
    vs_hold = float(result["return_pct"]) - row["hold_pct"]
    return {**row, "seed": seed, "vs_hold_pct": vs_hold, "result": result}


def _variant_result(variant: Variant, rows: list[dict]) -> dict:
    """As linhas da variante e os resumos: o da base fora do `holdout` (o que
    ranqueia), o do `holdout` (nunca ranqueia, para não escolher no que serve
    de prova) e um por estresse fora do `holdout` (A38). Uma janela que não
    leu não tem estresse: conta na base."""
    by_stress: dict[str, list[dict]] = {}
    for r in rows:
        by_stress.setdefault(r.get("stress", BASE.name), []).append(r)
    base = by_stress.pop(BASE.name, [])
    held = [r for r in base if HOLDOUT in r["tags"]]
    summary = summarize([r for r in base if HOLDOUT not in r["tags"]])
    stressed = {
        name: summarize([r for r in cells if HOLDOUT not in r["tags"]])
        for name, cells in by_stress.items()
    }
    return {
        "spec_id": variant.id,
        "changes": variant.changes,
        "windows": rows,
        "summary": summary,
        "holdout": summarize(held) if held else None,
        "stress": stressed,
        "fragile": is_fragile(summary, stressed),
    }


def is_fragile(summary: dict, stressed: dict[str, dict]) -> bool:
    """Ganha na base (mediana > 0) e perde (mediana <= 0) em algum estresse
    que rodou."""
    if not summary.get("median_return_pct", 0) > 0:
        return False
    medians = [s["median_return_pct"] for s in stressed.values() if s["ran"]]
    return any(m <= 0 for m in medians)


def rank(results: list[dict]) -> list[dict]:
    """Pela mediana das janelas que ranqueiam e, empatando, pela pior; sem
    nenhuma janela, por último (o empate final fica na ordem dada: a base
    primeiro)."""

    def key(result: dict) -> tuple:
        s = result["summary"]
        if not s["ran"]:
            return (1, 0.0, 0.0)
        return (0, -s["median_return_pct"], -s["worst_return_pct"])

    return sorted(results, key=key)


def summarize(rows: list[dict]) -> dict:
    """O resumo das linhas que rodaram (cada semente de uma janela conta)."""
    ran = [r for r in rows if "error" not in r]
    summary: dict = {"windows": len({r["id"] for r in rows}), "ran": len(ran)}
    if not ran:
        return summary
    returns = [float(r["result"]["return_pct"]) for r in ran]
    worst = min(range(len(ran)), key=returns.__getitem__)
    return {
        **summary,
        "median_return_pct": statistics.median(returns),
        "worst_return_pct": returns[worst],
        "worst_id": _cell_id(ran[worst], len(ran) > summary["windows"]),
        "worst_drawdown_pct": max(float(r["result"]["max_drawdown_pct"]) for r in ran),
        "profitable_pct": 100 * sum(1 for x in returns if x > 0) / len(ran),
        "trades": sum(r["result"]["closed_trades"] for r in ran),
        "median_vs_hold_pct": statistics.median(r["vs_hold_pct"] for r in ran),
    }


def _cell_id(row: dict, seeded: bool) -> str:
    """A janela, e a semente quando há várias."""
    return f"{row['id']}#{row['seed']}" if seeded else row["id"]
