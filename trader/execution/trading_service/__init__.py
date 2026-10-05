"""Serviço de trading: onde estratégias pedem ordens e buckets limitam o gasto.

O lado da estratégia só conhece `protocol.py` (dados) e `client.py` (a
interface `TradeClient`); quem executa é o `TradeService` (`service.py`),
acessado no mesmo processo por `LocalTradeClient` (`local.py`) ou, no item B3,
por socket. Ver docs/plan.md §3.
"""
