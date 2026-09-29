"""`pnl`: PnL líquido e custos por conta (bucket), lidos do ledger."""

from decimal import Decimal

import typer

from trader.ledger import Ledger, ledger_path
from trader.models.mode import RunningMode


def pnl(
    mode: RunningMode = typer.Argument(RunningMode.PAPER),
    account: str | None = typer.Option(None, help="Só esta conta (ex: paper:SOL-USDC)"),
):
    """PnL líquido por conta: nativo (token de cotação), custos em SOL e ~USD."""
    with Ledger(ledger_path(mode)) as ledger:
        report = ledger.pnl_report(account)
    if not report:
        typer.echo("Nenhum trade executado.")
        return
    for entry in report.values():
        typer.echo(_format_account_pnl(entry))
    typer.echo(_format_totals(report.values()))


def _format_account_pnl(entry) -> str:
    sym = entry.quote_symbol
    lines = [
        f"{entry.account}: {entry.trades} pernas, {entry.closed} posições fechadas",
        f"  bruto   {entry.gross_quote:+.6f} {sym}",
        f"  custos  {entry.costs_sol:.9f} SOL (das posições fechadas)",
        f"  líquido {entry.net_quote:+.6f} {sym}   (~${entry.net_usd:+.4f})",
        f"  pago em SOL: taxas {Decimal(entry.fee_lamports) / 10**9:.9f} "
        f"(priority {Decimal(entry.priority_fee_lamports) / 10**9:.9f}), "
        f"rent {Decimal(entry.rent_lamports) / 10**9:.9f} (reembolsável), "
        f"outros {Decimal(entry.other_lamports) / 10**9:.9f}, "
        f"txs com falha {Decimal(entry.failed_fee_lamports) / 10**9:.9f}",
    ]
    if entry.incomplete or entry.unknown_costs:
        lines.append(
            f"  [!] {entry.incomplete} posição(ões) sem custos convertidos, "
            f"{entry.unknown_costs} perna(s) sem custos reais"
        )
    return "\n".join(lines)


def _format_totals(entries) -> str:
    by_quote: dict[str, Decimal] = {}
    usd = Decimal("0")
    paid = Decimal("0")
    for entry in entries:
        by_quote[entry.quote_symbol] = (
            by_quote.get(entry.quote_symbol, Decimal("0")) + entry.net_quote
        )
        usd += entry.net_usd
        paid += entry.paid_sol
    native = ", ".join(f"{net:+.6f} {sym}" for sym, net in by_quote.items())
    return f"TOTAL líquido: {native} (~${usd:+.4f}); pago em SOL: {paid:.9f}"
