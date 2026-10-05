"""Replays de pares sem stablecoin (B6): duas séries, orçamento e PnL em USD."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import StubStrategy, make_spec

from trader.backtest import Backtester, Tick, TickRecorder, load_ticks
from trader.backtest.spec import backtest_spec, fetch_ticks
from trader.models import SOLANA_MINTS, OrderSide, OrderSignal, TickerData
from trader.strategy_spec.models import StrategySpec

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
JUP = SOLANA_MINTS.get_by_symbol("JUP").mint
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class BuyFirstSellThird(StubStrategy):
    def __init__(self):
        super().__init__()
        self.tick = 0
        self.balances = []

    def on_market_refresh(
        self, price, balance, current_position, quote_usd: Decimal | None = Decimal(1)
    ):
        self.tick += 1
        self.balances.append((balance, quote_usd))
        if self.tick == 1:
            return OrderSignal(OrderSide.BUY, balance / price)
        if self.tick == 3 and current_position:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        return None


def _ticks(points):
    return [
        Tick(T0 + timedelta(minutes=i), Decimal(price), Decimal(quote_usd))
        for i, (price, quote_usd) in enumerate(points)
    ]


async def test_jup_sol_pays_in_sol_and_measures_in_usd():
    # 100 USD a 200 USD/SOL = 0.5 SOL -> 100 JUP a 0.005; vende a 0.006 SOL
    # com o SOL a 210: 0.6 SOL = 126 USD
    strategy = BuyFirstSellThird()
    result = await Backtester(
        strategy,
        "JUP-SOL",
        _ticks([("0.005", "200"), ("0.0055", "200"), ("0.006", "210")]),
        initial_balance=Decimal(100),
        fee_bps=Decimal(0),
        slippage_bps=Decimal(0),
        budget_usd=Decimal(100),
    ).run()

    # a estratégia vê o disponível em SOL e o preço USD do SOL
    assert strategy.balances[0] == (Decimal("0.5"), Decimal("200"))
    buy, sell = result.trades
    assert buy.quantity == Decimal(100)
    assert buy.price == Decimal(1)  # USD por JUP: 0.005 SOL x 200
    assert result.final_equity == Decimal(126)
    assert result.realized_pnl == Decimal(26)  # 126 USD - 100 USD
    assert result.return_pct == Decimal(26)


async def test_the_network_fee_is_paid_in_usd_on_a_sol_quote():
    fee = Decimal(2)  # USD por perna = 0.01 SOL a 200
    result = await Backtester(
        BuyFirstSellThird(),
        "JUP-SOL",
        _ticks([("0.005", "200"), ("0.005", "200"), ("0.005", "200")]),
        initial_balance=Decimal(100),
        fee_bps=Decimal(0),
        slippage_bps=Decimal(0),
        budget_usd=Decimal(100),
        network_fee_usd=fee,
    ).run()
    # compra: 100 JUP - 2 JUP (2 USD a 1 USD/JUP); venda: 98 x 0.005 - 0.01 SOL
    assert (
        result.final_equity == (Decimal(98) * Decimal("0.005") - Decimal("0.01")) * 200
    )


class TwoSeries:
    def __init__(self):
        self.asked = []

    async def get_candles(self, mint, interval, candle_qty):
        self.asked.append(mint)
        price = {JUP: Decimal(1), SOL: Decimal(200)}[mint]
        return [
            TickerData(T0 + timedelta(minutes=m), price, price, price, price)
            for m in range(candle_qty)
        ]

    async def get_price(self, mint):
        raise AssertionError("não usado")

    async def aclose(self):
        return None


async def test_candles_of_both_mints_make_ratio_ticks_with_the_quote_usd():
    spec = StrategySpec.model_validate(make_spec(symbol="JUP-SOL"))
    data = TwoSeries()

    ticks = await fetch_ticks(data, spec, 30)

    assert data.asked == [JUP, SOL]
    assert ticks and all(t.price == Decimal("0.005") for t in ticks)
    assert all(t.quote_usd == Decimal(200) for t in ticks)
    result = await backtest_spec(spec, ticks)  # o motor aceita o par
    assert result["symbol"] == "JUP-SOL"


def test_recorded_ticks_keep_the_quote_usd_column(tmp_path):
    path = tmp_path / "ticks.csv"
    with TickRecorder(path) as recorder:
        recorder.record(T0, Decimal("0.005"), Decimal("200"))
        recorder.record(T0 + timedelta(seconds=1), Decimal("101"))  # par USDC
    first, second = load_ticks(path)
    assert (first.price, first.quote_usd) == (Decimal("0.005"), Decimal("200"))
    assert second.quote_usd is None
