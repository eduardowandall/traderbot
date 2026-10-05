"""Serviço de trading: onde estratégias pedem ordens e buckets limitam o gasto.

O lado da estratégia só conhece `protocol.py` (dados) e `client.py` (a
interface `TradeClient`); quem executa é o `TradeService` (`service.py`),
que o trade-runner serve por socket (`trader/execution/runner.py`) e o
backtest chama direto. Ver docs/plan.md §3.
"""
