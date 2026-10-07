"""Monta os dados de uma instrução Anchor pela IDL (A11a): o espelho do `idl.py`.

Uma instrução Anchor é o discriminador (`sha256("global:<nome_em_snake>")[:8]`)
seguido dos argumentos em Borsh, na ordem da IDL. Só monta bytes: as contas,
a transação e o envio são de quem executa (`trade/venues/jupiter_perps/`).
"""

import hashlib
import re
import struct
from collections.abc import Callable
from typing import Any

from solders.pubkey import Pubkey

from trader.execution.market.perps.idl import (
    PRIMITIVES,
    AccountFormatError,
    defined_type,
    instructions,
)

Writer = Callable[[Any, Any], bytes]


def instruction_discriminator(name: str) -> bytes:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return hashlib.sha256(f"global:{snake}".encode()).digest()[:8]


def encode_instruction(name: str, args: dict[str, Any]) -> bytes:
    """Os dados da instrução `name` (camelCase) com os argumentos `args`."""
    spec = instructions()[name]
    body = b"".join(_write(a["type"], args[a["name"]]) for a in spec["args"])
    return instruction_discriminator(name) + body


def instruction_accounts(name: str) -> list[dict]:
    """As contas da instrução, na ordem da IDL (`name`, `isMut`, `isSigner`)."""
    return instructions()[name]["accounts"]


def _write(spec: Any, value: Any) -> bytes:
    if isinstance(spec, str):
        return _scalar(spec, value)
    key = spec.get("kind") or next(iter(spec))
    writer = _COMPOUND.get(key)
    if writer is None:
        raise AccountFormatError(f"tipo da IDL desconhecido: {spec}")
    return writer(spec, value)


def _scalar(name: str, value: Any) -> bytes:
    if name in PRIMITIVES:
        return struct.pack(PRIMITIVES[name], value)
    writer = _SPECIAL.get(name)
    if writer is None:
        raise AccountFormatError(f"tipo da IDL desconhecido: {name}")
    return writer(name, value)


def _int128(name: str, value: int) -> bytes:
    return value.to_bytes(16, "little", signed=name == "i128")


def _pubkey(name: str, value: Pubkey) -> bytes:
    return bytes(value)


def _string(name: str, value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def _vec(spec: dict, value: list) -> bytes:
    return struct.pack("<I", len(value)) + b"".join(
        _write(spec["vec"], v) for v in value
    )


def _array(spec: dict, value: list) -> bytes:
    inner, size = spec["array"]
    if len(value) != size:
        raise AccountFormatError(f"array de {size}, veio {len(value)}")
    return b"".join(_write(inner, v) for v in value)


def _option(spec: dict, value: Any) -> bytes:
    return b"\x00" if value is None else b"\x01" + _write(spec["option"], value)


def _defined(spec: dict, value: Any) -> bytes:
    return _write(defined_type(spec["defined"]), value)


def _struct(spec: dict, value: dict) -> bytes:
    missing = [f["name"] for f in spec["fields"] if f["name"] not in value]
    if missing:
        raise AccountFormatError(f"campos faltando: {missing}")
    return b"".join(_write(f["type"], value[f["name"]]) for f in spec["fields"])


def _enum(spec: dict, value: str) -> bytes:
    names = [v["name"] for v in spec["variants"]]
    if value not in names:
        raise AccountFormatError(f"variante desconhecida: {value} (de {names})")
    if spec["variants"][names.index(value)].get("fields"):
        raise AccountFormatError(f"variante com campos: {value}")
    return bytes([names.index(value)])


_COMPOUND: dict[str, Writer] = {
    "vec": _vec,
    "array": _array,
    "option": _option,
    "defined": _defined,
    "struct": _struct,
    "enum": _enum,
}
_SPECIAL: dict[str, Callable[[str, Any], bytes]] = {
    "u128": _int128,
    "i128": _int128,
    "publicKey": _pubkey,
    "string": _string,
}
