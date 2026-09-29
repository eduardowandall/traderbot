"""Estratégias disponíveis na CLI (`main.py run/backtest <nome>`).

Fica fora de `trader/__init__.py` para que importar qualquer `trader.x` não
carregue todas as estratégias (backlog 1.5). Cada entrada é uma fábrica
chamada com os `key=value` da CLI.
"""

from collections.abc import Callable

from trader.strategy_spec.strategy import SpecStrategy
from trader.trading_strategy import (
    RandomStrategy,
    StrategyComposer,
    TargetValueStrategy,
    TradingStrategy,
)


class NotImplementedStrategy(Exception):
    pass


STRATEGIES: dict[str, Callable[..., TradingStrategy]] = {
    "random": RandomStrategy,
    "target_value": TargetValueStrategy,
    "composer": StrategyComposer,
    # spec declarativa em JSON: `spec 'file=minha-spec.json'`
    "spec": SpecStrategy.from_file,
}


def get_strategy_factory(strategy: str) -> Callable[..., TradingStrategy]:
    """Retorna a fábrica da estratégia correspondente."""
    if strategy not in STRATEGIES:
        raise NotImplementedStrategy(f"Estratégia {strategy} não implementada")
    return STRATEGIES[strategy]
