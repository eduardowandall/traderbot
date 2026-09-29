from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

import main as main_module
from main import __parse_kwargs, _get_notification_svc, _get_strategy_obj
from trader.ledger import Ledger, ledger_path
from trader.models import SOLANA_MINTS, SwapResult
from trader.models.intent import IntentStatus
from trader.models.mode import RunningMode
from trader.notification.notification_service import (
    NullNotificationService,
    TelegramNotificationService,
)
from trader.paper import SimulatedExecutor
from trader.paths import policy_file
from trader.strategies_registry import NotImplementedStrategy
from trader.trading_strategy import (
    RandomStrategy,
    StrategyComposer,
    TargetValueStrategy,
)


def test_parse_kwargs_key_values():
    assert __parse_kwargs(["sell_chance=20", "buy_chance=40"]) == {
        "sell_chance": "20",
        "buy_chance": "40",
    }


def test_parse_kwargs_bare_flag():
    assert __parse_kwargs(["dry_run"]) == {"dry_run": True}


def test_get_strategy_obj_with_args():
    strategy = _get_strategy_obj("random", "sell_chance=20 buy_chance=40")
    assert isinstance(strategy, RandomStrategy)
    assert strategy.sell_chance == "20"
    assert strategy.buy_chance == "40"


def test_get_strategy_obj_no_args_uses_defaults():
    strategy = _get_strategy_obj("composer", None)
    assert isinstance(strategy, StrategyComposer)


def test_get_strategy_obj_target_value_with_args():
    strategy = _get_strategy_obj(
        "target_value", "target_buy_price=10.0 target_profit_percent=1.0"
    )
    assert isinstance(strategy, TargetValueStrategy)
    assert strategy.target_buy_price == Decimal("10.0")


def test_get_strategy_obj_unknown_strategy_raises():
    with pytest.raises(NotImplementedStrategy):
        _get_strategy_obj("does_not_exist", None)


def test_run_composer_without_strategy_args():
    mock_bot = mock.Mock()
    with (
        mock.patch("main.AsyncWebsocketTradingBot", return_value=mock_bot) as bot_cls,
        mock.patch(
            "trader.wiring.AsyncJupiterProvider.on_chain", return_value=mock.Mock()
        ),
        mock.patch("trader.wiring.keypair_from_env", return_value=mock.Mock()),
    ):
        result = CliRunner().invoke(
            main_module.app, ["run", "dry", "SOL-USDC", "composer"]
        )

    assert result.exit_code == 0
    mock_bot.run.assert_called_once()
    config = bot_cls.call_args.args[0]
    assert isinstance(config.strategy, StrategyComposer)
    assert config.trader.service.mode == "dry"


def test_run_random_with_strategy_args():
    mock_bot = mock.Mock()
    with (
        mock.patch("main.AsyncWebsocketTradingBot", return_value=mock_bot) as bot_cls,
        mock.patch(
            "trader.wiring.AsyncJupiterProvider.on_chain", return_value=mock.Mock()
        ),
        mock.patch("trader.wiring.keypair_from_env", return_value=mock.Mock()),
    ):
        result = CliRunner().invoke(
            main_module.app,
            ["run", "dry", "SOL-USDC", "random", "sell_chance=20 buy_chance=40"],
        )

    assert result.exit_code == 0
    config = bot_cls.call_args.args[0]
    assert isinstance(config.strategy, RandomStrategy)
    assert config.strategy.sell_chance == "20"
    assert config.strategy.buy_chance == "40"


def test_get_notification_svc_null():
    svc = _get_notification_svc("null", None)
    assert isinstance(svc, NullNotificationService)


def test_get_notification_svc_telegram_without_args_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="chat_id e token"):
        _get_notification_svc("telegram", None)


def test_get_notification_svc_telegram_with_args():
    svc = _get_notification_svc("telegram", "chat_id=123 token=abc")
    assert isinstance(svc, TelegramNotificationService)
    assert svc.chat_id == "123"
    assert svc.token == "abc"


PERMISSIVE_POLICY = """
[trading]
real_trading_enabled = true
allow_unknown_notional = true
"""


def _invoke_swap(argv, policy: str | None = PERMISSIVE_POLICY):
    # policy_file() aponta para o diretório do teste (fixture isolated_workdir)
    if policy is not None:
        policy_file().write_text(policy, encoding="utf-8")
    mock_provider = mock.Mock()
    mock_provider.swap = mock.AsyncMock(
        return_value=SwapResult("sig123", "in", "out", 1, 1)
    )
    # o CLI executa via swap_with_details; os testes checam o mesmo mock
    mock_provider.swap_with_details = mock_provider.swap
    mock_provider.fetch_swap_costs = mock.AsyncMock(return_value=None)
    mock_provider.aclose = mock.AsyncMock()
    with (
        mock.patch(
            "trader.wiring.AsyncJupiterProvider.on_chain", return_value=mock_provider
        ) as provider_cls,
        mock.patch("trader.wiring.keypair_from_env", return_value=mock.Mock()),
    ):
        result = CliRunner().invoke(main_module.app, ["swap", *argv])
    return result, mock_provider, provider_cls


