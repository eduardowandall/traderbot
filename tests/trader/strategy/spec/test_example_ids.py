"""Os ids das specs de exemplo não mudam (A7).

O id é o bucket: um id novo abre outro bucket e deixa o antigo (posição,
PnL, conta de token a fechar) para trás. `ad3606394fc0` é o bucket real do
A6. Um campo novo da spec com valor padrão precisa ficar fora do
`canonical_json` (A8, `market`); mudar o comportamento de uma spec daqui
muda o id, e a linha muda junto, de propósito.
"""

from pathlib import Path

import pytest

from trader.shared.paths import PROJECT_ROOT
from trader.strategy.spec.models import StrategySpec

EXAMPLES = PROJECT_ROOT / "docs" / "examples"
IDS = {
    "spec-jup-sol-expr.json": "4c4dcb9b28aa",
    "spec-random.json": "3f82df8aa389",
    "spec-real-first-run.json": "ad3606394fc0",
    "spec-scalp-test.json": "555d793e2f18",
    "spec-soak-metronome.json": "733e9d719fd6",
    "spec-soak-revert.json": "e104dba8efcd",
    "spec-sol-dip.json": "8bb94a8356d2",
    "spec-target-value.json": "132be7f10464",
    "spec-wma-composer.json": "5422d6605a9a",
}


def _id(path: Path) -> str:
    return StrategySpec.model_validate_json(path.read_text(encoding="utf-8")).spec_id()


def test_every_example_is_pinned():
    assert sorted(p.name for p in EXAMPLES.glob("*.json")) == sorted(IDS)


@pytest.mark.parametrize(("name", "spec_id"), sorted(IDS.items()))
def test_example_ids_are_stable(name, spec_id):
    assert _id(EXAMPLES / name) == spec_id
