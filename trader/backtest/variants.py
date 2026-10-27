"""Variantes de uma spec (A36): `--vary exit.stop.pct=3,4.1,5`.

Cada eixo é um campo da spec pelo caminho (chaves e índices de lista
separados por ponto) e uma lista de valores; o produto dos eixos dá as
variantes. Cada uma é o JSON da spec com os campos trocados, validado como
qualquer spec: tem o próprio id. A spec original vem sempre primeiro.
"""

import copy
import itertools
import json
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.parse import SpecParseError, parse_spec

# mudam os dados da janela, não só a estratégia: backtests separados
FIXED = ("symbol", "timeframe", "market")


@dataclass(frozen=True)
class Variant:
    spec: StrategySpec
    data: dict  # o JSON da variante (o que `--write-best` grava)
    changes: dict[str, Any] = field(default_factory=dict)  # caminho -> valor

    @cached_property
    def id(self) -> str:
        return self.spec.spec_id()

    def describe(self) -> str:
        return ", ".join(f"{p}={v}" for p, v in self.changes.items()) or "base"


def parse_vary(items: list[str]) -> list[tuple[str, list[Any]]]:
    """`PATH=V1,V2` -> `(PATH, [V1, V2])`; cada valor é um literal JSON (`3`,
    `true`, `"wma"`) ou, se não for, o texto."""
    return [_axis(item) for item in items]


def _axis(item: str) -> tuple[str, list[Any]]:
    path, sep, values = item.partition("=")
    if not sep or not path.strip() or not values.strip():
        raise ValueError(f"--vary {item!r}: use CAMINHO=V1,V2 (ex: exit.stop.pct=3,4)")
    return path.strip(), [_literal(v.strip()) for v in values.split(",")]


def _literal(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def variants(base: dict, axes: list[tuple[str, list[Any]]]) -> list[Variant]:
    """A spec original e cada combinação dos eixos, sem repetir um id."""
    found = [Variant(_parse(base, {}), base)]
    paths = [path for path, _ in axes]
    for values in itertools.product(*(vals for _, vals in axes)):
        changes = dict(zip(paths, values, strict=True))
        data = _with(base, changes)
        variant = Variant(_parse(data, changes), data, changes)
        found.append(_same_data(found[0], variant))
    unique: dict[str, Variant] = {}
    for variant in found:
        unique.setdefault(variant.id, variant)
    return list(unique.values())


def _same_data(base: Variant, variant: Variant) -> Variant:
    """Uma variante lê as mesmas janelas que a base: o par, o timeframe e o
    mercado (spot ou perp) não mudam."""
    for name in FIXED:
        if getattr(variant.spec, name) != getattr(base.spec, name):
            raise ValueError(
                f"--vary {variant.describe()}: {', '.join(FIXED)} mudam os dados; "
                "rode backtests separados"
            )
    return variant


def _with(base: dict, changes: dict[str, Any]) -> dict:
    data = copy.deepcopy(base)
    for path, value in changes.items():
        _set(data, path, value)
    return data


def _set(data: Any, path: str, value: Any) -> None:
    *parents, last = path.split(".")
    node = data
    for key in parents:
        node = _child(node, key, path)
    _child(node, last, path)  # o campo precisa existir: um erro de digitação
    node[int(last) if isinstance(node, list) else last] = value


def _child(node: Any, key: str, path: str) -> Any:
    try:
        return node[int(key)] if isinstance(node, list) else node[key]
    except KeyError, IndexError, ValueError, TypeError:
        raise ValueError(
            f"--vary {path}: a spec não tem {key!r} nesse caminho"
        ) from None


def _parse(data: dict, changes: dict[str, Any]) -> StrategySpec:
    try:
        return parse_spec(json.dumps(data))
    except SpecParseError as ex:
        if not changes:
            raise  # a própria spec: o erro com o campo, como sem --vary
        what = ", ".join(f"{p}={v!r}" for p, v in changes.items()) or "a spec"
        raise ValueError(f"--vary {what}: {ex}") from ex
