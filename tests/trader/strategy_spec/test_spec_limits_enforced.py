"""R6: uma spec em execução respeita os próprios limites (validade, orçamento, perda)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from factories import make_spec, open_ledger
from pydantic import ValidationError
from typer.testing import CliRunner

import main as main_module
from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution import TradeGateway
from trader.models import SOLANA_MINTS, OrderSide
from trader.paper import SimulatedWallet, paper_provider
from trader.paths import PROJECT_ROOT
from trader.policy import Policy
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.strategy import SpecStrategy
from trader.strategy_spec.validate import SpecLimits, validate
from trader.trading_service.protocol import BucketStatus, OrderRequest, ReplyStatus
from trader.trading_service.service import TradeService

USDC = SOLANA_MINTS.get_by_symbol("USDC")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LIMITS = SpecLimits(max_trade_usd=Decimal("25"))


def _ttl_spec(**overrides):
    data = make_spec(**overrides)
    data.pop("expires_at")
    data.setdefault("ttl_days", 7)
    return StrategySpec.model_validate(data)


class TestRelativeExpiry:
    def test_exactly_one_expiry(self):
        with pytest.raises(ValidationError, match="exatamente um"):
            StrategySpec.model_validate({**make_spec(), "ttl_days": 3})
        data = make_spec()
        data.pop("expires_at")
        with pytest.raises(ValidationError, match="exatamente um"):
            StrategySpec.model_validate(data)

    def test_ttl_is_checked_against_max_days(self):
        assert validate(_ttl_spec(ttl_days=30), LIMITS, T0) == []
        (error,) = validate(_ttl_spec(ttl_days=31), LIMITS, T0)
        assert error.path == "ttl_days"

    def test_ttl_counts_from_the_first_tick(self):
        spec = _ttl_spec(ttl_days=1, cooldown_minutes=0)
        strategy = SpecStrategy(spec)
        now = [T0]
        strategy.set_clock(lambda: now[0])
        strategy._warm = True  # sem aquecimento: só a validade importa

        assert strategy.on_market_refresh(Decimal("50"), Decimal("100"), None)
        now[0] = T0 + timedelta(days=1, seconds=1)
        assert strategy.on_market_refresh(Decimal("50"), Decimal("100"), None) is None

    def test_the_shipped_example_validates(self):
        example = PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json"
        spec = StrategySpec.model_validate_json(example.read_text(encoding="utf-8"))
        assert validate(spec, LIMITS) == []


def _service(tmp_path, price="100"):
    client = ReplayQuoteClient(USDC, Decimal(0))
    client.tick = Tick(T0, Decimal(price))
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    gateway = TradeGateway(
        open_ledger(),
        Policy(max_trade_usd=Decimal(1000)),
        False,
    )
    provider = paper_provider(wallet, jupiter_client=client)
    return TradeService(provider, gateway, mode="paper", clock=lambda: T0), client


class TestMaxLoss:
    async def test_a_loss_past_the_limit_retires_the_bucket(self, tmp_path):
        service, client = _service(tmp_path)
        await service.open_bucket(
            "s", USDC.mint, JUP.mint, budget_usd=Decimal(50), max_loss_usd=Decimal(5)
        )
        bought = await service.submit_order(
            "s", OrderRequest(OrderSide.BUY, Decimal("0.2"), Decimal("100"))
        )
        assert bought.order is not None
        client.tick = Tick(T0, Decimal("60"))  # -40%: perde 8 USD
        await service.submit_order(
            "s", OrderRequest(OrderSide.SELL, bought.order.quantity, Decimal("60"))
        )

        snapshot = await service.get_bucket("s")
        assert snapshot.status == BucketStatus.RETIRING
        again = await service.submit_order(
            "s", OrderRequest(OrderSide.BUY, Decimal("0.1"), Decimal("60"))
        )
        assert again.status == ReplyStatus.REJECTED

    async def test_a_restored_bucket_past_the_limit_starts_retiring(self, tmp_path):
        service, client = _service(tmp_path)
        await service.open_bucket("s", USDC.mint, JUP.mint)
        bought = await service.submit_order(
            "s", OrderRequest(OrderSide.BUY, Decimal("0.2"), Decimal("100"))
        )
        assert bought.order is not None
        client.tick = Tick(T0, Decimal("60"))
        await service.submit_order(
            "s", OrderRequest(OrderSide.SELL, bought.order.quantity, Decimal("60"))
        )

        restarted = TradeService(
            service.provider, service.gateway, mode="paper", clock=lambda: T0
        )
        await restarted.open_bucket("s", USDC.mint, JUP.mint, max_loss_usd=Decimal(5))

        assert (await restarted.get_bucket("s")).status == BucketStatus.RETIRING


class TestRunCommand:
    def _run(self, tmp_path, spec):
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        with mock.patch("trader.cli.bot.AsyncWebsocketTradingBot") as bot_cls:
            result = CliRunner().invoke(main_module.app, ["run", "paper", str(path)])
        return result, bot_cls

    def test_an_invalid_spec_does_not_start(self, tmp_path):
        result, bot_cls = self._run(tmp_path, make_spec())  # expira em 2099

        assert result.exit_code != 0
        assert "spec inválida para paper" in result.output
        bot_cls.assert_not_called()

    def test_a_valid_spec_runs_in_its_own_capped_bucket(self, tmp_path):
        spec = make_spec()
        spec.pop("expires_at")
        spec["ttl_days"] = 7

        result, bot_cls = self._run(tmp_path, spec)

        assert result.exit_code == 0, result.output
        trader = bot_cls.call_args.args[0].trader
        spec_id = StrategySpec.model_validate(spec).spec_id()
        assert trader.name == f"strategy:{spec_id}"
        assert trader.budget_usd == Decimal("50")
        assert trader.max_loss_usd == Decimal("10")
