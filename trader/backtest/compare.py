"""Ao vivo x backtest: os ticks de um `run --record-ticks` contra o bucket.

Reproduz os ticks gravados como o bot os viu: a estratégia aquece antes com os
candles fechados antes do primeiro tick (o bot faz o mesmo ao iniciar) e opera
nos ticks. Ao lado, as pernas que o bucket `<modo>:strategy:<spec_id>`
executou na mesma janela, lidas do ledger (só leitura), e as diferenças.

Limitação: o backtest começa sem posição e com o orçamento inteiro; compare
janelas em que o bucket também começou assim.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from trader.backtest.replay import BacktestTrade
from trader.backtest.spec import ReplayCosts, result_to_dict, spec_backtester
from trader.backtest.ticks import Tick
from trader.execution.ledger import Ledger
from trader.execution.models.intent import IntentRecord
from trader.shared.indicators import to_utc
from trader.shared.market import MarketData
from trader.shared.models import SOLANA_MINTS, OrderSide, TickerData
from trader.shared.models.costs import BPS
from trader.shared.models.order import order_from_json
from trader.shared.spec.models import StrategySpec

MAX_CANDLES = 1000  # o que a API de candles devolve por pedido


def _bar(ts: datetime, seconds: int) -> int:
    return int(to_utc(ts).timestamp()) // seconds


async def fetch_warmup(
    data: MarketData, spec: StrategySpec, before: datetime, now: datetime
) -> list[TickerData]:
    """Os `spec.history()` candles que fecharam antes de `before`."""
    seconds = spec.timeframe.seconds
    need = spec.history()
    if need <= 0:
        return []  # só condições de preço: nada a aquecer
    since = _bar(now, seconds) - _bar(before, seconds) + 1
    if need + since > MAX_CANDLES:
        raise ValueError(
            f"os ticks começam há {since} barras de {spec.timeframe}: a API não "
            f"devolve os {need} candles de aquecimento de antes deles"
        )
    token, _ = SOLANA_MINTS.get_pair(spec.symbol)
    candles = await data.get_candles(token.mint, spec.timeframe, need + since)
    first = _bar(before, seconds)
    closed = [c for c in candles if _bar(c.timestamp, seconds) < first]
    return sorted(closed, key=lambda c: to_utc(c.timestamp))[-need:]


async def compare_live(
    spec: StrategySpec,
    ticks: list[Tick],
    ledger: Ledger,
    mode: str,
    warmup: list[TickerData],
    costs: ReplayCosts = ReplayCosts(),
    seed: str = "0",
) -> dict:
    """O backtest dos ticks e o bucket ao vivo na mesma janela, com diferenças."""
    result = await spec_backtester(spec, ticks, costs, seed, warmup).run()
    account = f"{mode}:strategy:{spec.spec_id()}"
    # o fill chega logo depois do tick que o disparou: a janela vai até a
    # barra seguinte ao último tick
    start = ticks[0].timestamp
    end = ticks[-1].timestamp + timedelta(seconds=spec.timeframe.seconds)
    live = [_live_trade(r) for r in ledger.executed_between(account, start, end)]
    live_pnl = ledger.pnl_totals(account, start, end).net_usd
    live_costs = ledger.round_trip_costs(account, start, end)
    backtest = result_to_dict(result)
    return {
        "spec_id": spec.spec_id(),
        "account": account,
        "start": start,
        "end": end,
        "ticks": len(ticks),
        "warmup_candles": len(warmup),
        "costs": costs,
        "backtest": {
            "trades": result.trades,
            "realized_pnl": result.realized_pnl,
            "open_position": result.open_position,
            "return_pct": backtest["return_pct"],
            "round_trip_costs": backtest["round_trip_costs"],
        },
        "live": {
            "trades": live,
            "realized_pnl": live_pnl,
            "round_trip_costs": live_costs.as_dict(),
        },
        "diff": {
            "trades": len(live) - len(result.trades),
            "realized_pnl": live_pnl - result.realized_pnl,
            "fills": [
                _pair(lv, bt) for lv, bt in zip(live, result.trades, strict=False)
            ],
        },
    }


def _live_trade(record: IntentRecord) -> BacktestTrade:
    """Uma perna do ledger no formato das do backtest.

    O horário é o da intenção (UTC, o momento do sinal), não o da ordem:
    `Order.timestamp` vem do relógio local da conta, sem fuso.
    """
    intent = record.intent
    if record.order_json:
        order = order_from_json(record.order_json)
        return BacktestTrade(
            intent.created_at,
            order.side,
            order.quantity,
            order.price,
            record.realized_pnl_usd,
        )
    return BacktestTrade(
        intent.created_at,
        OrderSide(str(intent.side)),
        intent.quantity or Decimal("0"),
        intent.price or Decimal("0"),
        record.realized_pnl_usd,
    )


def _pair(live: BacktestTrade, replay: BacktestTrade) -> dict:
    """A n-ésima perna ao vivo contra a n-ésima do backtest."""
    price_bps = (
        (live.price - replay.price) / replay.price * BPS if replay.price else None
    )
    return {
        "same_side": live.side == replay.side,
        "seconds_apart": (
            to_utc(live.timestamp) - to_utc(replay.timestamp)
        ).total_seconds(),
        "price_diff_bps": price_bps,
        "pnl_diff": (
            None
            if live.realized_pnl is None or replay.realized_pnl is None
            else live.realized_pnl - replay.realized_pnl
        ),
    }
