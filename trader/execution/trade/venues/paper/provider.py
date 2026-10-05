"""Provider de paper trading: quotes reais da Jupiter, execução simulada.

É o mesmo `AsyncJupiterProvider` do modo real (conversão de unidades, teto
de impacto de preço, re-tentativas); só o executor muda.
"""

from decimal import Decimal

from trader.execution.trade.venues.jupiter.async_jupiter_svc import (
    DEFAULT_MAX_PRICE_IMPACT_PCT,
    DEFAULT_MAX_SLIPPAGE_BPS,
    AsyncJupiterProvider,
)
from trader.execution.trade.venues.paper.executor import (
    DEFAULT_ACCOUNT_RENT_LAMPORTS,
    DEFAULT_FEE_LAMPORTS,
    DEFAULT_SLIPPAGE_BPS,
    SimulatedExecutor,
)
from trader.execution.trade.venues.paper.wallet import SimulatedWallet
from trader.shared.models.costs import SIMULATED


def paper_provider(
    wallet: SimulatedWallet,
    jupiter_client=None,  # AsyncJupiterClient ou substituto (testes)
    max_price_impact_pct: Decimal | None = DEFAULT_MAX_PRICE_IMPACT_PCT,
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
    fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    account_rent_lamports: int = DEFAULT_ACCOUNT_RENT_LAMPORTS,
    cost_source: str = SIMULATED,
    # o fill sai abaixo da quote (no mínimo o `otherAmountThreshold` dela)
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    max_quote_deviation_pct: Decimal | None = None,
    # o teto da política, cobrado inteiro em cada perna (0 nos replays)
    priority_fee_lamports: int = 0,
) -> AsyncJupiterProvider[SimulatedExecutor]:
    executor = SimulatedExecutor(
        wallet,
        fee_lamports,
        account_rent_lamports,
        cost_source,
        slippage_bps,
        priority_fee_lamports,
    )
    return AsyncJupiterProvider(
        executor,
        jupiter_client,
        max_price_impact_pct,
        max_slippage_bps,
        max_quote_deviation_pct,
    )
