"""Política de risco: função pura que decide se uma intenção pode executar.

`evaluate` não faz I/O: recebe a intenção, a política, o estado agregado do
ledger e se o kill switch está ativo, e devolve uma `PolicyDecision`. Isso a
torna fácil de testar e impossível de burlar por quem só propõe trades.

A política vem de um arquivo TOML do dono (padrão: `policy.toml` na raiz do
projeto, ou `TRADER_POLICY_FILE`; veja `trader.paths`). Sem arquivo, valem
os limites conservadores abaixo.
"""

import hashlib
import logging
import os
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from trader.models import SOLANA_MINTS
from trader.models.intent import IntentSide, PolicyDecision, TradeIntent
from trader.paths import policy_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Policy:
    # modo real precisa ser habilitado explicitamente pelo dono
    real_trading_enabled: bool = False
    # símbolos permitidos (vazio = todos de SOLANA_MINTS)
    allowed_symbols: tuple[str, ...] = ()
    max_trade_usd: Decimal = Decimal("25")
    max_daily_notional_usd: Decimal = Decimal("100")  # janela móvel de 24h
    max_trades_per_hour: int = 10
    max_daily_loss_usd: Decimal = Decimal("20")  # bloqueia compras
    max_consecutive_failures: int = 3  # circuit breaker
    # permite intenções sem estimativa em USD (ex: swap SOL -> JUP)
    allow_unknown_notional: bool = False
    version: str = field(default="defaults", compare=False)


@dataclass(frozen=True)
class PolicyState:
    """Agregados do ledger usados pela política."""

    daily_notional_usd: Decimal = Decimal("0")
    trades_last_hour: int = 0
    daily_realized_pnl_usd: Decimal = Decimal("0")
    consecutive_failures: int = 0
    unresolved_intent_ids: tuple[str, ...] = ()


def _decimal(key: str, value) -> Decimal:
    # str() evita imprecisão de float do TOML (ex: 0.1)
    parsed = Decimal(str(value))
    if parsed < 0:
        raise ValueError(f"{key} não pode ser negativo")
    return parsed


def _count(key: str, value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} deve ser um inteiro >= 0")
    return value


def _flag(key: str, value) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{key} deve ser true/false")
    return value


def _symbols(key: str, value) -> tuple[str, ...]:
    symbols = tuple(str(s) for s in value)
    for symbol in symbols:
        SOLANA_MINTS.get_by_symbol(symbol)  # valida
    return symbols


_PARSERS = {
    "real_trading_enabled": _flag,
    "allow_unknown_notional": _flag,
    "allowed_symbols": _symbols,
    "max_trade_usd": _decimal,
    "max_daily_notional_usd": _decimal,
    "max_daily_loss_usd": _decimal,
    "max_trades_per_hour": _count,
    "max_consecutive_failures": _count,
}


def _parse(data: dict, version: str) -> Policy:
    unknown = set(data) - set(_PARSERS)
    if unknown:
        raise ValueError(f"Chaves desconhecidas na política: {sorted(unknown)}")
    kwargs = {key: _PARSERS[key](key, value) for key, value in data.items()}
    return Policy(version=version, **kwargs)


MODES = ("real", "dry", "paper")
_SECTIONS = ("trading", "limits")


def _settings(table: dict, where: str) -> dict:
    """Junta [trading] e [limits] de uma tabela, recusando tabelas estranhas."""
    unknown = set(table) - set(_SECTIONS) - (set(MODES) if where == "" else set())
    if unknown:
        prefix = f"[{where}.*]" if where else "raiz"
        raise ValueError(
            f"Seções desconhecidas na política ({prefix}): {sorted(unknown)}"
        )
    merged: dict = {}
    for section in _SECTIONS:
        values = table.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(
                f"[{where + '.' if where else ''}{section}] deve ser uma tabela"
            )
        merged |= values
    return merged


def load_policy(
    path: str | os.PathLike | None = None, mode: str | None = None
) -> Policy:
    """Carrega a política do TOML; sem arquivo, usa os padrões.

    `[trading]`/`[limits]` valem para todos os modos; `[<modo>.trading]` e
    `[<modo>.limits]` (modo = real, dry ou paper) sobrescrevem só as chaves
    que definirem, e só naquele modo.
    """
    if mode is not None and mode not in MODES:
        raise ValueError(f"modo desconhecido: {mode!r}")
    policy_path = Path(path) if path else policy_file()
    if not policy_path.exists():
        logger.info(f"Política {policy_path} não encontrada; usando padrões")
        return Policy()
    content = policy_path.read_bytes()
    data = tomllib.loads(content.decode("utf-8"))

    settings = _settings(data, "")
    # valida as seções de todos os modos, não só a do modo em uso: um erro de
    # digitação em [real.limits] não pode passar despercebido num run paper
    overrides = {m: _settings(data.get(m, {}), m) for m in MODES}
    for mode_name, values in overrides.items():
        _parse(settings | values, "check:" + mode_name)

    version = hashlib.sha256(content).hexdigest()[:12]
    if mode is not None and overrides[mode]:
        settings |= overrides[mode]
        version += f"/{mode}"
    return _parse(settings, version)


