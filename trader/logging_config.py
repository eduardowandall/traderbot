"""Logging: console (stderr, filtrado) e um arquivo por processo em `.logs/`.

Cada linha do arquivo leva o nome do bot (`[%(botname)s]`), vindo da
ContextVar `botname` que o bot define por task: vários bots num processo
continuam distinguíveis num arquivo só.
"""

import logging
import logging.config
import os
import re
import time
from contextvars import ContextVar

from rich.console import Console
from rich.logging import RichHandler

botname: ContextVar[str | None] = ContextVar("botname", default=None)

LOG_DIR = ".logs"


def stderr_rich_handler(**kwargs) -> RichHandler:
    """RichHandler no stderr: o stdout fica livre para as saídas `--json`."""
    return RichHandler(console=Console(stderr=True), **kwargs)


def file_handler(**kwargs) -> logging.FileHandler:
    """`.logs/trader-<timestamp>.log`, aberto só no primeiro registro."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"trader-{int(time.time())}.log")
    return logging.FileHandler(path, encoding="utf-8", delay=True, **kwargs)


# segredos que podem aparecer em URLs: a api-key da HELIUS_RPC_URL e o token
# do bot do Telegram (api.telegram.org/bot<token>/...)
_SECRETS = (
    (re.compile(r"(api[-_]key=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(/bot)\d+:[\w-]+"), r"\1***"),
)


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Mascara segredos na linha inteira, inclusive tracebacks e notas."""

    def format(self, record):
        return redact(super().format(record))

    def formatException(self, ei):
        return redact(super().formatException(ei))


class BotNameFilter(logging.Filter):
    """Põe o nome do bot da task atual em `record.botname` ("-" fora de bots)."""

    def filter(self, record):
        record.botname = botname.get(None) or "-"
        return True


class ConsoleFilter(logging.Filter):
    # sinais das estratégias (legadas e specs) aparecem no console em DEBUG
    ALLOWED_LOGGERS = [
        "bot",
        "trader.trading_strategy",
        "trader.strategy_spec.strategy",
    ]

    def filter(self, record):
        if record.name in ConsoleFilter.ALLOWED_LOGGERS:
            return True
        return record.levelno >= logging.WARNING


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "consolefilter": {"()": ConsoleFilter},
        "botname": {"()": BotNameFilter},
    },
    "formatters": {
        "default": {
            "()": RedactingFormatter,
            "format": (
                "%(asctime)s %(levelname)s [%(botname)s] "
                "%(name)s.%(funcName)s %(message)s"
            ),
        },
        "console": {
            "()": RedactingFormatter,
            "format": "%(message)s",
        },
    },
    "handlers": {
        "console": {
            "()": stderr_rich_handler,
            "formatter": "console",
            "level": "DEBUG",
            "filters": ["consolefilter"],
        },
        "file": {
            "()": file_handler,
            "formatter": "default",
            "level": "DEBUG",
            "filters": ["botname"],
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "DEBUG",
    },
    "loggers": {
        "httpcore": {
            "level": "ERROR",
        },
        # httpx loga cada URL em INFO: a HELIUS_RPC_URL contém a api-key e a
        # URL do Telegram contém o token do bot. O solana-py usa o fork
        # `httpx2`/`httpcore2`, que loga igual (vazou a api-key em .logs/)
        "httpx": {"level": "WARNING"},
        "httpx2": {"level": "WARNING"},
        "httpcore2": {"level": "ERROR"},
    },
}


def setup_logging():
    logging.config.dictConfig(LOGGING)
