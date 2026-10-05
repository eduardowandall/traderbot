"""Camadas do pacote `trader`: quem pode importar quem.

Cada módulo pertence a uma camada e só pode importar das camadas listadas em
`ALLOWED`. A regra que mais importa: estratégias e o lado da estratégia
(strategy-runner) nunca importam execução, venue (quem move fundos) ou risco
(política/ledger), então código de estratégia não alcança a chave, o ledger
nem a política. Ver `docs/architecture.md`.

Módulo novo sem camada faz o teste falhar: adicione-o em `PACKAGES`.
"""

import ast
from pathlib import Path

import pytest

from trader.paths import PROJECT_ROOT

TRADER_DIR = PROJECT_ROOT / "trader"

# camada -> camadas que ela pode importar (inclui a própria)
ALLOWED: dict[str, set[str]] = {
    "core": {"core"},
    "strategy": {"core", "strategy"},
    "market": {"core", "market"},
    "venue": {"core", "market", "venue"},
    "risk": {"core", "risk"},
    "execution": {"core", "market", "venue", "risk", "execution"},
    "strategy-side": {"core", "strategy", "market", "strategy-side"},
    "app": set(),  # composição: pode importar tudo (tratado em `_allowed`)
}

# pacote/módulo -> camada; vale o prefixo mais longo
PACKAGES: dict[str, str] = {
    "trader.models": "core",
    "trader.paths": "core",
    "trader.logging_config": "core",
    "trader.indicators": "core",
    "trader.strategy_spec": "strategy",
    "trader.providers.jupiter.async_jupiter_client": "market",
    "trader.providers.jupiter.jupiter_data": "market",
    "trader.providers.jupiter.logging_utils": "market",
    "trader.providers.jupiter.candles": "market",
    "trader.market": "market",
    "trader.providers": "venue",
    "trader.paper": "venue",
    "trader.policy": "risk",
    "trader.ledger": "risk",
    "trader.execution": "execution",
    "trader.trading_service": "execution",
    "trader.trading_service.protocol": "core",
    "trader.trading_service.client": "strategy-side",
    "trader.trading_service.remote": "strategy-side",
    "trader.trading_service.wire": "core",
    "trader.runners": "app",
    # sem chave nem ledger: só o bot, a spec e o cliente remoto
    "trader.runners.strategy_runner": "strategy-side",
    # o loop do bot só conhece MarketData + TradeClient
    "trader.bot": "strategy-side",
    "trader.backtest": "app",
    "trader.notification": "app",
    "trader.wiring": "app",
    "trader.cli": "app",
}

# módulos com camada própria, sem valer como prefixo: o `__init__` do pacote
# raiz é carregado por todo `import trader.x`, então precisa ficar vazio (core)
EXACT: dict[str, str] = {"trader": "core"}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


MODULES = {
    _module_name(p): p
    for p in sorted(TRADER_DIR.rglob("*.py"))
    if "__pycache__" not in p.parts
}


def layer_of(module: str) -> str | None:
    if module in EXACT:
        return EXACT[module]
    matches = [key for key in PACKAGES if module == key or module.startswith(key + ".")]
    return PACKAGES[max(matches, key=len)] if matches else None


def _absolute(module: str, node: ast.ImportFrom) -> str:
    """Resolve `from .x import y` relativo ao módulo que importa."""
    if not node.level:
        return node.module or ""
    is_package = MODULES[module].name == "__init__.py"
    base = module.split(".")
    base = base if is_package else base[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _targets(module: str, node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    source = _absolute(module, node)
    # `from trader import logging_config` importa o submódulo, não o pacote
    return [
        f"{source}.{a.name}" if f"{source}.{a.name}" in MODULES else source
        for a in node.names
    ]


def internal_imports(module: str, source: str | None = None) -> set[str]:
    if source is None:
        source = MODULES[module].read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        target
        for node in ast.walk(tree)
        for target in _targets(module, node)
        if target == "trader" or target.startswith("trader.")
    }


def _allowed(importer_layer: str, target_layer: str) -> bool:
    return importer_layer == "app" or target_layer in ALLOWED[importer_layer]


def _forbidden(module: str, source: str | None = None) -> set[str]:
    importer = layer_of(module)
    return {
        target
        for target in internal_imports(module, source)
        if importer
        and (target_layer := layer_of(target))
        and not _allowed(importer, target_layer)
    }


def _violations() -> set[tuple[str, str]]:
    return {(m, t) for m in MODULES for t in _forbidden(m)}


def test_every_module_has_a_layer():
    unmapped = [m for m in MODULES if layer_of(m) is None]
    assert not unmapped, f"adicione a camada em PACKAGES: {unmapped}"


def test_imports_respect_the_layers():
    violations = _violations()
    assert not violations, "\n".join(
        f"{m} ({layer_of(m)}) importa {t} ({layer_of(t)})"
        for m, t in sorted(violations)
    )


@pytest.mark.parametrize("layer", ["strategy", "strategy-side"])
def test_strategy_code_cannot_reach_execution(layer):
    # a garantia central da arquitetura, explícita (não só via ALLOWED)
    assert not ALLOWED[layer] & {"venue", "risk", "execution", "app"}


def test_layer_checker_catches_a_forbidden_import():
    # auto-teste: o verificador precisa de fato enxergar uma violação,
    # inclusive importação relativa e dentro de função
    source = "\n".join(
        [
            "from ..ledger import Ledger",
            "def f():",
            "    import trader.providers.jupiter.async_jupiter_svc",
            "from trader.models import Order",
        ]
    )
    assert _forbidden("trader.strategy_spec.strategy", source) == {
        "trader.ledger",
        "trader.providers.jupiter.async_jupiter_svc",
    }
    assert _forbidden("trader.execution.account", source) == set()
    assert layer_of("trader.trading_service.protocol") == "core"
    assert layer_of("trader.trading_service.service") == "execution"
