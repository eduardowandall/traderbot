"""`paper balance / reset`: a carteira simulada do modo paper."""

import typer

from trader.execution import TradeGateway
from trader.models import SOLANA_MINTS
from trader.models.mode import RunningMode
from trader.paper import DEFAULT_PAPER_BALANCES, SimulatedWallet, parse_balances
from trader.policy import Policy
from trader.wiring import paper_wallet_path

paper_app = typer.Typer(help="Carteira simulada do modo paper.")


@paper_app.command("balance")
def paper_balance():
    """Mostra os saldos da carteira simulada."""
    wallet = SimulatedWallet(paper_wallet_path())
    if wallet.is_empty:
        typer.echo("Carteira paper vazia (use `main.py paper reset`).")
        return
    for mint, amount in wallet.balances().items():
        typer.echo(f"{SOLANA_MINTS.symbol_of(mint):>8} {amount}")


@paper_app.command("reset")
def paper_reset(
    balances: str = typer.Argument(
        " ".join(f"{s}={v}" for s, v in DEFAULT_PAPER_BALANCES.items()),
        help="Saldos iniciais, ex: 'USDC=100 SOL=0.5'",
    ),
):
    """Recria a carteira simulada com os saldos informados."""
    try:
        parsed = parse_balances(balances)
    except ValueError as ex:
        raise typer.BadParameter(str(ex)) from ex
    SimulatedWallet(paper_wallet_path()).reset(parsed)
    with TradeGateway.for_mode(RunningMode.PAPER, policy=Policy()) as gateway:
        gateway.add_event("paper_wallet_reset", {"balances": parsed})
    typer.echo(f"Carteira paper: {parsed}")
    typer.echo(
        "Atenção: posições abertas no ledger paper não são apagadas; "
        "o bot vai sinalizar a diferença ao iniciar."
    )
