"""`ledger list / verify / resolve`: consulta e manutenção do ledger.

Leem o `Ledger` direto, de propósito: são comandos do dono. `resolve` é a
única escrita (manutenção de intenções sem confirmação).
"""

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

import typer

from trader.execution.gateway import open_entry_of
from trader.execution.resolve import resolved_fill
from trader.ledger import Ledger, ledger_path
from trader.models import SOLANA_MINTS, Order
from trader.models.costs import PnLResult
from trader.models.intent import IntentSide, IntentStatus
from trader.models.mode import RunningMode
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.wiring import build_provider

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
    signature: str | None = typer.Option(
        None, help="Assinatura da transação, se a intenção não tem (ver o log)"
    ),
):
    """Resolve manualmente uma intenção sem confirmação."""
    try:
        parsed = IntentStatus(status)
    except ValueError as ex:
        raise typer.BadParameter("status deve ser executed ou failed") from ex
    _require_env(mode, parsed)
    with Ledger(ledger_path(mode)) as ledger:
        record = ledger.get(intent_id)
        if record is None:
            raise typer.BadParameter(f"intenção não encontrada: {intent_id}")
        if signature:
            record = replace(record, signature=signature)
        # rede antes de gravar: se o RPC falhar, nada muda e dá para repetir
        effects = asyncio.run(_prepare(ledger, mode, parsed, record))
        ledger.resolve(intent_id, parsed, note, signature=signature)
        _apply(ledger, intent_id, effects)
    typer.echo(f"Intenção {intent_id} resolvida como {parsed}.")


@dataclass(frozen=True)
class _Effects:
    """O que gravar depois da resolução, já lido da rede."""

    order: Order | None = None
    realized: Decimal | None = None
    pnl: PnLResult | None = None
    failed_fee: int | None = None


def _require_env(mode: RunningMode, status: IntentStatus) -> None:
    """Em modo real, a resolução lê a transação: confere o ambiente antes de gravar."""
    if mode != RunningMode.REAL:
        return
    needed = ["HELIUS_RPC_URL"]
    if status == IntentStatus.EXECUTED:
        needed.append("SOLANA_PRIVATE_KEY")
    missing = [name for name in needed if not os.getenv(name)]
    if missing:
        raise typer.BadParameter(
            f"defina {', '.join(missing)} (ex: uv run --env-file .env ...); "
            "nada foi gravado"
        )


async def _prepare(ledger: Ledger, mode, status, record) -> _Effects:
    if status == IntentStatus.EXECUTED:
        # a entrada precisa ser lida antes: resolvida, a venda vira a última perna
        entry = (
            open_entry_of(ledger, record.intent.account)
            if record.intent.side == IntentSide.SELL
            else None
        )
        order, realized, pnl = await _resolved_order(mode, record, entry)
        return _Effects(order, realized, pnl)
    if mode == RunningMode.REAL:
        # transações que falham também pagam taxa
        return _Effects(failed_fee=await _failed_fee(record))
    return _Effects()


async def _resolved_order(mode, record, entry):
    provider = build_provider(mode) if mode == RunningMode.REAL else None
    try:
        return await resolved_fill(
            record,
            entry,
            datetime.now(),
            provider.fetch_swap_costs if provider else None,
        )
    finally:
        if provider is not None:
            await provider.aclose()


async def _failed_fee(record) -> int | None:
    if not record.signature:
        return None
    rpc = AsyncRPCClient()
    try:
        tx = await rpc.get_confirmed_transaction(record.signature)
    finally:
        await rpc.aclose()
    meta = getattr(tx, "meta", None) if tx else None
    return None if meta is None else meta.fee


def _apply(ledger: Ledger, intent_id: str, effects: _Effects) -> None:
    if effects.order is not None:
        ledger.attach_order(intent_id, effects.order, effects.realized, effects.pnl)
        source = "da transação" if effects.order.costs else "estimada pela intenção"
        typer.echo(
            f"Ordem {source}: {effects.order.quantity} a {effects.order.fill_price}."
        )
    if effects.failed_fee is not None:
        ledger.record_failed_fee(intent_id, effects.failed_fee)
        typer.echo(
            f"Taxa da transação que falhou registrada: {effects.failed_fee} lamports."
        )


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
