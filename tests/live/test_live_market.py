"""Dados de mercado reais: preço, candles e resumo (comandos de agente)."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from live_helpers import invoke, invoke_json

from trader.paths import PROJECT_ROOT

EXAMPLE_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json")


def test_price_candles_and_summary():
    price = Decimal(invoke_json("market", "price", "SOL")["price_usd"])
    assert Decimal("1") < price < Decimal("100000")

    candles = invoke_json("market", "candles", "SOL", "--n", "20")["candles"]
    assert len(candles) == 20

    summary = invoke_json("market", "summary", "SOL", "--interval", "1_MINUTE")
    assert summary["bars"] > 50
    assert 0 <= Decimal(summary["rsi14"]) <= 100


def test_example_spec_validates_and_backtests_on_real_candles(tmp_path):
    # o exemplo é um modelo (expira em 2099); a validação exige <= 30 dias
    spec = json.loads(Path(EXAMPLE_SPEC).read_text(encoding="utf-8"))
    spec["expires_at"] = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    invoke_json("strategy", "validate", str(spec_file))
    result = invoke_json("strategy", "backtest", str(spec_file), "--candles", "300")

    assert result["ticks"] == 300
    assert result["symbol"] == "SOL-USDC"


def test_random_backtest_trades_on_real_candles():
    result = invoke(
        "backtest",
        "SOL-USDC",
        "random",
        "buy_chance=50 sell_chance=50",
        "--candles",
        "300",
    )

    assert result.exit_code == 0, result.output
    assert "300 ticks" in result.stdout
    trades = re.search(r"trades: (\d+)", result.stdout)
    assert trades and int(trades.group(1)) > 10
