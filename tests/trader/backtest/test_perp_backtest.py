"""O backtest de uma perp (A9): o motor do paper no replay.

Liquidação no caminho dos ticks, o empréstimo no relógio do replay, o
patrimônio com a posição aberta e o custo por ida e volta na exposição.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec

from trader.backtest import Tick
from trader.backtest.spec import ReplayCosts, spec_backtester
from trader.execution.trade.accounts.perp import PerpAccount
from trader.strategy.spec.models import StrategySpec

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NO_FEES = ReplayCosts(Decimal(0), Decimal(0), Decimal(0), Decimal(0))


def _spec(direction="short", leverage=3, stop=5, hold=600, **overrides):
    market = {"kind": "perp", "venue": "jupiter", "direction": direction}
    fields = {
        "market": market | {"leverage": leverage},
        # entra no primeiro tick e não volta (um trade por teste)
        "entry": {"conditions": [{"type": "random_chance", "pct": 100}]},
        "exit": {
            "stop": {"type": "stop_loss", "pct": stop},
            "conditions": [{"type": "max_hold", "minutes": hold}],
        },
        "sizing": {"type": "fixed_usd", "usd": 10},
        "budget_usd": 30,
        "max_loss_usd": 30,
        "cooldown_minutes": 10080,
        "timeframe": "1_MINUTE",
    }
    return StrategySpec.model_validate(make_spec(**fields | overrides))


def _ticks(*prices, step=timedelta(minutes=1)) -> list[Tick]:
    return [Tick(T0 + i * step, Decimal(str(p))) for i, p in enumerate(prices)]


async def test_a_gap_past_the_liquidation_price_liquidates_before_the_stop():
    # o stop (5%) estaria no caminho, mas o tick pula direto para +40%
    ticks = _ticks(100, 100, 140, 140, 140)
    result = await spec_backtester(_spec(), ticks, NO_FEES, "0").run()

    assert result.perp is not None and result.perp.liquidations == 1
    assert result.rent_usd == 0  # A19: uma perp não abre conta de token
    [entry, liquidation] = result.trades
    assert liquidation.liquidated and liquidation.price == 140
    # o colateral inteiro (10 USD) se foi; as taxas da abertura estavam nele
    assert liquidation.realized_pnl == Decimal(-10)
    assert result.final_equity == Decimal(20)
    assert not result.open_position


async def test_a_liquidated_leg_counts_its_borrow_but_no_close_fee():
    costs = ReplayCosts(Decimal(0), Decimal(0), Decimal(0), Decimal(60))
    ticks = _ticks(100, 100, 140)  # liquidada 2 minutos depois da entrada
    result = await spec_backtester(_spec(), ticks, costs, "0").run()

    assert result.perp is not None and result.perp.liquidations == 1
    # 30 de tamanho x 60 bps/h x 2 min
    assert result.perp.borrow_usd == pytest.approx(Decimal("0.006"))
    # só a taxa da abertura: na liquidação o colateral já se foi
    assert result.perp.fees_usd == Decimal("0.018009")


async def test_the_borrow_accrues_on_the_replay_clock():
    costs = ReplayCosts(Decimal(0), Decimal(0), Decimal(0), Decimal(10))
    # comprado 2x, preço parado: sai no max_hold de 120 minutos
    ticks = _ticks(*[100] * 130)
    spec = _spec("long", 2, hold=120)
    result = await spec_backtester(spec, ticks, costs, "0").run()

    assert result.perp is not None
    # 20 USD de tamanho x 10 bps/h x 2 h
    assert result.perp.borrow_usd == Decimal("0.04")
    # 0.06% de 20 + o impacto, nas duas pontas
    assert result.perp.fees_usd == pytest.approx(Decimal("0.024"), abs=Decimal("1e-4"))
    [_, exit_] = result.trades
    assert exit_.realized_pnl == pytest.approx(
        -(result.perp.borrow_usd + result.perp.fees_usd)
    )


async def test_equity_marks_the_open_perp_and_costs_are_on_the_exposure():
    # vendido 3x: o preço cai 2% e fica; a posição segue aberta no fim
    ticks = _ticks(100, 98, 98, 98)
    result = await spec_backtester(_spec(hold=600), ticks, NO_FEES, "0").run()

    assert result.open_position
    # 30 de tamanho x 2% = +0.6, menos as taxas de abrir e de fechar agora
    gain = Decimal("0.6") - 2 * Decimal("0.018")
    assert result.final_equity == pytest.approx(30 + gain, abs=Decimal("1e-3"))


async def test_a_round_trip_cost_is_measured_on_the_exposure():
    # vendido 3x, sai no stop: o custo é o das taxas, não o do movimento
    ticks = _ticks(100, 100, 106, 106)
    result = await spec_backtester(_spec(), ticks, NO_FEES, "0").run()

    costs = result.round_trip_costs
    assert costs.count == 1 and costs.notional_usd == 30
    # 2 x 0.06% de 30 (e um impacto ínfimo): ~12 bps, mesmo vendido
    assert Decimal(11) < costs.bps < Decimal(13)  # type: ignore[operator]


async def test_a_partial_take_profit_and_a_top_up_replay_like_live(monkeypatch):
    # A12: vendido 3x; -2% vende metade (uma vez); depois o preço sobe até
    # ficar a 20% da liquidação e entra colateral; o max_hold fecha o resto
    exit_ = {
        "stop": {"type": "stop_loss", "pct": 15},
        "conditions": [{"type": "max_hold", "minutes": 8}],
        "partial": {"pct": 50, "conditions": [{"type": "take_profit", "pct": 2}]},
    }
    market = {
        "kind": "perp",
        "venue": "jupiter",
        "direction": "short",
        "leverage": 3,
        "add_collateral": {"within_pct": 20, "usd": 2},
    }
    spec = _spec(exit=exit_, market=market)
    ticks = _ticks(100, 100, 97, 97, 110, 110, 110, 110, 110, 110, 110, 110)

    added = []
    add = PerpAccount.add_collateral

    async def counted(self, *args):
        order = await add(self, *args)
        added.append(order.quote_amount)
        return order

    monkeypatch.setattr(PerpAccount, "add_collateral", counted)
    result = await spec_backtester(spec, ticks, NO_FEES, "0").run()

    assert added == [2]  # uma vez (`max_times` 1), 2 USD
    sells = [t for t in result.trades if t.side == "sell"]
    assert len(sells) == 2 and not result.open_position
    half, rest = sells
    assert half.quantity == rest.quantity  # metade, depois o resto
    assert half.realized_pnl is not None and half.realized_pnl > 0
    # o resto perdeu ~10% em 15 de tamanho, e devolveu o colateral a mais
    assert rest.realized_pnl is not None and Decimal(-2) < rest.realized_pnl < 0


async def test_a_spot_spec_takes_its_partial_profit_too():
    # A12: o mesmo bloco no spot; a venda parcial passa pelo SpotAccount
    spec = StrategySpec.model_validate(
        make_spec(
            entry={"conditions": [{"type": "random_chance", "pct": 100}]},
            exit={
                "stop": {"type": "stop_loss", "pct": 10},
                "conditions": [{"type": "max_hold", "minutes": 4}],
                "partial": {
                    "pct": 40,
                    "conditions": [{"type": "take_profit", "pct": 2}],
                },
            },
            sizing={"type": "fixed_usd", "usd": 10},
            budget_usd=30,
            max_loss_usd=30,
            cooldown_minutes=10080,
            timeframe="1_MINUTE",
        )
    )
    ticks = _ticks(100, 100, 103, 103, 103, 103, 103, 103)

    result = await spec_backtester(spec, ticks, NO_FEES, "0").run()

    sells = [t for t in result.trades if t.side == "sell"]
    assert len(sells) == 2 and not result.open_position
    assert sells[0].quantity == pytest.approx(Decimal("0.04"))  # 40% de 0.1
    assert sells[1].quantity == pytest.approx(Decimal("0.06"))
