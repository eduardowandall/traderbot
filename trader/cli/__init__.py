"""O CLI (Typer): `run` e `backtest`. `main.py` só importa `app`."""

import typer

from trader.cli.bot import backtest, run

# sem o traceback "bonito" do Typer: ele ignora a redação de segredos (a
# URL do Helius com a api-key vem encadeada em erros do httpx2)
app = typer.Typer(pretty_exceptions_enable=False)
app.command()(run)
app.command()(backtest)

__all__ = ["app"]
