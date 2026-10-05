"""Os termos de uma spec: o que o trade-runner precisa saber dela (B13).

A spec inteira (condições, `expr`, sizing) é do lado da estratégia
(`trader.strategy.spec`). O trade-runner só usa o dinheiro e o prazo: o par, o
orçamento, a perda máxima, o maior gasto por compra e quando a spec vence.
`StrategySpec.terms()` monta estes termos; o `hello` os manda pelo fio.

Os termos chegam de outro processo, então o modelo confere as mesmas
invariantes da spec (um prazo só, perda máxima dentro do orçamento). As regras
moram aqui (`check_money_and_expiry`, `expiry`) e `StrategySpec` as usa.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trader.shared.indicators import to_utc

# os formatos dos campos que a spec e os termos têm em comum
SPEC_ID_PATTERN = r"^[0-9a-f]{12}$"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
SYMBOL_PATTERN = r"^[A-Z0-9]{1,16}-[A-Z0-9]{1,16}$"

Usd = Annotated[Decimal, Field(gt=0)]


def check_money_and_expiry(
    budget_usd: Decimal,
    max_loss_usd: Decimal,
    expires_at: datetime | None,
    ttl_days: int | None,
) -> None:
    """As invariantes da spec: perda máxima dentro do orçamento, um prazo só."""
    if max_loss_usd > budget_usd:
        raise ValueError("max_loss_usd não pode passar de budget_usd")
    if (expires_at is None) == (ttl_days is None):
        raise ValueError("informe expires_at ou ttl_days (exatamente um)")


def expiry(
    expires_at: datetime | None, ttl_days: int | None, start: datetime
) -> datetime:
    """Quando a spec vence, para uma execução que começou em `start`."""
    if expires_at is not None:
        return expires_at
    return to_utc(start) + timedelta(days=ttl_days or 0)


class SpecTerms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # hash da spec inteira (`StrategySpec.spec_id`), calculado pela estratégia
    spec_id: Annotated[str, Field(pattern=SPEC_ID_PATTERN)]
    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    symbol: Annotated[str, Field(pattern=SYMBOL_PATTERN)]
    budget_usd: Usd
    max_loss_usd: Usd
    # o maior gasto de uma compra (`Sizing.max_usd`), para os limites
    max_trade_usd: Usd
    # o campo da spec que define o gasto, para as mensagens de erro
    sizing_field: Literal["sizing.usd", "sizing.pct"]
    expires_at: AwareDatetime | None = None
    ttl_days: Annotated[int | None, Field(ge=1, le=365)] = None

    @model_validator(mode="after")
    def _invariants(self):
        check_money_and_expiry(
            self.budget_usd, self.max_loss_usd, self.expires_at, self.ttl_days
        )
        return self

    def expiry(self, start: datetime) -> datetime:
        """Quando a spec vence, para uma execução que começou em `start`."""
        return expiry(self.expires_at, self.ttl_days, start)
