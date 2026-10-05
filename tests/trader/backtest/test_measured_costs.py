"""B9: a taxa do par e a de rede medidas na Jupiter, no lugar de padrões fixos."""

from decimal import Decimal

import pytest
from factories import make_spec

from trader.backtest.costs import measure_costs, network_fee_usd
from trader.shared.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.mints import SOL_MINT
from trader.shared.spec.models import StrategySpec

SOL = SOLANA_MINTS.get_by_symbol("SOL")
USDC = SOLANA_MINTS.get_by_symbol("USDC")


class Quotes:
    """Cada perna devolve `keep` do que entrou (unidades raw, sem preço)."""

    def __init__(self, keep="0.999", prices=None):
        self.keep = Decimal(keep)
        self.prices = {SOL_MINT: Decimal(200)} if prices is None else prices
        self.asked: list[tuple[str, str, int]] = []

    async def get_usd_prices(self, mints):
        return {m: p for m, p in self.prices.items() if m in mints}

    async def get_quote(self, input_mint, output_mint, amount, slippage_bps=50):
        self.asked.append((input_mint, output_mint, amount))
        out = int(amount * self.keep)
        return JupiterQuoteResponse.single_route(input_mint, amount, output_mint, out)


def _spec(**overrides) -> StrategySpec:
    return StrategySpec.model_validate(make_spec(**overrides))


async def test_half_the_round_trip_loss_is_the_fee_per_leg():
    quotes = Quotes(keep="0.999")  # 10 bps por perna

    measured = await measure_costs(quotes, _spec(), priority_fee_lamports=100_000)

    # compra do tamanho da spec (20 USDC), venda do que ela devolveu
    assert quotes.asked[0] == (USDC.mint, SOL.mint, USDC.ui_to_raw("20"))
    assert quotes.asked[1][2] == int(USDC.ui_to_raw("20") * Decimal("0.999"))
    assert measured.round_trip_bps == Decimal("19.99")
    assert measured.fee_bps == Decimal("10.00")  # 9.995, metade arredondada
    # (5000 + 100000) lamports a 200 USD/SOL
    assert measured.network_fee_usd == Decimal("0.021")


async def test_a_non_stable_quote_sizes_in_its_own_units():
    jup = SOLANA_MINTS.get_by_symbol("JUP")
    quotes = Quotes()

    await measure_costs(quotes, _spec(symbol="JUP-SOL"), priority_fee_lamports=1)

    # 20 USD a 200 USD/SOL: a compra gasta 0.1 SOL
    assert quotes.asked[0] == (SOL.mint, jup.mint, SOL.ui_to_raw("0.1"))


async def test_getting_back_more_than_spent_is_not_a_negative_fee():
    measured = await measure_costs(Quotes(keep="1.01"), _spec(), 1)
    assert measured.fee_bps == 0 and measured.round_trip_bps == 0


async def test_without_a_sol_price_it_refuses():
    with pytest.raises(ValueError, match="sem preço para SOL"):
        await measure_costs(Quotes(prices={}), _spec(), 1)


def test_network_fee_is_base_plus_the_cap_at_the_sol_price():
    assert network_fee_usd(0, Decimal(100)) == Decimal("0.0005")
    assert network_fee_usd(95_000, Decimal(100)) == Decimal("0.01")
