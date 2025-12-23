import typer

import logging_config  # pyright: ignore[reportMissingImports]
from trader import get_strategy_cls
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.models.bot_config import (
    RunningMode,
    create_bot_config,
    get_keypair_from_env,
)
from trader.notification.notification_service import (
    NullNotificationService,
    TelegramNotificationService,
)
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.trading_strategy import StrategyComposer

app = typer.Typer()


@app.command()
def run(
    mode: RunningMode = typer.Argument(
        RunningMode.REAL, help="Modo de execucão do bot."
    ),
    currency: str = typer.Argument(
        "SOL-USDC", help="The trading symbol tuple (ex: SOL-USDC)"
    ),
    strategy: str = typer.Argument(..., help="The trading strategy to use"),
    notification_service: str = typer.Option(
        "null", help="Serviço de notificação: 'telegram' ou 'null'"
    ),
    notification_args: str | None = typer.Option(
        None, help="Argumentos do serviço de notificação"
    ),
    strategy_args: str | None = typer.Argument(None, help="Argumentos da estratégia"),
):
    """
    Executa o bot em modo produção.

    Exemplos:
        # Jupiter
        uv run python main.py run SOL-USDC dynamic_target 60 --api jupiter --wallet-key=WALLET_PUBLIC_KEY 'ema_period=20'
    """

    provider = AsyncJupiterProvider(
        keypair=get_keypair_from_env(), is_dryrun=(mode == RunningMode.DRY)
    )
    strategy_obj = _get_strategy_obj(strategy, strategy_args)
    notification_svc = _get_notification_svc(notification_service, notification_args)

    config = create_bot_config(
        mode,
        f"run-{strategy}",
        currency,
        provider,
        strategy_obj,
        notification_svc,
    )

    bot = AsyncWebsocketTradingBot(config)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()


@app.command()
def start(
    mode: RunningMode = typer.Argument(
        RunningMode.REAL, help="Modo de execucão do bot."
    ),
    symbol: str = typer.Argument("SOL-USDC", help="The trading symbol"),
):
    provider = AsyncJupiterProvider(
        keypair=get_keypair_from_env(), is_dryrun=(mode == RunningMode.DRY)
    )

    config = create_bot_config(
        mode,
        "my_config",
        symbol,
        provider,
        # RandomStrategy(sell_chance=20, buy_chance=50),
        StrategyComposer(sell_mode="any", buy_mode="all"),
        NullNotificationService(),
    )

    bot = AsyncWebsocketTradingBot(config)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()


def _get_strategy_obj(strategy: str, strategy_args: str | None = None):
    strategy_cls = get_strategy_cls(strategy)
    try:
        if strategy_args:
            args = __parse_kwargs(strategy_args.split())
        return strategy_cls(**args)
    except ValueError as ex:
        raise Exception(f"Erro ao configurar estratégia: {ex}") from ex


def _get_notification_svc(
    notification_service: str, notification_args: str | None = None
):
    if notification_service == "telegram":
        if not notification_args:
            raise ValueError(
                "Para notificação via Telegram, é necessário informar os argumentos chat_id e token"
            )
        args = __parse_kwargs(notification_args.split())
        return TelegramNotificationService(args["chat_id"], args["token"])
    return NullNotificationService()


def __parse_kwargs(argv: list[str]) -> dict[str, str]:
    kwargs = {}

    for arg in argv:
        key_value = arg.split("=", 1)
        if len(key_value) == 2:
            key, value = key_value
            kwargs[key] = value
        else:
            kwargs[key_value[0]] = True  # flag booleana

    return kwargs


if __name__ == "__main__":
    logging_config.setup_logging()
    app()
