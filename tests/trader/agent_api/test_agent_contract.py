"""R8: o contrato dos comandos de agente (sempre JSON, schema útil, id estável)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from factories import make_spec
from typer.testing import CliRunner

import main as main_module
from trader.agent_api import cli
from trader.strategy_spec.models import StrategySpec


class BrokenMarket:
    async def get_price(self, mint):
        raise httpx.ConnectError("sem rede")

    async def get_candles(self, mint, interval, candle_qty):
        raise httpx.ConnectError("sem rede")

    async def aclose(self):
        return None


def _invoke(*args):
    result = CliRunner().invoke(main_module.app, list(args))
    return result, json.loads(result.stdout)


def _spec_file(tmp_path, **overrides):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(make_spec(**overrides)), encoding="utf-8")
    return path


def test_an_unexpected_error_is_still_json(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "MARKET_DATA", BrokenMarket)
    spec = _spec_file(
        tmp_path, expires_at=(datetime.now(UTC) + timedelta(days=5)).isoformat()
    )

    result, body = _invoke("strategy", "backtest", str(spec))
    _, price = _invoke("market", "price", "SOL")

    assert result.exit_code == 1
    assert body["ok"] is False and "ConnectError" in body["errors"][0]["msg"]
    assert price["ok"] is False


def test_a_failed_validate_keeps_the_spec_context(tmp_path):
    result, body = _invoke("strategy", "validate", str(_spec_file(tmp_path)))

    assert result.exit_code == 1
    assert body["ok"] is False
    assert body["errors"][0]["path"] == "expires_at"  # 2099
    assert len(body["spec_id"]) == 12
    assert body["warmup_bars"] >= 1


def test_the_schema_has_bounds_and_descriptions():
    _, body = _invoke("strategy", "schema")
    schema = body["schema"]

    pct = schema["$defs"]["StopLoss"]["properties"]["pct"]
    assert pct["type"] == "number" and pct["maximum"] == 50
    for field in ("budget_usd", "max_loss_usd", "ttl_days", "symbol", "timeframe"):
        assert schema["properties"][field].get("description"), field


def test_the_id_ignores_metadata_but_not_behavior():
    base = StrategySpec.model_validate(make_spec())
    reworded = StrategySpec.model_validate(
        make_spec(name="outro-nome", rationale="outra justificativa", agent_id="x")
    )
    bigger = StrategySpec.model_validate(make_spec(budget_usd=60))

    assert base.spec_id() == reworded.spec_id()
    assert base.spec_id() != bigger.spec_id()
