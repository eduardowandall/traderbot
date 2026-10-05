"""Leitura e validação de specs.

`parse_spec` transforma o texto JSON numa `StrategySpec` (erros de formato
viram `SpecParseError` com um `SpecError` por campo). `validate` confere a
spec contra os limites do dono (`SpecLimits`) e devolve a lista de problemas;
lista vazia = válida.

`SpecLimits` é um dado simples, não a `Policy`: a camada de estratégia não
importa a política (camada de risco). Quem monta os limites a partir da
política é a camada de aplicação (`trader/cli/bot.py`). Esta validação é
consultiva: quem executa (o gateway) valida de novo com a política dele.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError

from trader.models import SOLANA_MINTS, Mint
from trader.strategy_spec.models import FixedUsd, StrategySpec

# prazo máximo de uma spec; vai para a seção [strategies] da política (B1)
DEFAULT_MAX_DAYS = 30


@dataclass(frozen=True)
class SpecError:
    path: str  # campo com problema, ex: "exit.stop.pct" ("" = a spec toda)
    msg: str


class SpecParseError(ValueError):
    def __init__(self, errors: list[SpecError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.path}: {e.msg}" for e in errors))


@dataclass(frozen=True)
class SpecLimits:
    max_trade_usd: Decimal
    allowed_symbols: tuple[str, ...] = ()  # vazio = todos do registro
    max_days: int = DEFAULT_MAX_DAYS


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


Rule = Callable[[StrategySpec, SpecLimits, datetime], Iterator[SpecError]]


def _symbol(spec: StrategySpec, limits: SpecLimits, now: datetime):
    try:
        token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    except ValueError as ex:
        yield SpecError("symbol", str(ex))
        return
    yield from _legs(token, quote)
    yield from _allowed(token, quote, limits)


def _legs(token: Mint, quote: Mint) -> Iterator[SpecError]:
    # qualquer entrada do registro: preços e saldos ficam no token de cotação,
    # e orçamentos em USD são convertidos pelo preço dele (Price API)
    if token.mint == quote.mint:
        yield SpecError("symbol", f"entrada e saída são o mesmo token {token.symbol}")
    if token.is_usd_stable:
        # só compras (long): comprar stablecoin seria vender o outro token
        yield SpecError("symbol", f"saída {token.symbol} não pode ser stablecoin")


def _allowed(token: Mint, quote: Mint, limits: SpecLimits) -> Iterator[SpecError]:
    if not limits.allowed_symbols:
        return
    for mint in (token, quote):
        if mint.symbol not in limits.allowed_symbols:
            yield SpecError("symbol", f"símbolo não permitido: {mint.symbol}")


def _sizing(spec: StrategySpec, limits: SpecLimits, now: datetime):
    sizing = spec.sizing
    path = "sizing.usd" if isinstance(sizing, FixedUsd) else "sizing.pct"
    usd = sizing.max_usd(spec.budget_usd)
    if usd > limits.max_trade_usd:
        # acima do limite por trade toda compra seria recusada pela política
        yield SpecError(
            path, f"{usd} USD acima do limite por trade {limits.max_trade_usd}"
        )
    if usd > spec.budget_usd:
        yield SpecError(path, f"{usd} USD acima do budget_usd {spec.budget_usd}")


def _expiry(spec: StrategySpec, limits: SpecLimits, now: datetime):
    path = "expires_at" if spec.expires_at is not None else "ttl_days"
    expiry = spec.expiry(now)
    if expiry <= now:
        yield SpecError(path, "já expirou")
    if expiry > now + timedelta(days=limits.max_days):
        yield SpecError(path, f"mais de {limits.max_days} dias no futuro")


RULES: tuple[Rule, ...] = (_symbol, _sizing, _expiry)


def validate(
    spec: StrategySpec, limits: SpecLimits, now: datetime | None = None
) -> list[SpecError]:
    now = now or datetime.now(UTC)
    return [error for rule in RULES for error in rule(spec, limits, now)]
