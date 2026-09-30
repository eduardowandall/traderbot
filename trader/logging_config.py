"""Logging: console (stderr, filtrado) e um arquivo por processo em `.logs/`.

Cada linha do arquivo leva o nome do bot (`[%(botname)s]`), vindo da
ContextVar `botname` que o bot define por task: vários bots num processo
continuam distinguíveis num arquivo só.
"""

import logging
import logging.config
import os
import re
import sys
import time
import traceback
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from rich.console import Console
from rich.logging import RichHandler

from trader.paths import logs_dir

botname: ContextVar[str | None] = ContextVar("botname", default=None)

# rotação por processo: 10 MB x 5 arquivos (antes crescia sem limite)
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
# cada comando cria um arquivo: os mais velhos que isso são apagados
LOG_RETENTION_DAYS = 14


def stderr_rich_handler(**kwargs) -> RichHandler:
    """RichHandler no stderr: o stdout fica livre para as saídas `--json`."""
    return RichHandler(console=Console(stderr=True), **kwargs)


def file_handler(**kwargs) -> logging.Handler:
    """`logs_dir()/trader-<timestamp>.log` com rotação, aberto no 1º registro."""
    folder = logs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    prune_logs(folder)
    return RotatingFileHandler(
        # o pid evita dois processos do mesmo segundo no mesmo arquivo
        folder / f"trader-{int(time.time())}-{os.getpid()}.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
        delay=True,
        **kwargs,
    )


def prune_logs(folder, days: float = LOG_RETENTION_DAYS) -> int:
    """Apaga `trader-*.log*` mais velhos que `days`; devolve quantos."""
    cutoff = time.time() - days * 86400
    removed = 0
    for path in folder.glob("trader-*.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass  # em uso por outro processo (Windows): fica para a próxima
    return removed


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
    # sinais das estratégias (specs) aparecem no console em DEBUG
    ALLOWED_LOGGERS = [
        "bot",
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


def redacted_excepthook(exc_type, exc, tb) -> None:
    """Traceback de um erro que escapou, com os segredos mascarados."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(redact(text))
