"""O ledger contra as posições da Jupiter Perps (A11a, D10).

Uma posição é identificada pelo mercado e pelo lado (a Jupiter tem uma por
carteira, mercado, colateral e lado; o colateral sai do lado). A alavancagem
não entra: o venue guarda tamanho e colateral, não a alavancagem pedida.

- `missing_on_venue`: o ledger tem a posição aberta e o venue não (liquidada,
  ou fechada fora do bot): o serviço registra `perp_mismatch` e para as
  entradas nesse mercado (ligado na A11b).
- `unknown_to_ledger`: o venue tem uma posição que nenhum bucket abriu (aberta
  à mão, ou um envio sem desfecho): o dono confere.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from trader.execution.models.perp import PerpTerms
from trader.shared.models.direction import Direction


class MismatchKind(StrEnum):
    MISSING_ON_VENUE = "missing_on_venue"
    UNKNOWN_TO_LEDGER = "unknown_to_ledger"


@dataclass(frozen=True)
class PerpMismatch:
    market_mint: str
    direction: Direction
    kind: MismatchKind


def _keys(terms: Iterable[PerpTerms]) -> set[tuple[str, Direction]]:
    return {(t.market_mint, t.direction) for t in terms}


def reconcile_perps(
    ledger_open: Iterable[PerpTerms], venue_open: Iterable[PerpTerms]
) -> list[PerpMismatch]:
    """O que o ledger e o venue discordam, em ordem estável."""
    ledger, venue = _keys(ledger_open), _keys(venue_open)
    found = [(k, MismatchKind.MISSING_ON_VENUE) for k in ledger - venue]
    found += [(k, MismatchKind.UNKNOWN_TO_LEDGER) for k in venue - ledger]
    return [PerpMismatch(m, d, kind) for (m, d), kind in sorted(found)]
