"""`docs/specs.md` é o contrato para escrever specs: não pode ficar para trás.

Specs são escritas à mão (ou por um agente) a partir desse guia, sem comando
de schema: um campo ou tipo de condição novo no código precisa aparecer lá.
"""

import json

import pytest

from trader.paths import PROJECT_ROOT
from trader.strategy_spec.conditions import PREDICATES
from trader.strategy_spec.models import StrategySpec

GUIDE = (PROJECT_ROOT / "docs" / "specs.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("condition", sorted(PREDICATES))
def test_every_condition_type_is_documented(condition):
    assert f"| `{condition}` |" in GUIDE


@pytest.mark.parametrize("field", sorted(StrategySpec.model_fields))
def test_every_top_level_field_is_documented(field):
    assert f"| `{field}` |" in GUIDE


def test_the_example_in_the_guide_is_a_valid_spec():
    start = GUIDE.index("```json") + len("```json")
    example = GUIDE[start : GUIDE.index("```", start)]
    spec = StrategySpec.model_validate(json.loads(example))
    assert spec.name == "sol-dip"
