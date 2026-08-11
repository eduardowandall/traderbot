from decimal import Decimal
from unittest import mock

import pytest
from typer.testing import CliRunner

import main as main_module
from main import __parse_kwargs, _get_notification_svc, _get_strategy_obj
from trader import NotImplementedStrategy
from trader.models import SOLANA_MINTS
from trader.models.bot_config import RunningMode
from trader.notification.notification_service import (
    NullNotificationService,
    TelegramNotificationService,
)
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
        mock.patch("main.AsyncJupiterProvider", return_value=mock.Mock()),
        mock.patch("main.get_keypair_from_env", return_value=mock.Mock()),
        mock.patch(
            "trader.models.bot_config.get_keypair_from_env",
            return_value=mock.Mock(),
        ),
    ):
        result = CliRunner().invoke(
            main_module.app, ["run", "dry", "SOL-USDC", "composer"]
        )

    assert result.exit_code == 0
    mock_bot.run.assert_called_once()
    config = bot_cls.call_args.args[0]
    assert isinstance(config.strategy, StrategyComposer)
    assert config.mode == RunningMode.DRY


def test_run_random_with_strategy_args():
    mock_bot = mock.Mock()
    with (
        mock.patch("main.AsyncWebsocketTradingBot", return_value=mock_bot) as bot_cls,
        mock.patch("main.AsyncJupiterProvider", return_value=mock.Mock()),
        mock.patch("main.get_keypair_from_env", return_value=mock.Mock()),
        mock.patch(
            "trader.models.bot_config.get_keypair_from_env",
            return_value=mock.Mock(),
        ),
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


def test_get_notification_svc_telegram_without_args_raises():
    with pytest.raises(ValueError, match="chat_id e token"):
        _get_notification_svc("telegram", None)


def test_get_notification_svc_telegram_with_args():
    svc = _get_notification_svc("telegram", "chat_id=123 token=abc")
    assert isinstance(svc, TelegramNotificationService)
    assert svc.chat_id == "123"
    assert svc.token == "abc"


def _invoke_swap(argv):
    mock_provider = mock.Mock()
    mock_provider.swap = mock.AsyncMock(return_value="sig123")
    with (
        mock.patch(
            "main.AsyncJupiterProvider", return_value=mock_provider
        ) as provider_cls,
        mock.patch("main.get_keypair_from_env", return_value=mock.Mock()),
    ):
        result = CliRunner().invoke(main_module.app, ["swap", *argv])
    return result, mock_provider, provider_cls


def test_swap_command_dry_with_slippage():
    result, mock_provider, provider_cls = _invoke_swap(
        ["dry", "SOL", "USDC", "0.5", "--slippage-bps", "100"]
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "Swap executado: sig123"
    provider_cls.assert_called_once_with(keypair=mock.ANY, is_dryrun=True)
    mock_provider.swap.assert_awaited_once_with(
        SOLANA_MINTS.get_by_symbol("SOL").mint,
        SOLANA_MINTS.get_by_symbol("USDC").mint,
        500000000,
        100,
    )


def test_swap_command_real_mode_default_slippage():
    result, mock_provider, provider_cls = _invoke_swap(["real", "JUP", "USDC", "10"])

    assert result.exit_code == 0
    provider_cls.assert_called_once_with(keypair=mock.ANY, is_dryrun=False)
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
