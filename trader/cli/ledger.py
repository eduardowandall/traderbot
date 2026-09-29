"""`ledger list / verify / resolve`: consulta e manutenção do ledger.

Leem o `Ledger` direto, de propósito: são comandos do dono. `resolve` é a
única escrita (manutenção de intenções sem confirmação).
"""

import asyncio
from decimal import Decimal

import typer

from trader.ledger import Ledger, ledger_path
from trader.models import SOLANA_MINTS
from trader.models.intent import IntentStatus
from trader.models.mode import RunningMode
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient

ledger_app = typer.Typer(help="Consulta e manutenção do ledger de trades.")


@ledger_app.command("list")
def ledger_list(
    mode: RunningMode = typer.Argument(RunningMode.DRY),
    limit: int = typer.Option(20, min=1),
):
    """Lista as intenções mais recentes."""
    with Ledger(ledger_path(mode)) as ledger:
        for record in ledger.list_intents(limit):
            intent = record.intent
            typer.echo(
                f"{intent.created_at:%Y-%m-%d %H:%M:%S} {intent.intent_id[:8]} "
                f"{record.status:<11} {intent.side:<4} {intent.spend_amount} "
                f"{SOLANA_MINTS.symbol_of(intent.spend_mint)} -> "
                f"{SOLANA_MINTS.symbol_of(intent.receive_mint)}"
                f" [{intent.source}]{_record_costs(record)}{_record_pnl(record)}"
                f"{_repeats(record)}"
            )
            for reason in record.decision_reasons:
                typer.echo(f"    recusa: {reason}")
            if record.error:
                typer.echo(f"    erro: {record.error}")


@ledger_app.command("verify")
def ledger_verify(mode: RunningMode = typer.Argument(RunningMode.DRY)):
    """Confere a cadeia de hashes dos eventos."""
    with Ledger(ledger_path(mode)) as ledger:
        bad = ledger.verify_chain()
    if bad is not None:
        typer.echo(f"Ledger ADULTERADO a partir do evento {bad}.")
        raise typer.Exit(1)
    typer.echo("Ledger íntegro.")


@ledger_app.command("resolve")
def ledger_resolve(
    mode: RunningMode = typer.Argument(...),
    intent_id: str = typer.Argument(..., help="Id (completo) da intenção"),
    status: str = typer.Argument(..., help="executed ou failed"),
    note: str = typer.Option(..., help="Como foi verificado (ex: explorer)"),
):
    """Resolve manualmente uma intenção sem confirmação."""
    try:
        parsed = IntentStatus(status)
    except ValueError as ex:
        raise typer.BadParameter("status deve ser executed ou failed") from ex
    with Ledger(ledger_path(mode)) as ledger:
        ledger.resolve(intent_id, parsed, note)
        if parsed == IntentStatus.FAILED and mode == RunningMode.REAL:
            # transações que falham também pagam taxa
            asyncio.run(_backfill_failed_fee(ledger, intent_id))
    typer.echo(f"Intenção {intent_id} resolvida como {parsed}.")


async def _backfill_failed_fee(ledger: Ledger, intent_id: str) -> None:
    record = ledger.get(intent_id)
    if record is None or not record.signature:
        return
    rpc = AsyncRPCClient()
    try:
        tx = await rpc.get_confirmed_transaction(record.signature)
    finally:
        await rpc.aclose()
    meta = getattr(tx, "meta", None) if tx else None
    if meta is None:
        typer.echo("Transação não encontrada: nenhuma taxa foi paga.")
        return
    ledger.record_failed_fee(intent_id, meta.fee)
    typer.echo(f"Taxa da transação que falhou registrada: {meta.fee} lamports.")


def _record_costs(record) -> str:
    if record.fee_lamports is None:
        return ""
    paid = (record.fee_lamports or 0) + (record.rent_lamports or 0)
    paid += record.other_lamports or 0
    return f" custos={Decimal(paid) / Decimal(10**9):.9f}SOL[{record.costs_source}]"


def _repeats(record) -> str:
    # recusas idênticas seguidas ficam numa linha só
    return f" (x{record.repeat_count + 1})" if record.repeat_count else ""


def _record_pnl(record) -> str:
    if record.realized_pnl_usd is None:
        return ""
    text = f" pnl~${record.realized_pnl_usd:+.4f}"
    if record.net_pnl_quote is not None and record.quote_mint:
        text += (
            f" líquido={record.net_pnl_quote:+.6f}"
            f"{SOLANA_MINTS.symbol_of(record.quote_mint)}"
        )
    return text
