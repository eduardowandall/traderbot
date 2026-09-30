"""Ajudantes compartilhados pelos comandos do dono (argumentos, a spec)."""

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer

from trader.notification.notification_service import (
    NullNotificationService,
    TelegramNotificationService,
)
from trader.strategy_spec.strategy import SpecStrategy


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


def load_spec_strategy(spec_file: Path, seed: str | None = None) -> SpecStrategy:
    """A estratégia de um `run`/`backtest`: sempre uma spec (o par vem dela)."""
    try:
        strategy = SpecStrategy.from_file(str(spec_file))
    except (OSError, ValueError) as ex:
        raise typer.BadParameter(f"spec {spec_file}: {ex}") from ex
    if seed is not None:
        strategy.seed(seed)
    return strategy


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
