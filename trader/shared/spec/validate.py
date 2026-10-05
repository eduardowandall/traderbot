"""Validação dos termos de uma spec contra os limites do dono.

`validate` confere os `SpecTerms` (o par, o maior gasto por compra, o prazo)
contra os `SpecLimits` e devolve a lista de problemas; lista vazia = válida.
Quem tem a spec inteira passa `spec.terms()`; o trade-runner recebe os termos
pelo `hello` e confere com a política dele. A leitura do JSON da spec fica do
lado da estratégia (`trader.strategy.spec.parse`).

`SpecLimits` é um dado simples, não a `Policy`: a camada de estratégia não
importa a política (camada de risco). Quem monta os limites a partir da
política é a camada de aplicação (`trader/api/cli/runners.py`). Esta validação é
consultiva: quem executa (o gateway) valida de novo com a política dele.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.shared.models import SOLANA_MINTS, Mint
from trader.shared.spec.terms import SpecTerms

# prazo máximo de uma spec; vai para a seção [strategies] da política (B1)
DEFAULT_MAX_DAYS = 30


@dataclass(frozen=True)
class SpecError:
    path: str  # campo com problema, ex: "exit.stop.pct" ("" = a spec toda)
    msg: str


@dataclass(frozen=True)
class SpecLimits:
    max_trade_usd: Decimal
    allowed_symbols: tuple[str, ...] = ()  # vazio = todos do registro
    max_days: int = DEFAULT_MAX_DAYS


Rule = Callable[[SpecTerms, SpecLimits, datetime], Iterator[SpecError]]


def _symbol(terms: SpecTerms, limits: SpecLimits, now: datetime):
    try:
        token, quote = SOLANA_MINTS.get_pair(terms.symbol)
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


def _sizing(terms: SpecTerms, limits: SpecLimits, now: datetime):
    path, usd = terms.sizing_field, terms.max_trade_usd
    if usd > limits.max_trade_usd:
        # acima do limite por trade toda compra seria recusada pela política
        yield SpecError(
            path, f"{usd} USD acima do limite por trade {limits.max_trade_usd}"
        )
    if usd > terms.budget_usd:
        yield SpecError(path, f"{usd} USD acima do budget_usd {terms.budget_usd}")


def _expiry(terms: SpecTerms, limits: SpecLimits, now: datetime):
    path = "expires_at" if terms.expires_at is not None else "ttl_days"
    expiry = terms.expiry(now)
    if expiry <= now:
        yield SpecError(path, "já expirou")
    if expiry > now + timedelta(days=limits.max_days):
        yield SpecError(path, f"mais de {limits.max_days} dias no futuro")


RULES: tuple[Rule, ...] = (_symbol, _sizing, _expiry)


def validate(
    terms: SpecTerms, limits: SpecLimits, now: datetime | None = None
) -> list[SpecError]:
    now = now or datetime.now(UTC)
    return [error for rule in RULES for error in rule(terms, limits, now)]
