"""O CLI (Typer). `main.py` só importa `app`.

- `run <modo> spec.json`: uma spec, num processo só;
- `serve <modo>` + `connect spec.json`: várias specs, a chave num processo;
- `backtest spec.json`: a spec sobre candles ou ticks gravados.
"""

import typer

from trader.cli.bot import backtest, run
from trader.cli.runners import connect, serve

# sem o traceback "bonito" do Typer: ele ignora a redação de segredos (a
# URL do Helius com a api-key vem encadeada em erros do httpx2)
app = typer.Typer(pretty_exceptions_enable=False)
for command in (run, serve, connect, backtest):
    app.command()(command)

__all__ = ["app"]
