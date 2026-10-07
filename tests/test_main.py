"""O CLI: `serve`, `connect` e `backtest`."""

import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx
import pytest
from factories import example_spec, make_spec, spot_provider
from typer.testing import CliRunner

import main as main_module
from trader.api.cli import backtest as cli_backtest
from trader.api.cli.output import dumps
from trader.execution.market.hub import PriceHub
from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.execution.models.mode import RunningMode
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.venues.paper import SimulatedExecutor
from trader.shared.models import SOLANA_MINTS, TickerData
from trader.shared.models.mints import SOL_MINT
from trader.shared.notification import (
    NotificationService,
    TelegramNotificationService,
    notifier_from_env,
)
from trader.shared.paths import policy_file
from trader.strategy.spec.strategy import SpecStrategy

RANDOM_SPEC = example_spec("random")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PRICES = [100, 99, 97, 95, 96, 98, 101, 104, 103, 99, 96, 94, 97, 100, 102] * 4
# entra na queda, sai com trailing stop ou take profit: opera nos PRICES
SWING = {
    "entry": {"mode": "all", "conditions": [{"type": "price_below", "value": 96}]},
    "exit": {
        "mode": "any",
        "stop": {"type": "trailing_stop", "pct": 3},
        "conditions": [{"type": "take_profit", "pct": 4}],
    },
}


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
                timestamp=T0 + timedelta(minutes=i), high=p, last=p, low=p, open=p
            )
            for i, p in enumerate(self.prices[-candle_qty:])
        ]

    async def aclose(self):
        self.closed = True


class BrokenMarket(FakeMarket):
    async def get_candles(self, mint, interval, candle_qty):
        raise httpx.ConnectError("sem rede")


class FakeQuotes:
    """Quotes para medir custos (B9): cada perna perde 20 bps; SOL a 200 USD."""

    def __init__(self):
        self.quotes = 0
        self.closed = False

    async def get_usd_prices(self, mints):
        return {m: Decimal(200) if m == SOL_MINT else Decimal(1) for m in mints}

    async def get_quote(self, input_mint, output_mint, amount, slippage_bps=50):
        self.quotes += 1
        out = amount * 998 // 1000
        return JupiterQuoteResponse.single_route(input_mint, amount, output_mint, out)

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_quotes(monkeypatch):
    # o backtest mede os custos na Jupiter por padrão: aqui, offline
    quotes = FakeQuotes()
    monkeypatch.setattr(cli_backtest, "COST_QUOTES", lambda: quotes)
    return quotes


@pytest.fixture
def fake_market(monkeypatch):
    market = FakeMarket()
    monkeypatch.setattr(cli_backtest, "MARKET_DATA", lambda: market)
    return market


def _invoke(*args):
    return CliRunner().invoke(main_module.app, [str(a) for a in args])


def _spec_file(tmp_path, **overrides):
    overrides.setdefault(
        "expires_at", (datetime.now(UTC) + timedelta(days=5)).isoformat()
    )
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(make_spec(**overrides)), encoding="utf-8")
    return path


# --- o app ---------------------------------------------------------------------


def test_the_cli_commands():
    names = sorted(
        getattr(c.callback, "__name__", "") for c in main_module.app.registered_commands
    )
    assert names == ["backtest", "connect", "serve"]
    for gone in (
        "run",  # B14: só `serve` + `connect` operam
        "swap",
        "halt",
        "resume",
        "pnl",
        "ledger",
        "paper",
        "market",
        "strategy",
    ):
        assert _invoke(gone).exit_code != 0


# --- serve / connect ---------------------------------------------------------


def _serve(*args):
    """`serve` até montar o trade-runner; devolve (resultado, runner)."""
    with mock.patch("trader.api.cli.runners._serve", new=mock.AsyncMock()) as run:
        result = _invoke("serve", *args)
    assert run.call_args, result.output
    return result, run.call_args.args[0]


def test_serve_requires_the_mode():
    assert _invoke("serve").exit_code != 0
    assert _invoke("serve", "dry").exit_code != 0


def test_serve_paper_needs_no_private_key(monkeypatch):
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    result, runner = _serve("paper")

    assert result.exit_code == 0, result.output
    assert runner.service.mode == "paper"
    executor = spot_provider(runner.service).executor
    assert isinstance(executor, SimulatedExecutor)
    usdc = SOLANA_MINTS.get_by_symbol("USDC").mint
    assert executor.wallet.balance(usdc) == Decimal("100")
    # o hub de preços e o relatório diário rodam junto com o servidor
    assert isinstance(runner.hub, PriceHub)
    (report,) = runner.background
    assert report.__self__.gateway is runner.service.gateway


