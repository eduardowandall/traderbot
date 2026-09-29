"""Ajudantes da suíte live (fora do conftest: há dois módulos `conftest`)."""

import json

from typer.testing import CliRunner

import main as main_module

# limites folgados para o paper: as estratégias de teste gastam o saldo todo
LOOSE_PAPER_POLICY = """
[paper.limits]
max_trade_usd = 1000
max_daily_notional_usd = 100000
max_trades_per_hour = 100000
max_daily_loss_usd = 100000
"""


def invoke(*argv: str):
    """Roda o CLI no processo; devolve o resultado do CliRunner."""
    return CliRunner().invoke(main_module.app, list(argv))


def invoke_json(*argv: str) -> dict:
    result = invoke(*argv)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["ok"], data
    return data
