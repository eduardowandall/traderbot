"""`halt` e `resume`: o kill switch e o circuit breaker."""

import typer

from trader.execution import KillSwitch, TradeGateway
from trader.models.mode import RunningMode
from trader.policy import Policy
from trader.wiring import build_gateway


def halt(reason: str = typer.Argument("manual", help="Motivo da parada")):
    """Ativa o kill switch: nenhum trade executa até `resume`."""
    KillSwitch().activate(reason)
    for mode in RunningMode:
        # `Policy()` em vez do policy.toml: o kill switch precisa funcionar
        # mesmo com a política quebrada
        with TradeGateway.for_mode(mode, policy=Policy()) as gateway:
            gateway.add_event("halt", {"reason": reason})
    typer.echo(f"Kill switch ATIVO ({reason}).")


def resume(
    mode: RunningMode = typer.Argument(RunningMode.DRY, help="Ledger a rearmar"),
    note: str = typer.Option("", help="Observação para o ledger"),
):
    """Desativa o kill switch e rearma o circuit breaker do modo."""
    gateway = build_gateway(mode)
    with gateway:
        gateway.resume(note)
    typer.echo(f"Kill switch desativado; circuit breaker de {mode} rearmado.")