def test_swap_command_dry_with_slippage():
    result, mock_provider, provider_cls = _invoke_swap(
        ["dry", "SOL", "USDC", "0.5", "--slippage-bps", "100"]
    )

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == "Swap executado: sig123"
    provider_cls.assert_called_once_with(
        keypair=mock.ANY, is_dryrun=True, max_price_impact_pct=Decimal("1")
    )
    mock_provider.aclose.assert_awaited_once()
    mock_provider.swap.assert_awaited_once_with(
        SOLANA_MINTS.get_by_symbol("SOL").mint,
        SOLANA_MINTS.get_by_symbol("USDC").mint,
        500000000,
        100,
    )


def test_swap_command_real_mode_default_slippage():
    result, mock_provider, provider_cls = _invoke_swap(["real", "JUP", "USDC", "10"])

    assert result.exit_code == 0
    provider_cls.assert_called_once_with(
        keypair=mock.ANY, is_dryrun=False, max_price_impact_pct=Decimal("1")
    )
    mock_provider.swap.assert_awaited_once_with(
        SOLANA_MINTS.get_by_symbol("JUP").mint,
        SOLANA_MINTS.get_by_symbol("USDC").mint,
        10_000_000,
        50,
    )


def test_swap_command_unknown_symbol_fails():
    result, mock_provider, _ = _invoke_swap(["dry", "FOO", "USDC", "1"])

    assert result.exit_code != 0
    mock_provider.swap.assert_not_awaited()


def test_swap_command_zero_quantity_fails():
    result, mock_provider, _ = _invoke_swap(["dry", "SOL", "USDC", "0"])

    assert result.exit_code != 0
    mock_provider.swap.assert_not_awaited()


def test_start_command_was_removed():
    result = CliRunner().invoke(main_module.app, ["start"])
    assert result.exit_code != 0


def test_swap_command_parses_quantity_as_exact_decimal():
    result, mock_provider, _ = _invoke_swap(["dry", "USDC", "SOL", "0.1000001"])

    assert result.exit_code == 0
    mock_provider.swap.assert_awaited_once_with(
        SOLANA_MINTS.get_by_symbol("USDC").mint,
        SOLANA_MINTS.get_by_symbol("SOL").mint,
        100000,
        50,
    )


@pytest.mark.parametrize("quantity", ["abc", "nan", "inf", "-1"])
def test_swap_command_rejects_invalid_quantity(quantity):
    result, mock_provider, _ = _invoke_swap(["dry", "SOL", "USDC", "--", quantity])

    assert result.exit_code != 0
    mock_provider.swap.assert_not_awaited()


def test_swap_command_rejects_slippage_above_10_percent():
    result, mock_provider, _ = _invoke_swap(
        ["dry", "SOL", "USDC", "1", "--slippage-bps", "1001"]
    )

    assert result.exit_code != 0
    mock_provider.swap.assert_not_awaited()


def test_swap_command_forwards_max_price_impact():
    result, _, provider_cls = _invoke_swap(
        ["dry", "SOL", "USDC", "1", "--max-price-impact", "0.3"]
    )

    assert result.exit_code == 0
    assert provider_cls.call_args.kwargs["max_price_impact_pct"] == Decimal("0.3")


def test_get_notification_svc_telegram_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")

    svc = _get_notification_svc("telegram", None)

    assert isinstance(svc, TelegramNotificationService)
    assert svc.chat_id == "999"
    assert svc.token == "env-token"


def test_get_notification_svc_telegram_missing_token_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")

    with pytest.raises(ValueError, match="chat_id e token"):
        _get_notification_svc("telegram", None)


def test_swap_is_recorded_in_the_ledger():
    result, _, _ = _invoke_swap(["dry", "USDC", "SOL", "10"])

    assert result.exit_code == 0
    ledger = Ledger(ledger_path("dry"))
    records = ledger.list_intents()
    ledger.close()
    assert len(records) == 1
    assert records[0].status == IntentStatus.EXECUTED
    assert records[0].signature == "sig123"
    assert records[0].intent.notional_usd == Decimal("10")
    assert records[0].intent.account == "dry:manual"
    assert records[0].order_json  # o fill foi gravado, não só a assinatura
    assert "gasto" in result.stdout
    assert "custos: desconhecidos" in result.stdout


def test_default_policy_blocks_real_mode():
    result, mock_provider, _ = _invoke_swap(["real", "USDC", "SOL", "1"], policy=None)

    assert result.exit_code != 0
    assert "real_trading_enabled" in result.stderr
    mock_provider.swap.assert_not_awaited()


def test_default_policy_blocks_unknown_notional_and_large_trades():
    result, mock_provider, _ = _invoke_swap(["dry", "SOL", "USDC", "1"], policy=None)
    assert "allow_unknown_notional" in result.stderr

    result, mock_provider, _ = _invoke_swap(["dry", "USDC", "SOL", "26"], policy=None)
    assert "acima do limite" in result.stderr
    mock_provider.swap.assert_not_awaited()


