"""Backtest de uma spec: o único motor, usado por `main.py backtest`.

Reproduz a spec com o orçamento e a perda máxima dela (o mesmo teto do bucket
ao vivo) e recusa dados curtos demais: sem aquecer, a spec nunca opera, e um
resultado com 0 trades enganaria.
"""

from dataclasses import asdict
from decimal import Decimal

from trader.backtest.replay import Backtester, BacktestResult
from trader.backtest.ticks import Tick, ticks_from_candles
from trader.indicators import to_utc
from trader.market import MarketData
from trader.models import SOLANA_MINTS
from trader.models.public_data import Interval
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.strategy import SpecStrategy

DEFAULT_BACKTEST_CANDLES = 1000
# barras avaliadas depois do aquecimento, no mínimo
MIN_EVAL_BARS = 20


async def fetch_ticks(data: MarketData, spec: StrategySpec, n: int) -> list[Tick]:
    """Ticks dos candles fechados do timeframe da spec (abertura->mín->máx->fech)."""
    token, _ = SOLANA_MINTS.get_pair(spec.symbol)
    raw = await data.get_candles(token.mint, spec.timeframe, n)
    return ticks_from_candles(raw, spec.timeframe)


def bars_in(ticks: list[Tick], interval: Interval) -> int:
    """Quantas barras do timeframe os ticks cobrem."""
    return len(
        {int(to_utc(t.timestamp).timestamp()) // interval.seconds for t in ticks}
    )


async def backtest_spec(
    spec: StrategySpec,
    ticks: list[Tick],
    fee_bps: Decimal = Decimal("30"),
    seed: str = "0",
    slippage_bps: Decimal = Decimal("10"),
) -> dict:
    bars = bars_in(ticks, spec.timeframe)
    if bars < spec.history() + MIN_EVAL_BARS:
        raise ValueError(
            f"{bars} barras de {spec.timeframe} < aquecimento de "
            f"{spec.history()} barras + {MIN_EVAL_BARS} avaliadas: use mais candles"
        )
    result = await Backtester(
        SpecStrategy(spec),
        spec.symbol,
        ticks,
        initial_balance=spec.budget_usd,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        seed=seed,
        budget_usd=spec.budget_usd,
        max_loss_usd=spec.max_loss_usd,
    ).run()
    return {
        "spec_id": spec.spec_id(),
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
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
    }
