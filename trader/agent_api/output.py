"""Saída JSON dos comandos de agente: sempre um único objeto no stdout.

Sucesso: `{"ok": true, ...}`. Erro: `{"ok": false, "errors": [{"path", "msg"}]}`
com código de saída 1. Decimais saem como string (sem perda de precisão) e o
texto é ASCII (`ensure_ascii`), então funciona no console cp1252 do Windows.
"""

import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import typer


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
