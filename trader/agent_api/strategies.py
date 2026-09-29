"""Specs de estratégia para o agente: schema, validação e backtest local.

Nada é gravado aqui (o registro de specs vem na fase 3). O backtest usa o
mesmo `Backtester` do `main.py backtest`, com a spec como estratégia e o
`budget_usd` como saldo inicial.
"""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from trader.backtest import Backtester, BacktestResult, Tick, ticks_from_candles
from trader.market import MarketData
from trader.models import SOLANA_MINTS
from trader.policy import Policy
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.strategy import SpecStrategy
from trader.strategy_spec.validate import SpecLimits, parse_spec, validate

DEFAULT_BACKTEST_CANDLES = 1000


def limits_from_policy(policy: Policy) -> SpecLimits:
    return SpecLimits(
        max_trade_usd=policy.max_trade_usd, allowed_symbols=policy.allowed_symbols
    )


def schema() -> dict:
    return StrategySpec.model_json_schema()


def check(text: str, limits: SpecLimits, now: datetime | None = None) -> dict:
    """Lê e valida; `SpecParseError` sobe para a CLI se o JSON for inválido."""
    spec = parse_spec(text)
    errors = validate(spec, limits, now)
    return {
        "spec_id": spec.spec_id(),
        "valid": not errors,
        "errors": [asdict(e) for e in errors],
        "warmup_bars": spec.lookback(),
        "timeframe": str(spec.timeframe),
    }


async def fetch_ticks(data: MarketData, spec: StrategySpec, n: int) -> list[Tick]:
    """Um tick por candle do timeframe da spec (fechamento)."""
    token, _ = SOLANA_MINTS.get_pair(spec.symbol)
    return ticks_from_candles(await data.get_candles(token.mint, spec.timeframe, n))


async def backtest(
    spec: StrategySpec, ticks: list[Tick], fee_bps: Decimal, seed: str
) -> dict:
    result = await Backtester(
        SpecStrategy(spec),
        spec.symbol,
        ticks,
        initial_balance=spec.budget_usd,
        fee_bps=fee_bps,
        seed=seed,
        # mesmo teto do bucket ao vivo: prejuízo realizado reduz o orçamento
        budget_usd=spec.budget_usd,
    ).run()
    return {"spec_id": spec.spec_id(), "fee_bps": fee_bps, **result_to_dict(result)}


def result_to_dict(result: BacktestResult) -> dict:
    return {
        **asdict(result),
        "return_pct": result.return_pct,
        "win_rate_pct": result.win_rate_pct,
        "closed_trades": len(result.closed_trades),
    }
