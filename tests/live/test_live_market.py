"""Dados de mercado reais e o `backtest` sobre candles reais."""

import asyncio
from decimal import Decimal

from live_helpers import invoke_json

from trader.backtest.ticks import PATH_STEPS
from trader.market import JupiterMarketData, JupiterPriceOracle
from trader.models import SOLANA_MINTS, Interval
from trader.paths import PROJECT_ROOT
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
EXAMPLE_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json")
RANDOM_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-random.json")


def test_price_and_candles():
    async def read():
        data = JupiterMarketData()
        try:
            price = await data.get_price(SOL)
            candles = await data.get_candles(SOL, Interval.MINUTE_1, 20)
        finally:
            await data.aclose()
        return price, candles

    price, candles = asyncio.run(read())

    assert Decimal("1") < price < Decimal("100000")
    assert len(candles) == 20
    assert all(c.low <= c.last <= c.high for c in candles)


def test_example_spec_backtests_on_real_candles():
    result = invoke_json("backtest", EXAMPLE_SPEC, "--candles", "300")

    # cada barra fechada vira um caminho interpolado abertura->mín->máx->fech
    # (até 1 + 3 * PATH_STEPS ticks); a barra em formação fica de fora
    assert result["bars"] in (299, 300)
    assert result["bars"] <= result["ticks"] <= (1 + 3 * PATH_STEPS) * result["bars"]
    assert result["symbol"] == "SOL-USDC"


def test_random_backtest_trades_on_real_candles():
    result = invoke_json("backtest", RANDOM_SPEC, "--candles", "300")

    assert 299 <= result["ticks"] <= (1 + 3 * PATH_STEPS) * 300
    assert len(result["trades"]) > 10


def test_price_api_prices_every_registry_mint_close_to_the_websocket():
    async def check():
        client = AsyncJupiterClient()
        try:
            oracle = JupiterPriceOracle(client)
            mints = [m.mint for m in SOLANA_MINTS.values()]
            prices = await oracle.usd_prices(mints)
            ws_sol = await JupiterMarketData(client).get_price(SOL)
            # websocket mudo: o preço vem da Price API
            fallback = await JupiterMarketData(client, price_timeout=0.001).get_price(
                SOL
            )
        finally:
            await client.aclose()
        return prices, ws_sol, fallback

    prices, ws_sol, fallback = asyncio.run(check())

    assert set(prices) == {m.mint for m in SOLANA_MINTS.values()}
    assert prices[SOLANA_MINTS.get_by_symbol("USDC").mint] == Decimal("1")
    assert abs(prices[SOL] / ws_sol - 1) < Decimal("0.02")
    assert abs(fallback / ws_sol - 1) < Decimal("0.02")
