"""Dados de mercado reais e o `backtest` sobre candles reais."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from live_helpers import invoke_json
from solders.keypair import Keypair

from trader.backtest.compare import fetch_warmup
from trader.backtest.ticks import PATH_STEPS
from trader.market import JupiterMarketData, JupiterPriceOracle
from trader.market.hub import PriceHub
from trader.market.pair import market_for
from trader.market.prices import usd_snapshot
from trader.models import SOLANA_MINTS, Interval
from trader.paths import PROJECT_ROOT
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.tx_inspection import (
    JUPITER_V6,
    check_programs,
    programs_of,
)
from trader.strategy_spec.validate import parse_spec

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
JUP = SOLANA_MINTS.get_by_symbol("JUP").mint
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


def test_warmup_candles_are_the_bars_just_before_the_first_tick():
    # `live_vs_backtest.py`: o candle da API é marcado pela abertura da barra
    spec = parse_spec(Path(EXAMPLE_SPEC).read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    first_tick = now - timedelta(minutes=10)

    async def read():
        data = JupiterMarketData()
        try:
            return await fetch_warmup(data, spec, first_tick, now)
        finally:
            await data.aclose()

    warmup = asyncio.run(read())

    seconds = spec.timeframe.seconds
    bars = [int(c.timestamp.timestamp()) // seconds for c in warmup]
    assert len(warmup) == spec.history()
    assert bars == sorted(bars) and bars[-1] < int(first_tick.timestamp()) // seconds
    # sem buraco grande antes dos ticks (o banco de indicadores recomeçaria)
    assert int(first_tick.timestamp()) // seconds - bars[-1] <= 2


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
            prices = await usd_snapshot(oracle, mints)  # stablecoins a 1
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


PAIR_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-jup-sol-expr.json")


def test_a_non_stable_pair_backtests_on_two_real_series(tmp_path):
    # B6: JUP-SOL com entradas aleatórias, para operar com certeza
    spec = json.loads(Path(PAIR_SPEC).read_text(encoding="utf-8"))
    spec["entry"]["conditions"] = [{"type": "random_chance", "pct": 5}]
    spec["exit"]["conditions"] = [{"type": "random_chance", "pct": 5}]
    spec["cooldown_minutes"] = 0
    path = tmp_path / "jup-sol-random.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    result = invoke_json("backtest", str(path), "--candles", "300", "--seed", "1")

    # as duas séries têm as mesmas barras (quase todas)
    assert 280 <= result["bars"] <= 300
    assert result["closed_trades"] > 3
    # preço de entrada em USD por JUP: perto do da Price API (centavos)
    assert all(Decimal("0.01") < Decimal(t["price"]) < 10 for t in result["trades"])


def test_the_pair_feed_is_token_usd_over_quote_usd():
    async def read():
        pair = market_for("JUP-SOL", JupiterMarketData)
        client = AsyncJupiterClient()
        try:
            ratio = await pair.get_price(JUP)
            usd = await client.get_usd_prices([JUP, SOL])
            candles = await pair.get_candles(JUP, Interval.MINUTE_1, 20)
        finally:
            await pair.aclose()
            await client.aclose()
        return ratio, usd, candles

    ratio, usd, candles = asyncio.run(read())

    assert abs(ratio / (usd[JUP] / usd[SOL]) - 1) < Decimal("0.02")
    assert len(candles) >= 18
    assert abs(candles[-1].last / ratio - 1) < Decimal("0.05")


def test_the_price_hub_keeps_an_active_and_a_quiet_token_fresh():
    # B7: um websocket para os dois; a Price API cobre o parado (soak F2)
    quiet = SOLANA_MINTS.get_by_symbol("NOBODY").mint

    async def watch():
        hub = PriceHub()
        task = asyncio.create_task(hub.run())
        try:
            await hub.price(SOL)
            await hub.price(quiet)
            await asyncio.sleep(12)
            return await hub.price(SOL), await hub.price(quiet)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    (sol, sol_age), (nobody, nobody_age) = asyncio.run(watch())

    assert Decimal(1) < sol < Decimal(100000) and nobody > 0
    assert sol_age < 5  # o websocket manda SOL o tempo todo
    assert nobody_age < 10  # parado no websocket, a API renova


def test_a_real_swap_transaction_uses_only_allowed_programs():
    # B7: a inspeção de programas sobre transações reais da Jupiter
    async def build():
        client = AsyncJupiterClient()
        try:
            usdc = SOLANA_MINTS.get_by_symbol("USDC").mint
            quote = await client.get_quote(usdc, SOL, 5_000_000, 50)
            return await client.get_swap_transaction(quote, Keypair().pubkey())
        finally:
            await client.aclose()

    tx = asyncio.run(build())

    check_programs(tx)
    assert JUPITER_V6 in programs_of(tx)
