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
    if result.get("rent_usd"):
        lines.insert(-1, f"  {_rent_line(result)}")
    if result.get("window"):
        lines.insert(2, f"  {_window_line(result)}")
    return "\n".join(lines + _trade_lines(result["trades"]))


def _window_line(result: dict) -> str:
    """A janela pedida (A34) e quantas barras dela a API tinha."""
    window, cover = result["window"], result["coverage"]
    return (
        f"janela (UTC): {_when(window['from'])} -> {_when(window['to'])}, "
        f"{cover['bars']}/{cover['expected']} barras com candle "
        f"({cover['pct']:.1f}%)"
    )


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


def _rent_line(result: dict) -> str:
    """O rent da conta do token (A19): pago na primeira compra, de volta se o
    replay acabou sem posição (menos a taxa do fechamento)."""
    return (
        f"rent da conta do token: {result['rent_usd']:.4f} USD pago, "
        f"{result['rent_refund_usd']:.4f} USD de volta"
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
        account_rent_usd=costs["account_rent_usd"],
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


def scenarios_summary(run: dict) -> str:
    """Um run de cenários (A35): uma linha por janela e o resumo."""
    summary = run["summary"]
    lines = [
        f"Cenários {run['name']} ({run['spec_id']}) {run['symbol']} "
        f"{run['timeframe']}: {summary['ran']} de {summary['windows']} janela(s)"
    ]
    for row in run["windows"]:
        lines.extend(_scenario_lines(row))
    ran = [row for row in run["windows"] if "error" not in row]
    if ran:
        lines.append(_costs_line(ran[0]["result"]))
        lines.append(f"  {_scenarios_total(summary)}")
    if run.get("measured_costs"):
        lines.append(f"  {_measured_line(run['measured_costs'])}")
    return "\n".join(lines)


def _scenario_lines(row: dict) -> list[str]:
    if "error" in row:
        return [f"  {row['id']:<20} erro: {row['error']}"]
    result, stats = row["result"], row["stats"]
    line = (
        f"  {row['id']:<20} {float(result['return_pct']):+7.2f}%  "
        f"dd {float(result['max_drawdown_pct']):5.2f}%  "
        f"{result['closed_trades']:>3} trade(s)  par {stats['return_pct']:+6.1f}%  "
        f"vs segurar {row['vs_hold_pct']:+6.1f}%"
    )
    return [line, *(f"    {flag}" for flag in row["flags"])]


def _scenarios_total(s: dict) -> str:
    return (
        f"resumo: mediana {s['median_return_pct']:+.2f}%, pior "
        f"{s['worst_return_pct']:+.2f}% ({s['worst_id']}), drawdown máximo "
        f"{s['worst_drawdown_pct']:.2f}%, {s['profitable_pct']:.0f}% com lucro, "
        f"{s['trades']} trade(s), mediana vs segurar {s['median_vs_hold_pct']:+.2f}%"
    )


def matrix_summary(run: dict) -> str:
    """Variantes x janelas (A36): uma linha por variante na ordem do ranking,
    depois as janelas da melhor."""
    best = run["variants"][0]
    lines = [
        f"Variantes {run['name']} ({run['spec_id']}) {run['symbol']} "
        f"{run['timeframe']}: {len(run['variants'])} variante(s), "
        f"{best['summary']['windows']} janela(s) no ranking"
    ]
    lines += [_variant_line(i, v) for i, v in enumerate(run["variants"], start=1)]
    lines.append(f"  janelas da melhor ({_changes(best['changes'])}):")
    seeded = len({r["seed"] for r in best["windows"] if "seed" in r}) > 1
    stressed = bool(best["stress"])
    for row in best["windows"]:
        head, *notes = _scenario_lines(row)
        seed = f" semente {row['seed']}" if seeded else ""
        stress = f" [{row['stress']}]" if stressed and "stress" in row else ""
        held = " [holdout]" if "holdout" in row["tags"] else ""
        lines += [f"  {head}{seed}{stress}{held}", *(f"  {note}" for note in notes)]
    ran = [row for row in best["windows"] if "error" not in row]
    if ran:
        lines.append(_costs_line(ran[0]["result"]))
    if run.get("measured_costs"):
        lines.append(f"  {_measured_line(run['measured_costs'])}")
    return "\n".join(lines)


def _variant_line(rank: int, variant: dict) -> str:
    head = f"  {rank:>2}. {variant['spec_id']} {_changes(variant['changes']):<28}"
    s = variant["summary"]
    if not s["ran"]:
        return f"{head} nenhuma janela rodou"
    held = variant["holdout"]
    holdout = (
        "-" if not held or not held["ran"] else f"{held['median_return_pct']:+.2f}%"
    )
    return (
        f"{head} mediana {s['median_return_pct']:+.2f}%, pior "
        f"{s['worst_return_pct']:+.2f}% ({s['worst_id']}), "
        f"{s['profitable_pct']:.0f}% com lucro, {s['trades']} trade(s); "
        f"holdout {holdout}{_stress_text(variant)}"
    )


def _stress_text(variant: dict) -> str:
    """A mediana em cada estresse (A38) e o aviso de frágil."""
    parts = [
        f"{name} {s['median_return_pct']:+.2f}%" if s["ran"] else f"{name} -"
        for name, s in variant["stress"].items()
    ]
    text = f"; {', '.join(parts)}" if parts else ""
    return f"{text} [!] frágil" if variant["fragile"] else text


def _changes(changes: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in changes.items()) or "base"
