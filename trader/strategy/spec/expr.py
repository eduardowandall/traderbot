"""A condição `expr`: uma expressão restrita, lida sem `eval`.

O `ast` do Python faz a leitura (`ast.parse(mode="eval")`: precedência,
parênteses, espaços); daqui só sai uma árvore que passou por uma lista
branca: números, as variáveis de `VARIABLES`, `FUNÇÃO(INTEIRO)` de
`FUNCTIONS`, `+ - * /`, `-` unário, uma comparação (`< <= > >=`) por par de
valores, `and`, `or`, `not`. Nada mais (atributos, chamadas livres, nomes
soltos) passa, e a árvore é avaliada aqui, nó a nó, nunca compilada.

Cada nó é um número ou uma condição, conferido na leitura: a comparação junta
dois números, `and`/`or`/`not` juntam condições, e a expressão inteira é uma
condição. Um valor que falta (indicador sem dados, sem posição, divisão por
zero) deixa a comparação **desconhecida**; `and`/`or`/`not` seguem a lógica de
três valores (desconhecido `and` falso = falso, `not` desconhecido =
desconhecido), e só uma expressão verdadeira dispara.

O texto guardado é o normalizado (`render`, o `ast.unparse`), então espaços e
parênteses redundantes não mudam o id da spec.
"""

import ast
import operator
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from trader.shared import indicators as ind

MAX_LENGTH = 200
MIN_WINDOW, MAX_WINDOW = 2, 500

# atributos do contexto do tick
VARIABLES = frozenset({"price", "entry_price", "peak", "last_exit_price"})
# os indicadores do registro (`trader.shared.indicators.INDICATORS`)
FUNCTIONS = frozenset(ind.INDICATORS)


def _div(a: Decimal, b: Decimal) -> Decimal | None:
    return None if b == 0 else a / b


ARITH: dict[type, Callable[[Decimal, Decimal], Decimal | None]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: _div,
}
COMPARISONS: dict[type, Callable[[Decimal, Decimal], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class ExprError(ValueError):
    def __init__(self, msg: str, pos: int):
        super().__init__(f"{msg} (posição {pos})")
        self.pos = pos


# --- a leitura -------------------------------------------------------------------


def parse(text: str) -> ast.expr:
    """A árvore conferida; `ExprError` (um `ValueError`) com a posição."""
    if len(text) > MAX_LENGTH:
        raise ExprError(f"mais de {MAX_LENGTH} caracteres", MAX_LENGTH)
    try:
        tree = ast.parse(text.strip(), mode="eval").body
    except SyntaxError as ex:
        pos = (ex.offset or 1) - 1 + len(text) - len(text.lstrip())
        raise ExprError(f"sintaxe inválida: {ex.msg}", pos) from None
    _condition(tree)
    return tree


def _condition(node: ast.expr) -> None:
    match node:
        case ast.Compare(left=left, ops=[op], comparators=[right]) if (
            type(op) in COMPARISONS
        ):
            _number(left)
            _number(right)
        case ast.BoolOp(values=values):
            for value in values:
                _condition(value)
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            _condition(operand)
        case _:
            raise ExprError(_not_a_condition(node), _pos(node))


def _not_a_condition(node: ast.expr) -> str:
    if isinstance(node, ast.Compare) and len(node.ops) > 1:
        return "uma comparação por vez"
    return "esperava uma condição (uma comparação)"


def _number(node: ast.expr) -> None:
    match node:
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            _number(operand)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in ARITH:
            _number(left)
            _number(right)
        case _:
            _leaf(node)


def _leaf(node: ast.expr) -> None:
    if isinstance(node, ast.Name):
        _known(node.id, VARIABLES, node)
    elif isinstance(node, ast.Call):
        _call(node)
    elif not _is_numeral(node):
        raise ExprError(_not_a_number(node), _pos(node))


def _is_numeral(node: ast.expr) -> bool:
    value = getattr(node, "value", None) if isinstance(node, ast.Constant) else None
    return isinstance(value, int | float) and not isinstance(value, bool)


def _not_a_number(node: ast.expr) -> str:
    if isinstance(node, ast.Compare | ast.BoolOp) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
    ):
        return "precisa de números, não de condições"
    return f"não permitido: {ast.unparse(node)!r}"


def _known(name: str, names: frozenset[str], node: ast.expr) -> None:
    if name not in names:
        raise ExprError(f"nome desconhecido {name!r}", _pos(node))


def _call(node: ast.Call) -> None:
    if not isinstance(node.func, ast.Name):
        raise ExprError(f"não permitido: {ast.unparse(node)!r}", _pos(node))
    _known(node.func.id, FUNCTIONS, node)
    match node:
        case ast.Call(args=[ast.Constant(value=int() as window)], keywords=[]) if (
            not isinstance(window, bool)
        ):
            if not MIN_WINDOW <= window <= MAX_WINDOW:
                raise ExprError(
                    f"janela {window} fora de {MIN_WINDOW}-{MAX_WINDOW}",
                    _pos(node.args[0]),
                )
        case _:
            pos = _pos(node.args[0]) if node.args else _pos(node)
            raise ExprError("esperava um número inteiro de barras", pos)


def _pos(node: ast.expr) -> int:
    return node.col_offset


def render(tree: ast.expr) -> str:
    return ast.unparse(tree)


# --- o aquecimento ---------------------------------------------------------------


def _calls(tree: ast.expr) -> list[tuple[str, int]]:
    return [
        (node.func.id, node.args[0].value)  # type: ignore[attr-defined]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]


def lookback(tree: ast.expr) -> int:
    """Barras para todos os indicadores da expressão terem valor."""
    return max((ind.lookback(*c) for c in _calls(tree)), default=1)


def history(tree: ast.expr) -> int:
    """Barras para todos convergirem."""
    return max((ind.history(*c) for c in _calls(tree)), default=1)


# --- a avaliação (só árvores que passaram por `parse`) ---------------------------


def value(node: ast.expr, ctx: Any) -> Decimal | None:
    match node:
        case ast.Constant(value=number):
            return Decimal(repr(number))
        case ast.Name(id=name):
            return getattr(ctx, name)
        case ast.Call(func=ast.Name(id=name), args=[ast.Constant(value=window)]):
            return ctx.bank.get(name, window)
        case ast.UnaryOp(operand=operand):
            v = value(operand, ctx)
            return None if v is None else -v
    return _arith(node, ctx)  # type: ignore[arg-type]


def _arith(node: ast.BinOp, ctx: Any) -> Decimal | None:
    a, b = value(node.left, ctx), value(node.right, ctx)
    return None if a is None or b is None else ARITH[type(node.op)](a, b)


def truth(node: ast.expr, ctx: Any) -> bool | None:
    match node:
        case ast.Compare(left=left, ops=[op], comparators=[right]):
            a, b = value(left, ctx), value(right, ctx)
            return None if a is None or b is None else COMPARISONS[type(op)](a, b)
        case ast.UnaryOp(operand=operand):
            t = truth(operand, ctx)
            return None if t is None else not t
    return _logic(node, ctx)  # type: ignore[arg-type]


def _logic(node: ast.BoolOp, ctx: Any) -> bool | None:
    """Três valores: o que decide (falso no and, verdadeiro no or) vence."""
    values = [truth(item, ctx) for item in node.values]
    decisive = isinstance(node.op, ast.Or)
    if decisive in values:
        return decisive
    return None if None in values else not decisive


def holds(tree: ast.expr, ctx: Any) -> bool:
    """Só uma expressão verdadeira dispara (desconhecida não)."""
    return truth(tree, ctx) is True
