"""O CLI (Typer): monta os comandos do dono e os do agente num `app` só.

Cada grupo mora no seu módulo; `main.py` só importa `app` e configura o log.
Os comandos do agente (`market`, `strategy`, sempre em JSON) são de
`trader/agent_api/cli.py`.
"""

import typer

from trader.agent_api.cli import market_app, strategy_app
from trader.cli.bot import backtest, run
from trader.cli.ledger import ledger_app
from trader.cli.paper import paper_app
from trader.cli.pnl import pnl
from trader.cli.safety import halt, resume
from trader.cli.swap import swap

# sem o traceback "bonito" do Typer: ele ignora a redação de segredos (a
# URL do Helius com a api-key vem encadeada em erros do httpx2)
app = typer.Typer(pretty_exceptions_enable=False)
for command in (run, swap, halt, resume, backtest, pnl):
    app.command()(command)
app.add_typer(ledger_app, name="ledger")
app.add_typer(paper_app, name="paper")
# comandos de agente: sempre respondem em JSON (trader/agent_api/cli.py)
app.add_typer(market_app, name="market")
app.add_typer(strategy_app, name="strategy")

__all__ = ["app"]
