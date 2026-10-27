"""O CLI (Typer). `main.py` só importa `app`.

- `serve <modo>` + `connect spec.json`: a única forma de operar (B14); a chave
  fica num processo só, uma spec por `connect`;
- `stop spec.json` (ou `--all`): a parada manual, vende e não compra mais;
- `backtest spec.json`: a spec sobre candles ou ticks gravados.
"""

import typer

from trader.api.cli import backtest as backtest_module
from trader.api.cli.runners import connect, serve, stop

# sem o traceback "bonito" do Typer: ele ignora a redação de segredos (a
# URL do Helius com a api-key vem encadeada em erros do httpx2)
app = typer.Typer(pretty_exceptions_enable=False)
for command in (serve, connect, stop, backtest_module.backtest):
    app.command()(command)

__all__ = ["app"]