def test_serve_real_uses_the_key():
    with (
        mock.patch(
            "trader.execution.wiring.AsyncJupiterProvider.on_chain",
            return_value=mock.Mock(),
        ),
        mock.patch(
            "trader.execution.wiring.keypair_from_env", return_value=mock.Mock()
        ) as key,
    ):
        result, runner = _serve("real")

    assert result.exit_code == 0, result.output
    key.assert_called_once()
    assert runner.service.mode == "real"


def _connect(*args):
    with mock.patch("trader.api.cli.runners.strategy_bot") as build:
        result = _invoke("connect", *args)
    return result, build


def test_connect_seeds_the_spec():
    result, build = _connect(RANDOM_SPEC, "--seed", "1")

    assert result.exit_code == 0, result.output
    strategy = build.call_args.args[0]
    assert isinstance(strategy, SpecStrategy)
    assert strategy.rng.random() == random.Random("1").random()
    assert build.call_args.kwargs["on_tick"] is None
    build.return_value.run.assert_called_once()


def test_connect_record_ticks_passes_recorder():
    result, build = _connect(RANDOM_SPEC, "--record-ticks", "ticks/sol.csv")
    assert result.exit_code == 0, result.output
    on_tick = build.call_args.kwargs["on_tick"]
    assert on_tick.__self__.path == Path("ticks/sol.csv")


