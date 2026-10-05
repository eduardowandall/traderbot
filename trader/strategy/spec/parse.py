"""`parse_spec`: o texto JSON de uma spec vira uma `StrategySpec`.

Erros de formato viram `SpecParseError`, com um `SpecError` por campo (o mesmo
formato dos erros de `validate`, em `trader.shared.spec.validate`).
"""

from pydantic import ValidationError

from trader.shared.spec.validate import SpecError
from trader.strategy.spec.models import StrategySpec


class SpecParseError(ValueError):
    def __init__(self, errors: list[SpecError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.path}: {e.msg}" for e in errors))


def parse_spec(text: str) -> StrategySpec:
    try:
        return StrategySpec.model_validate_json(text)
    except ValidationError as ex:
        raise SpecParseError(
            [
                SpecError(".".join(str(p) for p in err["loc"]), err["msg"])
                for err in ex.errors(include_url=False)
            ]
        ) from ex
