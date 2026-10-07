"""O lado de uma posição (A7, D3): comprado ou vendido.

Spot é sempre `LONG`. Um `SHORT` (perps, A8) ganha quando o preço cai: as
contas de posição multiplicam pela `sign` e os blocos de posição comparam
no sentido favorável.
"""

from decimal import Decimal
from enum import StrEnum, auto


class Direction(StrEnum):
    LONG = auto()
    SHORT = auto()

    @property
    def sign(self) -> Decimal:
        """+1 comprado, -1 vendido: o PnL é `sign x (preço - entrada)`."""
        return _SIGNS[self]

    def better(self, a: Decimal, b: Decimal) -> Decimal:
        """O mais favorável dos dois preços (o maior comprado, o menor vendido)."""
        return max(a, b) if self is Direction.LONG else min(a, b)


# por tick (blocos de posição): um Decimal só por lado
_SIGNS = {Direction.LONG: Decimal(1), Direction.SHORT: Decimal(-1)}