def test_connect_rejects_an_unreadable_spec(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    missing, build = _connect("nope.json")
    broken, _ = _connect(str(path))
    assert missing.exit_code != 0 and "nope.json" in missing.output
    assert broken.exit_code != 0 and "broken.json" in broken.output
    build.assert_not_called()


def test_notifier_comes_from_the_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert type(notifier_from_env()) is NotificationService
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert type(notifier_from_env()) is NotificationService  # falta o token
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    notifier = notifier_from_env()
    assert isinstance(notifier, TelegramNotificationService)
    assert (notifier.chat_id, notifier.token) == ("123", "abc")


def test_paper_has_roomy_limits_by_default():
    paper = TradeGateway.for_mode(RunningMode.PAPER)
    real = TradeGateway.for_mode(RunningMode.REAL)
    try:
        assert paper.policy.max_trade_usd == 1000
        assert real.policy.max_trade_usd == 25
        assert not real.policy.real_trading_enabled
    finally:
        paper.close()
        real.close()


# --- backtest ------------------------------------------------------------------


def test_backtest_with_candles(tmp_path, fake_market):
    path = _spec_file(tmp_path, **SWING)
    result = _invoke("backtest", path, "--candles", "60", "--json")
    body = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert body["ok"] and body["trades"]
    assert Decimal(body["initial_equity"]) == Decimal(50)  # budget_usd
    assert fake_market.requests[0][2] == 60
    assert fake_market.closed


def test_backtest_runs_a_perp(tmp_path, fake_market):
    market = {"kind": "perp", "venue": "jupiter", "direction": "short", "leverage": 3}
    exit_ = {
        "stop": {"type": "stop_loss", "pct": 5},
        "conditions": [{"type": "max_hold", "minutes": 60}],
    }
    path = _spec_file(tmp_path, **SWING | {"market": market, "exit": exit_})
    args = ("--candles", "60", "--fee-bps", "0", "--network-fee-usd", "0.003")
    body = json.loads(
        _invoke("backtest", path, *args, "--borrow-bps-hour", "5", "--json").stdout
    )
    assert body["ok"], body
    assert body["perp"]["direction"] == "short"
    assert body["perp"]["borrow_bps_hour"] == "5"
    text = _invoke("backtest", path, *args).stdout
    assert "perp short 3x" in text and "(1 bps/h)" in text


def test_backtest_reports_the_cost_per_round_trip(tmp_path, fake_market):
    path = _spec_file(tmp_path, **SWING)
    body = json.loads(_invoke("backtest", path, "--candles", "60", "--json").stdout)
    text = _invoke("backtest", path, "--candles", "60").stdout

    costs = body["round_trip_costs"]
    assert costs["count"] == body["closed_trades"] > 0
    # cada perna paga ao menos 30 + 10 bps: a ida e volta, ao menos ~80 bps
    assert Decimal(costs["bps"]) > 79
    assert f"bps) em {costs['count']}" in text


def test_backtest_measures_the_pair_and_network_fees_by_default(
    tmp_path, fake_market, fake_quotes
):
    policy_file().write_text(
        "[real.trading]\nmax_priority_fee_lamports = 45000\n", encoding="utf-8"
    )
    body = json.loads(
        _invoke("backtest", _spec_file(tmp_path, **SWING), "--json").stdout
    )

    # 20 bps por perna na quote: a ida e volta perde 39.96 bps, metade por perna
    assert body["fee_bps"] == "19.98"
    assert body["network_fee_usd"] == "0.01"  # (5000 + 45000) a 200 USD/SOL
    measured = body["measured_costs"]
    assert measured["round_trip_bps"] == "39.96"
    assert measured["size_usd"] == "20"  # o sizing da spec
    assert fake_quotes.quotes == 2 and fake_quotes.closed


def test_backtest_with_both_costs_given_measures_nothing(
    tmp_path, fake_market, fake_quotes
):
    path = _spec_file(tmp_path, **SWING)
    result = _invoke(
        "backtest", path, "--fee-bps", "5", "--network-fee-usd", "0", "--json"
    )

    body = json.loads(result.stdout)
    assert (body["fee_bps"], body["network_fee_usd"]) == ("5", "0")
    assert body["measured_costs"] is None and fake_quotes.quotes == 0


def test_a_failed_measurement_names_the_flags(tmp_path, fake_market, monkeypatch):
    class Down(FakeQuotes):
        async def get_usd_prices(self, mints):
            raise httpx.ConnectError("sem rede")

    monkeypatch.setattr(cli_backtest, "COST_QUOTES", Down)
    body = json.loads(_invoke("backtest", _spec_file(tmp_path), "--json").stdout)

    message = body["errors"][0]["msg"]
    assert "medir os custos" in message and "--fee-bps" in message


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
    first = _invoke("backtest", path, "--ticks", ticks, "--seed", "1", "--json")
    again = _invoke("backtest", path, "--ticks", ticks, "--seed", "1", "--json")
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["ticks"] == len(PRICES)
    assert again.stdout == first.stdout  # determinístico


def test_backtest_network_fee_is_an_option(tmp_path, fake_market):
    path = _spec_file(tmp_path, **SWING)
    default = json.loads(_invoke("backtest", path, "--candles", "60", "--json").stdout)
    free = _invoke(
        "backtest", path, "--candles", "60", "--network-fee-usd", "0", "--json"
    )
    bad = _invoke("backtest", path, "--network-fee-usd", "x", "--json")

    # medido: (5000 + 100000) lamports a 200 USD/SOL
    assert default["network_fee_usd"] == "0.021"
    body = json.loads(free.stdout)
    assert body["network_fee_usd"] == "0"
    assert Decimal(body["final_equity"]) > Decimal(default["final_equity"])
    assert "network-fee-usd inválido" in json.loads(bad.stdout)["errors"][0]["msg"]


def test_backtest_refuses_too_few_bars(tmp_path):
    ticks = tmp_path / "ticks.csv"
    ticks.write_text(f"{T0.isoformat()},100", encoding="utf-8")
    result = _invoke("backtest", _spec_file(tmp_path), "--ticks", ticks, "--json")
    body = json.loads(result.stdout)
    assert result.exit_code == 1
    assert "aquecimento" in body["errors"][0]["msg"]


def test_backtest_reports_parse_errors_as_json(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(make_spec(budget_usd=-1)), encoding="utf-8")
    result = _invoke("backtest", path, "--json")
    body = json.loads(result.stdout)
    assert result.exit_code == 1
    assert body["ok"] is False and body["errors"][0]["path"] == "budget_usd"


def test_an_unexpected_error_is_still_json(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_backtest, "MARKET_DATA", BrokenMarket)
    result = _invoke("backtest", _spec_file(tmp_path), "--json")
    body = json.loads(result.stdout)
    assert result.exit_code == 1
    assert body["ok"] is False and "ConnectError" in body["errors"][0]["msg"]


def test_backtest_prints_a_readable_summary_by_default(tmp_path, fake_market):
    result = _invoke("backtest", _spec_file(tmp_path, **SWING), "--candles", "60")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("Backtest sol-dip (")
    assert "SOL-USDC 1_MINUTE" in lines[0]
    assert any("USD (" in line and "drawdown" in line for line in lines)
    assert "  trades:" in lines
    assert "{" not in result.stdout  # não é JSON


def test_backtest_errors_are_text_on_stderr_by_default(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(make_spec(budget_usd=-1)), encoding="utf-8")

    result = _invoke("backtest", path)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "erro: budget_usd:" in result.stderr


def test_decimals_are_printed_in_plain_notation():
    body = json.loads(dumps({"a": Decimal("0E-26"), "b": Decimal("1.50")}))
    assert body == {"a": "0", "b": "1.5"}
