"""Camada de serviço para agentes: dados de mercado e specs de estratégia.

As funções daqui devolvem dados simples (dicts serializáveis). A CLI
(`cli.py`, montada em `main.py market ...` / `main.py strategy ...`) só as
embrulha em JSON; um servidor MCP futuro pode reutilizá-las do mesmo jeito.
Nada aqui assina, envia transações ou escreve no ledger.
"""
