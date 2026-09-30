"""Saída JSON do `backtest`: sempre um único objeto no stdout.

Sucesso: `{"ok": true, ...}`. Erro: `{"ok": false, "errors": [{"path", "msg"}]}`
com código de saída 1. Decimais saem como string (sem perda de precisão) e o
texto é ASCII (`ensure_ascii`), então funciona no console cp1252 do Windows.
"""

import dataclasses
import functools
import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import typer

from trader.strategy_spec.validate import SpecParseError


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        # notação simples e normalizada: "0", não "0E-26"; "1.5", não "1.50"
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"não serializável: {type(value).__name__}")


def dumps(payload: dict) -> str:
    return json.dumps(payload, default=_default, ensure_ascii=True, sort_keys=True)


def emit(payload: dict) -> None:
    typer.echo(dumps({"ok": True, **payload}))


def fail(errors: list, **extra) -> None:
    """Imprime os erros (e contexto, ex: `spec_id`) e encerra com código 1."""
    typer.echo(dumps({**extra, "ok": False, "errors": errors}))
    raise typer.Exit(1)


def _errors_of(ex: Exception) -> list[dict]:
    """O erro como lista `[{"path", "msg"}]` para o JSON."""
    if isinstance(ex, SpecParseError):
        return [{"path": e.path, "msg": e.msg} for e in ex.errors]
    if isinstance(ex, ValueError | OSError):
        return [{"path": "", "msg": str(ex)}]
    # rede, API, qualquer outra coisa: quem chama sempre recebe JSON
    return [{"path": "", "msg": f"{type(ex).__name__}: {ex}"}]


def json_errors(command: Callable[..., None]) -> Callable[..., None]:
    """Qualquer erro vira `{"ok": false, ...}` em vez de traceback."""

    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        try:
            command(*args, **kwargs)
        except typer.Exit:
            raise
        except Exception as ex:
            fail(_errors_of(ex))

    return wrapper
