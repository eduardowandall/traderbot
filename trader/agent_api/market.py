"""Dados de mercado para o agente decidir o que operar.

Usa os mesmos indicadores (`trader.indicators`) que as estratégias, então o
`summary` mostra exatamente os valores que uma spec vai enxergar.
"""

from collections.abc import Sequence
from decimal import Decimal

from trader import indicators as ind
from trader.market import MarketData
from trader.models import SOLANA_MINTS, Interval, TickerData
from trader.strategy_spec.validate import SpecLimits

# janelas do resumo, em barras do timeframe pedido
CHANGE_BARS = (1, 12, 60)
MA_WINDOWS = (20, 50)
VOLATILITY_WINDOW = 20
RSI_PERIOD = 14


def symbols(limits: SpecLimits) -> list[dict]:
    """Tokens do registro; `tradable` = pode ser a saída de uma spec."""
    allowed = set(limits.allowed_symbols)
    return [
        {
            "symbol": mint.symbol,
            "mint": mint.mint,
            "decimals": mint.decimals,
            "usd_stable": mint.is_usd_stable,
            "allowed": not allowed or mint.symbol in allowed,
            "tradable": not mint.is_usd_stable
            and (not allowed or mint.symbol in allowed),
        }
        for mint in SOLANA_MINTS.values()
    ]


async def price(data: MarketData, symbol: str) -> dict:
    mint = SOLANA_MINTS.get_by_symbol(symbol)
    return {"symbol": symbol, "price_usd": await data.get_price(mint.mint)}


async def candles(
    data: MarketData, symbol: str, interval: Interval, n: int
) -> list[TickerData]:
    mint = SOLANA_MINTS.get_by_symbol(symbol)
    return await data.get_candles(mint.mint, interval, n)


def _moving_averages(closes: Sequence[Decimal]) -> dict:
    return {
        f"{kind}{window}": ind.moving_average(kind, closes, window)
        for kind in ("sma", "ema", "wma")
        for window in MA_WINDOWS
    }


def summarize(closes: Sequence[Decimal]) -> dict:
    """Indicadores sobre uma série de fechamentos (mais antigo primeiro)."""
    return {
        "bars": len(closes),
        "last": closes[-1] if closes else None,
        "pct_change": {f"{b}": ind.pct_change(closes, b) for b in CHANGE_BARS},
        f"volatility{VOLATILITY_WINDOW}": ind.volatility(closes, VOLATILITY_WINDOW),
        f"rsi{RSI_PERIOD}": ind.rsi(closes, RSI_PERIOD),
        "moving_averages": _moving_averages(closes),
        "high": ind.rolling_high(closes, len(closes)),
        "low": ind.rolling_low(closes, len(closes)),
    }


# 5x a EMA50: o bastante para o resumo convergir como numa spec aquecida
SUMMARY_MIN_BARS = 250


async def summary(data: MarketData, symbol: str, interval: Interval, n: int) -> dict:
    n = max(n, SUMMARY_MIN_BARS)
    bars = ind.BarSeries(interval, maxlen=n)
    bars.seed(await candles(data, symbol, interval, n))
    return {"symbol": symbol, "interval": str(interval), **summarize(bars.closes)}
