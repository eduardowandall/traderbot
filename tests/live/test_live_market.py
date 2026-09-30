"""Dados de mercado reais: preço, candles e resumo (comandos de agente)."""

import asyncio
import re
from decimal import Decimal

from live_helpers import invoke, invoke_json

from trader.backtest.ticks import PATH_STEPS
from trader.market import JupiterMarketData, JupiterPriceOracle
from trader.models import SOLANA_MINTS
from trader.paths import PROJECT_ROOT
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
EXAMPLE_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json")
RANDOM_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-random.json")


def test_price_candles_and_summary():
    price = Decimal(invoke_json("market", "price", "SOL")["price_usd"])
    assert Decimal("1") < price < Decimal("100000")

    candles = invoke_json("market", "candles", "SOL", "--n", "20")["candles"]
    assert len(candles) == 20

    summary = invoke_json("market", "summary", "SOL", "--interval", "1_MINUTE")
    assert summary["bars"] > 50
    assert 0 <= Decimal(summary["rsi14"]) <= 100


def test_example_spec_validates_and_backtests_on_real_candles():
    # o exemplo usa `ttl_days`: valida sem precisar de uma data nova
    invoke_json("strategy", "validate", EXAMPLE_SPEC)
    result = invoke_json("strategy", "backtest", EXAMPLE_SPEC, "--candles", "300")

    # cada barra fechada vira um caminho interpolado abertura->mín->máx->fech
    # (até 1 + 3 * PATH_STEPS ticks); a barra em formação fica de fora
    assert result["bars"] in (299, 300)
    assert result["bars"] <= result["ticks"] <= (1 + 3 * PATH_STEPS) * result["bars"]
    assert result["symbol"] == "SOL-USDC"


def test_random_backtest_trades_on_real_candles():
    result = invoke(
        "backtest",
        RANDOM_SPEC,
        "--candles",
        "100",
    )

    assert result.exit_code == 0, result.output
    ticks = re.search(r"(\d+) ticks", result.stdout)
    assert ticks and 99 <= int(ticks.group(1)) <= (1 + 3 * PATH_STEPS) * 100
    trades = re.search(r"trades: (\d+)", result.stdout)
    assert trades and int(trades.group(1)) > 10


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
