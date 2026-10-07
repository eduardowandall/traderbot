"""Saída do `backtest`: um resumo legível (padrão) ou um objeto JSON (`--json`).

JSON: sucesso `{"ok": true, ...}`, erro `{"ok": false, "errors": [{"path",
"msg"}]}` com código de saída 1. Decimais saem como string (sem perda de
precisão) e o texto é ASCII (`ensure_ascii`), então funciona no console cp1252
do Windows. No modo texto, os erros vão para o stderr, também com código 1.
"""

import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import typer

from trader.shared.models.costs import BASE_FEE_LAMPORTS, RoundTripCosts
from trader.shared.models.perp import PERP_FEE_RATE
from trader.strategy.spec.parse import SpecParseError

# trades listados no resumo (o JSON traz todos)
SHOWN_TRADES = 10


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        # notação simples e normalizada: "0", não "0E-26"; "1.5", não "1.50"
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"não serializável: {type(value).__name__}")


def dumps(payload: dict) -> str:
    return json.dumps(payload, default=_default, ensure_ascii=True, sort_keys=True)


def emit(payload: dict) -> None:
    typer.echo(dumps({"ok": True, **payload}))


def errors_of(ex: Exception) -> list[dict]:
    """O erro como lista `[{"path", "msg"}]`."""
    if isinstance(ex, SpecParseError):
        return [{"path": e.path, "msg": e.msg} for e in ex.errors]
    if isinstance(ex, ValueError | OSError):
        return [{"path": "", "msg": str(ex)}]
    # rede, API, qualquer outra coisa: sem traceback, só a causa
    return [{"path": "", "msg": f"{type(ex).__name__}: {ex}"}]


def fail(ex: Exception, as_json: bool) -> None:
    """Imprime os erros (JSON no stdout, ou texto no stderr) e sai com código 1."""
    errors = errors_of(ex)
    if as_json:
        typer.echo(dumps({"ok": False, "errors": errors}))
    else:
        for e in errors:
            where = f"{e['path']}: " if e["path"] else ""
            typer.echo(f"erro: {where}{e['msg']}", err=True)
    raise typer.Exit(1)


def backtest_summary(result: dict) -> str:
    """O resultado de `backtest_spec` em poucas linhas, para quem lê."""
    win = result["win_rate_pct"]
    lines = [
        f"Backtest {result['name']} ({result['spec_id']}) {result['symbol']} "
        f"{result['timeframe']}: {result['bars']} barras "
        f"(aquecimento {result['warmup_bars']}), {result['ticks']} ticks",
        f"  período (UTC): {_when(result['start'])} -> {_when(result['end'])}",
        f"  patrimônio: {result['initial_equity']:.2f} -> "
        f"{result['final_equity']:.2f} USD ({result['return_pct']:+.2f}%), "
        f"drawdown máximo {result['max_drawdown_pct']:.2f}%",
        f"  PnL realizado: {result['realized_pnl']:+.4f} USD em "
        f"{result['closed_trades']} trade(s) fechado(s), win rate "
        f"{'-' if win is None else f'{win:.1f}%'}",
        f"  sinais recusados: {result['rejected_signals']}; posição aberta no fim: "
        f"{'sim' if result['open_position'] else 'não'}",
        _costs_line(result),
        f"  {_round_trip_line(result['round_trip_costs'])}",
    ]
    if result.get("measured_costs"):
        lines.insert(-1, f"  {_measured_line(result['measured_costs'])}")
    return "\n".join(lines + _trade_lines(result["trades"]))


def _costs_line(result: dict) -> str:
    perp = result.get("perp")
    network = f"{result['network_fee_usd']} USD de rede por perna"
    if perp is None:
        return (
            f"  custos: {result['fee_bps']} bps de taxa + {result['slippage_bps']} "
            f"bps de slippage + {network}"
        )
    # A9: o motor de perps cobra as taxas dele; a do par não se aplica
    return (
        f"  perp {perp['direction']} {perp['leverage']}x: taxas ${perp['fees_usd']:.4f}"
        f" ({(PERP_FEE_RATE * 100).normalize()}% + impacto por ponta), empréstimo "
        f"${perp['borrow_usd']:.4f} "
        f"({perp['borrow_bps_hour']} bps/h), {perp['liquidations']} liquidação(ões);"
        f" {network}"
    )


def _measured_line(m: dict) -> str:
    """De onde vieram os custos padrão (B9): a Jupiter agora."""
    return (
        f"medido agora: ida e volta de {m['size_usd']:.2f} USD perde "
        f"{m['round_trip_bps']:.1f} bps; rede {BASE_FEE_LAMPORTS} + {m['priority_fee_lamports']} "
        f"lamports a {m['sol_usd']:.2f} USD/SOL"
    )


def _round_trip_line(costs: dict) -> str:
    return RoundTripCosts(
        count=costs["count"],
        notional_usd=costs["notional_usd"],
        cost_usd=costs["cost_usd"],
    ).describe()


def _trade_lines(trades: list[dict]) -> list[str]:
    if not trades:
        return ["  nenhum trade (as condições de entrada não dispararam)"]
    lines = ["  trades:"]
    for t in trades[:SHOWN_TRADES]:
        pnl = t["realized_pnl"]
        suffix = "" if pnl is None else f"  pnl {pnl:+.4f} USD"
        if t.get("liquidated"):
            suffix += "  [!] LIQUIDADA"
        lines.append(
            f"    {_when(t['timestamp'])}  {t['side']:<4} {t['quantity']:.6f} "
            f"@ {t['price']:.4f}{suffix}"
        )
    if len(trades) > SHOWN_TRADES:
        lines.append(f"    ... e mais {len(trades) - SHOWN_TRADES} (veja --json)")
    return lines


def _when(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")
