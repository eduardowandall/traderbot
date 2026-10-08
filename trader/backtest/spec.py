"""Backtest de uma spec: o único motor, usado por `main.py backtest`.

Reproduz a spec com o orçamento e a perda máxima dela (o mesmo teto do bucket
ao vivo) e recusa dados curtos demais: sem aquecer, a spec nunca opera, e um
resultado com 0 trades enganaria.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

from trader.backtest.replay import Backtester, BacktestResult
from trader.backtest.ticks import Tick, ticks_from_candles
from trader.execution.market.jupiter.candles import MAX_CANDLES
from trader.execution.models.perp import perp_terms_for
from trader.execution.trade.venues.paper.perps import DEFAULT_BORROW_BPS_HOUR
from trader.shared.indicators import bar_index
from trader.shared.market import CandleSource
from trader.shared.market.pair import ratio_candles
from trader.shared.models import SOLANA_MINTS, TickerData
from trader.shared.models.public_data import Interval
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.strategy import SpecStrategy

DEFAULT_BACKTEST_CANDLES = MAX_CANDLES
# barras avaliadas depois do aquecimento, no mínimo
MIN_EVAL_BARS = 20
# taxa de rede por perna (USD) de quem chama `backtest_spec` direto (testes,
# `compare_live`); o `backtest` do CLI mede a base + o teto da priority fee ao
# preço do SOL agora (`trader/backtest/costs.py`)
DEFAULT_NETWORK_FEE_USD = Decimal("0.002")


async def fetch_ticks(data: CandleSource, spec: StrategySpec, n: int) -> list[Tick]:
    """Ticks dos candles fechados do timeframe da spec (abertura->mín->máx->fech).

    Num par sem stablecoin, duas séries: o token em cotação (a razão dos
    candles USD) e o preço USD do token de cotação em cada tick.
    """
    token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    raw = await data.get_candles(token.mint, spec.timeframe, n)
    if quote.is_usd_stable:
        return ticks_from_candles(raw, spec.timeframe)
    quote_raw = await data.get_candles(quote.mint, spec.timeframe, n)
    return ticks_from_candles(
        ratio_candles(raw, quote_raw), spec.timeframe, quote=quote_raw
    )


def bars_in(ticks: list[Tick], interval: Interval) -> int:
    """Quantas barras do timeframe os ticks cobrem."""
    return len({bar_index(t.timestamp, interval.seconds) for t in ticks})


@dataclass(frozen=True)
class ReplayCosts:
    fee_bps: Decimal = Decimal("30")
    slippage_bps: Decimal = Decimal("10")
    network_fee_usd: Decimal = DEFAULT_NETWORK_FEE_USD
    # perps (A9): empréstimo por hora, em bps do tamanho
    borrow_bps_hour: Decimal = DEFAULT_BORROW_BPS_HOUR


def spec_backtester(
    spec: StrategySpec,
    ticks: list[Tick],
    costs: ReplayCosts,
    seed: str,
    warmup: Sequence[TickerData] = (),
) -> Backtester:
    """O backtest de uma spec: o orçamento e a perda máxima dela; numa perp, o
    motor de perps do paper (A9)."""
    return Backtester(
        SpecStrategy(spec),
        spec.symbol,
        ticks,
        initial_balance=spec.budget_usd,
        fee_bps=costs.fee_bps,
        slippage_bps=costs.slippage_bps,
        network_fee_usd=costs.network_fee_usd,
        seed=seed,
        budget_usd=spec.budget_usd,
        max_loss_usd=spec.max_loss_usd,
        warmup=warmup,
        perp=perp_terms_for(spec.symbol, spec.market, spec.exit.stop.pct),
        borrow_bps_hour=costs.borrow_bps_hour,
    )


async def backtest_spec(
    spec: StrategySpec,
    ticks: list[Tick],
    fee_bps: Decimal = ReplayCosts.fee_bps,
    seed: str = "0",
    slippage_bps: Decimal = ReplayCosts.slippage_bps,
    network_fee_usd: Decimal = ReplayCosts.network_fee_usd,
    borrow_bps_hour: Decimal = ReplayCosts.borrow_bps_hour,
) -> dict:
    bars = bars_in(ticks, spec.timeframe)
    if bars < spec.history() + MIN_EVAL_BARS:
        raise ValueError(
            f"{bars} barras de {spec.timeframe} < aquecimento de "
            f"{spec.history()} barras + {MIN_EVAL_BARS} avaliadas: use mais candles"
        )
    costs = ReplayCosts(fee_bps, slippage_bps, network_fee_usd, borrow_bps_hour)
    result = await spec_backtester(spec, ticks, costs, seed).run()
    return {
        "spec_id": spec.spec_id(),
        "name": spec.name,
        "timeframe": str(spec.timeframe),
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "network_fee_usd": network_fee_usd,
        "bars": bars,
        "warmup_bars": spec.history(),
        **result_to_dict(result),
    }


def result_to_dict(result: BacktestResult) -> dict:
    return {
        **asdict(result),
        "return_pct": result.return_pct,
        "win_rate_pct": result.win_rate_pct,
        "closed_trades": len(result.closed_trades),
        "round_trip_costs": result.round_trip_costs.as_dict(),
    }
