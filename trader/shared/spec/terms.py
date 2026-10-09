"""Os termos de uma spec: o que o trade-runner precisa saber dela (B13).

A spec inteira (condições, `expr`, sizing) é do lado da estratégia
(`trader.strategy.spec`). O trade-runner só usa o dinheiro e o prazo: o par, o
orçamento, a perda máxima, o maior gasto por compra e quando a spec vence; numa
perp (A8), também o mercado (lado, alavancagem), o stop e o `max_hold`, que ele
confere de novo contra a política.
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
from trader.shared.models.direction import Direction
from trader.shared.models.perp import PERP_FEE_RATE

# os formatos dos campos que a spec e os termos têm em comum
SPEC_ID_PATTERN = r"^[0-9a-f]{12}$"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
SYMBOL_PATTERN = r"^[A-Z0-9]{1,16}-[A-Z0-9]{1,16}$"

Usd = Annotated[Decimal, Field(gt=0)]
Leverage = Annotated[Decimal, Field(ge=Decimal("1.1"), le=250)]


class AddCollateral(BaseModel):
    """`market.add_collateral` (A12): colateral a mais perto da liquidação.

    Com o preço a `within_pct`% do preço de liquidação, o trade-runner põe
    `usd` de colateral na posição (dentro do orçamento do bucket), no máximo
    `max_times` vezes por posição.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    within_pct: Annotated[Decimal, Field(gt=0, le=50)]
    usd: Usd
    max_times: Annotated[int, Field(ge=1, le=10)] = 1


class PerpMarket(BaseModel):
    """`market` de uma spec de perp (A8, D2); sem ele, a spec é spot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["perp"]
    venue: Literal["jupiter"]
    direction: Direction
    leverage: Leverage
    # fora do `canonical_json` quando None: os ids das specs de perp não mudam
    add_collateral: AddCollateral | None = None

    def max_stop_pct(self) -> Decimal:
        """O stop mais largo: metade da distância até a liquidação, menos as
        taxas de ida e volta (D10)."""
        return 50 / self.leverage - 2 * PERP_FEE_RATE * 100


def check_perp(
    market: PerpMarket | None, stop_pct: Decimal | None, max_hold_minutes: int | None
) -> None:
    """Uma perp precisa de um stop bem antes da liquidação e de um `max_hold`."""
    if market is None:
        return
    if max_hold_minutes is None:
        raise ValueError("uma perp precisa de uma saída max_hold (exit.mode any)")
    limit = market.max_stop_pct()
    if stop_pct is None or stop_pct > limit:
        raise ValueError(
            f"o stop de uma perp {market.leverage}x vai até {limit:.2f}% "
            "(metade da distância até a liquidação, depois das taxas)"
        )


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
    # só perps (A8): o mercado, o stop e o `max_hold` (conferidos de novo)
    market: PerpMarket | None = None
    stop_pct: Decimal | None = None
    max_hold_minutes: int | None = None

    @model_validator(mode="after")
    def _invariants(self):
        check_money_and_expiry(
            self.budget_usd, self.max_loss_usd, self.expires_at, self.ttl_days
        )
        check_perp(self.market, self.stop_pct, self.max_hold_minutes)
        return self

    @property
    def exposure_usd(self) -> Decimal:
        """O maior trade em exposição: o gasto x alavancagem numa perp."""
        leverage = self.market.leverage if self.market else Decimal(1)
        return self.max_trade_usd * leverage

    def expiry(self, start: datetime) -> datetime:
        """Quando a spec vence, para uma execução que começou em `start`."""
        return expiry(self.expires_at, self.ttl_days, start)
