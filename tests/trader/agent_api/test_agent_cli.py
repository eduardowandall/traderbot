import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec
from typer.testing import CliRunner

import main as main_module
from trader.agent_api import cli
from trader.models import TickerData

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PRICES = [100, 99, 97, 95, 96, 98, 101, 104, 103, 99, 96, 94, 97, 100, 102] * 4


class FakeMarket:
    """`MarketData` falsa: candles de 1 min com os preços dados."""

    def __init__(self, prices=PRICES):
        self.prices = [Decimal(p) for p in prices]
        self.closed = False
        self.requests = []

    async def get_price(self, mint):
        return self.prices[-1]

    async def get_candles(self, mint, interval, candle_qty):
        self.requests.append((mint, interval, candle_qty))
        return [
            TickerData(
                buy=p,
                timestamp=T0 + timedelta(minutes=i),
                high=p,
                last=p,
                low=p,
                open=p,
                pair="x",
                sell=p,
                vol=Decimal(0),
            )
            for i, p in enumerate(self.prices[-candle_qty:])
        ]

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_market(monkeypatch):
    market = FakeMarket()
    monkeypatch.setattr(cli, "MARKET_DATA", lambda: market)
    return market


def _invoke(*args):
    result = CliRunner().invoke(main_module.app, list(args))
    # stdout é só o JSON: logs e avisos vão para o stderr
    return result, json.loads(result.stdout)


def _spec_file(tmp_path, **overrides):
    overrides.setdefault(
        "expires_at", (datetime.now(UTC) + timedelta(days=5)).isoformat()
    )
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(make_spec(**overrides)), encoding="utf-8")
    return path


def test_schema():
    result, body = _invoke("strategy", "schema")
    assert result.exit_code == 0
    assert body["ok"] is True
    assert "entry" in body["schema"]["properties"]


def test_validate_ok(tmp_path):
    result, body = _invoke("strategy", "validate", str(_spec_file(tmp_path)))
    assert result.exit_code == 0, result.output
    assert body["valid"] is True
    assert len(body["spec_id"]) == 12
    assert body["warmup_bars"] == 1


def test_validate_reports_policy_limits(tmp_path):
    # política padrão: max_trade_usd = 25
    path = _spec_file(tmp_path, sizing={"type": "fixed_usd", "usd": 30})
    result, body = _invoke("strategy", "validate", str(path))
    assert result.exit_code == 1
    assert body["ok"] is False
    assert [e["path"] for e in body["errors"]] == ["sizing.usd"]


@pytest.mark.parametrize("content", ["not json", '{"version": 1}'])
def test_validate_reports_parse_errors(tmp_path, content):
    path = tmp_path / "spec.json"
    path.write_text(content, encoding="utf-8")
    result, body = _invoke("strategy", "validate", str(path))
    assert result.exit_code == 1
    assert body["ok"] is False and body["errors"]


def test_validate_missing_file_and_bad_mode(tmp_path):
    result, body = _invoke("strategy", "validate", str(tmp_path / "nope.json"))
    assert result.exit_code == 1 and not body["ok"]
    path = _spec_file(tmp_path)
    result, body = _invoke("strategy", "validate", str(path), "--mode", "live")
    assert result.exit_code == 1 and "modo" in body["errors"][0]["msg"]


SWING = {
    "entry": {"conditions": [{"type": "dip_from_high", "window": 5, "pct": 3}]},
    "exit": {
        "stop": {"type": "trailing_stop", "pct": 3},
        "conditions": [{"type": "take_profit", "pct": 4}],
    },
}


def test_backtest_with_candles(tmp_path, fake_market):
    path = _spec_file(tmp_path, **SWING)
    result, body = _invoke("strategy", "backtest", str(path), "--candles", "60")
    assert result.exit_code == 0, result.output
    assert body["ok"] and body["trades"]
    assert Decimal(body["initial_equity"]) == Decimal(50)  # budget_usd
    assert fake_market.requests[0][2] == 60
    assert fake_market.closed


def test_backtest_with_ticks_file(tmp_path):
    ticks = tmp_path / "ticks.csv"
    ticks.write_text(
        "\n".join(
            f"{(T0 + timedelta(minutes=i)).isoformat()},{p}"
            for i, p in enumerate(PRICES)
        ),
        encoding="utf-8",
    )
    path = _spec_file(tmp_path, **SWING)
    result, body = _invoke("strategy", "backtest", str(path), "--ticks", str(ticks))
    assert result.exit_code == 0, result.output
    assert body["ticks"] == len(PRICES)


def test_market_symbols():
    result, body = _invoke("market", "symbols")
    assert result.exit_code == 0
    by_symbol = {s["symbol"]: s for s in body["symbols"]}
    assert by_symbol["SOL"]["tradable"] is True
    assert by_symbol["USDC"]["tradable"] is False


def test_market_price_candles_and_summary(fake_market):
    _, price = _invoke("market", "price", "SOL")
    assert Decimal(price["price_usd"]) == Decimal(102)

    _, candles = _invoke("market", "candles", "SOL", "--n", "3")
    assert len(candles["candles"]) == 3

    result, summary = _invoke("market", "summary", "SOL", "--n", "60")
    assert result.exit_code == 0, result.output
    assert summary["bars"] == 60
    assert Decimal(summary["last"]) == Decimal(102)
    assert summary["rsi14"] is not None
    assert set(summary["moving_averages"]) == {
        "sma20",
        "sma50",
        "ema20",
        "ema50",
        "wma20",
        "wma50",
    }


def test_market_unknown_symbol_is_a_json_error(fake_market):
    result, body = _invoke("market", "price", "NOPE")
    assert result.exit_code == 1
    assert "NOPE" in body["errors"][0]["msg"]


def test_run_rejects_an_unreadable_spec(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    missing = CliRunner().invoke(main_module.app, ["run", "paper", "nope.json"])
    broken = CliRunner().invoke(main_module.app, ["run", "paper", str(path)])
    assert missing.exit_code != 0 and "nope.json" in missing.output
    assert broken.exit_code != 0 and "broken.json" in broken.output


def test_decimals_are_printed_in_plain_notation():
    from trader.agent_api.output import dumps

    body = json.loads(dumps({"a": Decimal("0E-26"), "b": Decimal("1.50")}))
    assert body == {"a": "0", "b": "1.5"}
