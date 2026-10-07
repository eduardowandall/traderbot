"""Lê contas Anchor (Borsh) pela IDL do programa (A10): Jupiter Perps e Doves.

Uma conta Anchor é o discriminador (`sha256("account:<Nome>")[:8]`) seguido
dos campos na ordem da IDL, em little-endian, sem alinhamento. A IDL fica em
`perpetuals_idl.json` (as contas lidas aqui, as instruções de pedido e os
tipos delas). Este módulo só lê; montar instruções é o `encode.py` (A11a).
"""

import hashlib
import json
import struct
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey

IDL_FILE = Path(__file__).with_name("perpetuals_idl.json")

PRIMITIVES = {
    "u8": "<B",
    "i8": "<b",
    "u16": "<H",
    "i16": "<h",
    "u32": "<I",
    "i32": "<i",
    "u64": "<Q",
    "i64": "<q",
    "f32": "<f",
    "f64": "<d",
    "bool": "<?",
}


class AccountFormatError(ValueError):
    """Os bytes não são da conta pedida (discriminador) ou acabaram antes."""


@cache
def _raw_idl() -> dict:
    return json.loads(IDL_FILE.read_text(encoding="utf-8"))


@cache
def _idl() -> tuple[dict, dict]:
    raw = _raw_idl()
    accounts = {a["name"]: a["type"] for a in raw["accounts"]}
    types = {t["name"]: t["type"] for t in raw["types"]}
    return accounts, types


@cache
def instructions() -> dict[str, dict]:
    """As instruções vendorizadas pelo nome (camelCase, como na IDL)."""
    return {i["name"]: i for i in _raw_idl()["instructions"]}


def defined_type(name: str) -> dict:
    return _idl()[1][name]


def discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


def decode_account(name: str, data: bytes) -> dict[str, Any]:
    """Os campos da conta `name` (como na IDL, em camelCase)."""
    if data[:8] != discriminator(name):
        raise AccountFormatError(f"a conta não é um {name}")
    accounts, _ = _idl()
    try:
        value, _ = _read(accounts[name], data, 8)
    except (struct.error, IndexError) as ex:
        raise AccountFormatError(f"{name} curto demais: {ex}") from ex
    return value


Reader = Callable[[Any, bytes, int], tuple[Any, int]]


def _read(spec: Any, data: bytes, offset: int) -> tuple[Any, int]:
    """Um valor do tipo `spec` (da IDL) em `offset`: (valor, próximo offset)."""
    if isinstance(spec, str):
        return _scalar(spec, data, offset)
    # um tipo definido traz `kind`; um composto é um dict de uma chave só
    key = spec.get("kind") or next(iter(spec))
    reader = _COMPOUND.get(key)
    if reader is None:
        raise AccountFormatError(f"tipo da IDL desconhecido: {spec}")
    return reader(spec, data, offset)


def _scalar(name: str, data: bytes, offset: int) -> tuple[Any, int]:
    if name in PRIMITIVES:
        fmt = PRIMITIVES[name]
        return struct.unpack_from(fmt, data, offset)[0], offset + struct.calcsize(fmt)
    reader = _SPECIAL.get(name)
    if reader is None:
        raise AccountFormatError(f"tipo da IDL desconhecido: {name}")
    return reader(name, data, offset)


def _int128(name: str, data: bytes, offset: int) -> tuple[int, int]:
    raw = data[offset : offset + 16]
    if len(raw) < 16:
        raise IndexError(name)
    return int.from_bytes(raw, "little", signed=name == "i128"), offset + 16


def _pubkey(name: str, data: bytes, offset: int) -> tuple[Pubkey, int]:
    raw = bytes(data[offset : offset + 32])
    if len(raw) < 32:  # o `solders` entra em pânico, não levanta
        raise IndexError(name)
    return Pubkey.from_bytes(raw), offset + 32


def _string(name: str, data: bytes, offset: int) -> tuple[str, int]:
    size = struct.unpack_from("<I", data, offset)[0]
    start = offset + 4
    return data[start : start + size].decode(), start + size


def _vec(spec: dict, data: bytes, offset: int) -> tuple[list, int]:
    size = struct.unpack_from("<I", data, offset)[0]
    return _many(spec["vec"], size, data, offset + 4)


def _array(spec: dict, data: bytes, offset: int) -> tuple[list, int]:
    inner, size = spec["array"]
    return _many(inner, size, data, offset)


def _many(inner: Any, size: int, data: bytes, offset: int) -> tuple[list, int]:
    values = []
    for _ in range(size):
        value, offset = _read(inner, data, offset)
        values.append(value)
    return values, offset


def _option(spec: dict, data: bytes, offset: int) -> tuple[Any, int]:
    if not data[offset]:
        return None, offset + 1
    return _read(spec["option"], data, offset + 1)


def _defined(spec: dict, data: bytes, offset: int) -> tuple[Any, int]:
    _, types = _idl()
    return _read(types[spec["defined"]], data, offset)


def _struct(spec: dict, data: bytes, offset: int) -> tuple[dict, int]:
    values = {}
    for field in spec["fields"]:
        values[field["name"]], offset = _read(field["type"], data, offset)
    return values, offset


def _enum(spec: dict, data: bytes, offset: int) -> tuple[str, int]:
    variant = spec["variants"][data[offset]]
    if variant.get("fields"):
        # os campos viriam depois do índice: ler só o índice desalinharia tudo
        raise AccountFormatError(f"variante com campos: {variant['name']}")
    return variant["name"], offset + 1


# pela chave do tipo composto, ou pelo `kind` de um tipo definido
_COMPOUND: dict[str, Reader] = {
    "vec": _vec,
    "array": _array,
    "option": _option,
    "defined": _defined,
    "struct": _struct,
    "enum": _enum,
}
_SPECIAL: dict[str, Callable[[str, bytes, int], tuple[Any, int]]] = {
    "u128": _int128,
    "i128": _int128,
    "publicKey": _pubkey,
    "string": _string,
}