def test_swap_with_same_idempotency_key_runs_once():
    argv = ["dry", "USDC", "SOL", "1", "--idempotency-key", "k1"]
    first, provider_1, _ = _invoke_swap(argv)
    second, provider_2, _ = _invoke_swap(argv)

    assert first.exit_code == 0
    provider_1.swap.assert_awaited_once()
    assert second.exit_code != 0
    assert "duplicada" in second.stderr
    provider_2.swap.assert_not_awaited()


def test_halt_blocks_swaps_until_resume():
    assert CliRunner().invoke(main_module.app, ["halt", "teste"]).exit_code == 0
    blocked, provider, _ = _invoke_swap(["dry", "USDC", "SOL", "1"])
    assert "kill switch" in blocked.stderr
    provider.swap.assert_not_awaited()

    assert CliRunner().invoke(main_module.app, ["resume", "dry"]).exit_code == 0
    ok, provider, _ = _invoke_swap(["dry", "USDC", "SOL", "1"])
    assert ok.exit_code == 0


def test_ledger_commands():
    _invoke_swap(["dry", "USDC", "SOL", "1"])
    runner = CliRunner()

    listed = runner.invoke(main_module.app, ["ledger", "list", "dry"])
    assert listed.exit_code == 0
    assert "executed" in listed.stdout
    assert "USDC -> SOL" in listed.stdout

    verified = runner.invoke(main_module.app, ["ledger", "verify", "dry"])
    assert verified.exit_code == 0
    assert "íntegro" in verified.stdout


def test_paper_reset_and_balance():
    runner = CliRunner()
    reset = runner.invoke(main_module.app, ["paper", "reset", "USDC=50 JUP=10"])
    assert reset.exit_code == 0

    balance = runner.invoke(main_module.app, ["paper", "balance"])
    assert "USDC 50" in balance.stdout
    assert "JUP 10" in balance.stdout


def test_paper_reset_rejects_bad_spec():
    result = CliRunner().invoke(main_module.app, ["paper", "reset", "FOO=1"])
    assert result.exit_code != 0


def test_run_paper_needs_no_private_key(monkeypatch):
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    mock_bot = mock.Mock()
    with mock.patch("main.AsyncWebsocketTradingBot", return_value=mock_bot) as bot_cls:
        result = CliRunner().invoke(
            main_module.app,
            ["run", "paper", "SOL-USDC", "random", "buy_chance=1 sell_chance=1"],
        )

    assert result.exit_code == 0, result.output
    config = bot_cls.call_args.args[0]
    assert config.trader.service.mode == "paper"
    executor = config.trader.service.provider.executor
    assert isinstance(executor, SimulatedExecutor)
    assert executor.wallet.balance(SOLANA_MINTS.get_by_symbol("USDC").mint) == Decimal(
        "100"
    )
    # vai para o stderr: o stdout fica reservado para saídas `--json`
    assert "Carteira paper criada" in result.stderr
    assert "Carteira paper criada" not in result.stdout


def test_run_record_ticks_passes_recorder(tmp_path):
    mock_bot = mock.Mock()
    with (
        mock.patch("main.AsyncWebsocketTradingBot", return_value=mock_bot) as bot_cls,
    ):
        result = CliRunner().invoke(
            main_module.app,
            [
                "run",
                "paper",
                "SOL-USDC",
                "random",
                "buy_chance=1 sell_chance=1",
                "--record-ticks",
                "ticks/sol.csv",
            ],
        )
    assert result.exit_code == 0, result.output
    on_tick = bot_cls.call_args.args[0].on_tick
    assert on_tick.__self__.path == Path("ticks/sol.csv")


def test_backtest_from_ticks_file():
    lines = [f"2026-09-01T12:{m:02d}:00,{100 + m}" for m in range(30)]
    Path("ticks.csv").write_text(chr(10).join(lines), encoding="utf-8")

    result = CliRunner().invoke(
        main_module.app,
        [
            "backtest",
            "SOL-USDC",
            "random",
            "sell_chance=20 buy_chance=20",
            "--ticks",
            "ticks.csv",
            "--seed",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Backtest SOL-USDC: 30 ticks" in result.stdout
    again = CliRunner().invoke(
        main_module.app,
        [
            "backtest",
            "SOL-USDC",
            "random",
            "sell_chance=20 buy_chance=20",
            "--ticks",
            "ticks.csv",
            "--seed",
            "1",
        ],
    )
    assert again.stdout == result.stdout


def test_backtest_requires_a_source():
    result = CliRunner().invoke(main_module.app, ["backtest", "SOL-USDC", "random"])
    assert result.exit_code != 0


def test_paper_uses_its_own_policy_section():
    policy_file().write_text("[paper.limits]\nmax_trade_usd = 1000\n", encoding="utf-8")
    paper = main_module.build_gateway(RunningMode.PAPER)
    dry = main_module.build_gateway(RunningMode.DRY)
    try:
        assert paper.policy.max_trade_usd == 1000
        assert dry.policy.max_trade_usd == 25
    finally:
        paper.ledger.close()
        dry.ledger.close()