@dataclass(frozen=True)
class _Check:
    intent: TradeIntent
    policy: Policy
    state: PolicyState
    halted: bool
    real_mode: bool


# cada regra devolve os motivos de recusa (vazio = ok)
Rule = Callable[[_Check], Iterator[str]]


def _kill_switch(c: _Check) -> Iterator[str]:
    if c.halted:
        yield "kill switch ativo (use `main.py resume`)"


def _real_mode(c: _Check) -> Iterator[str]:
    if c.real_mode and not c.policy.real_trading_enabled:
        yield "modo real desabilitado na política (real_trading_enabled)"


def _unresolved(c: _Check) -> Iterator[str]:
    if c.state.unresolved_intent_ids:
        yield "intenções sem confirmação precisam ser resolvidas: " + ", ".join(
            c.state.unresolved_intent_ids
        )


def _circuit_breaker(c: _Check) -> Iterator[str]:
    if c.state.consecutive_failures >= c.policy.max_consecutive_failures:
        yield (
            f"circuit breaker: {c.state.consecutive_failures} falhas seguidas "
            "(use `main.py resume` para rearmar)"
        )


def _mints(c: _Check) -> Iterator[str]:
    allowed = c.policy.allowed_symbols
    for mint in (c.intent.spend_mint, c.intent.receive_mint):
        if mint not in SOLANA_MINTS:
            yield f"mint desconhecido: {mint}"
        elif allowed and SOLANA_MINTS.symbol_of(mint) not in allowed:
            yield f"símbolo não permitido: {SOLANA_MINTS.symbol_of(mint)}"


def _spend_amount(c: _Check) -> Iterator[str]:
    if c.intent.spend_amount <= 0:
        yield "quantidade a gastar deve ser maior que zero"


def _unknown_notional(c: _Check) -> Iterator[str]:
    if c.intent.notional_usd is None and not c.policy.allow_unknown_notional:
        yield "valor em USD desconhecido (allow_unknown_notional = false)"


def _max_trade(c: _Check) -> Iterator[str]:
    notional = c.intent.notional_usd
    if notional is not None and notional > c.policy.max_trade_usd:
        yield (
            f"trade de {notional:.2f} USD acima do limite {c.policy.max_trade_usd} USD"
        )


def _daily_notional(c: _Check) -> Iterator[str]:
    notional = c.intent.notional_usd
    used = c.state.daily_notional_usd
    if notional is not None and used + notional > c.policy.max_daily_notional_usd:
        yield (
            f"limite diário: {used:.2f} + {notional:.2f} "
            f"> {c.policy.max_daily_notional_usd} USD"
        )


def _trades_per_hour(c: _Check) -> Iterator[str]:
    if c.state.trades_last_hour >= c.policy.max_trades_per_hour:
        yield f"limite de {c.policy.max_trades_per_hour} trades por hora atingido"


def _daily_loss(c: _Check) -> Iterator[str]:
    if c.state.daily_realized_pnl_usd <= -c.policy.max_daily_loss_usd:
        yield (
            f"perda diária de {-c.state.daily_realized_pnl_usd:.2f} USD atingiu o "
            f"limite de {c.policy.max_daily_loss_usd} USD"
        )


# valem para toda intenção, inclusive vendas
_SAFETY_RULES: tuple[Rule, ...] = (
    _kill_switch,
    _real_mode,
    _unresolved,
    _circuit_breaker,
    _mints,
    _spend_amount,
)
# orçamento: não se aplica a vendas, porque bloquear uma saída deixaria a
# posição presa. As travas acima continuam valendo.
_BUDGET_RULES: tuple[Rule, ...] = (
    _unknown_notional,
    _max_trade,
    _daily_notional,
    _trades_per_hour,
    _daily_loss,
)


def evaluate(
    intent: TradeIntent,
    policy: Policy,
    state: PolicyState,
    *,
    halted: bool,
    real_mode: bool,
) -> PolicyDecision:
    check = _Check(intent, policy, state, halted, real_mode)
    rules = _SAFETY_RULES
    if intent.side != IntentSide.SELL:
        rules += _BUDGET_RULES
    reasons = tuple(reason for rule in rules for reason in rule(check))
    return PolicyDecision(
        allowed=not reasons, reasons=reasons, policy_version=policy.version
    )
