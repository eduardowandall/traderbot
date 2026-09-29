"""Ajudantes compartilhados pelos comandos do dono (argumentos, estratégias)."""

import os
from decimal import Decimal, InvalidOperation

import typer

from trader.notification.notification_service import (
    NullNotificationService,
    TelegramNotificationService,
)
from trader.strategies_registry import get_strategy_factory


def warn(message: str) -> None:
    # stderr: não pode sujar o stdout dos comandos `--json`
    typer.echo(message, err=True)


def parse_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as ex:
        raise typer.BadParameter(f"{name} inválido: {value!r}") from ex
    if not parsed.is_finite():
        raise typer.BadParameter(f"{name} inválido: {value!r}")
    return parsed


def parse_kwargs(argv: list[str]) -> dict[str, str]:
    kwargs = {}

    for arg in argv:
        key_value = arg.split("=", 1)
        if len(key_value) == 2:
            key, value = key_value
            kwargs[key] = value
        else:
            kwargs[key_value[0]] = True  # flag booleana

    return kwargs


def get_strategy_obj(strategy: str, strategy_args: str | None = None):
    strategy_cls = get_strategy_factory(strategy)
    try:
        args = parse_kwargs(strategy_args.split()) if strategy_args else {}
        return strategy_cls(**args)
    except ValueError as ex:
        raise Exception(f"Erro ao configurar estratégia: {ex}") from ex


def check_symbol(strategy_obj, symbol: str) -> None:
    # estratégias com par próprio (specs) só rodam no par delas
    expected = getattr(strategy_obj, "symbol", None)
    if expected and expected != symbol:
        raise typer.BadParameter(f"a estratégia é para {expected}, não {symbol}")


def get_notification_svc(
    notification_service: str, notification_args: str | None = None
):
    if notification_service == "telegram":
        args = parse_kwargs(notification_args.split()) if notification_args else {}
        # preferir variáveis de ambiente: argumentos de CLI ficam visíveis na
        # lista de processos e no histórico do shell
        chat_id = args.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")
        token = args.get("token") or os.getenv("TELEGRAM_BOT_TOKEN")
        if not chat_id or not token:
            raise ValueError(
                "Para notificação via Telegram, é necessário informar chat_id e token "
                "(TELEGRAM_CHAT_ID / TELEGRAM_BOT_TOKEN)"
            )
        return TelegramNotificationService(str(chat_id), str(token))
    return NullNotificationService()
