"""Provider de paper trading: quotes reais da Jupiter, execução simulada.

É o mesmo `AsyncJupiterProvider` do modo real (conversão de unidades, teto
de impacto de preço, re-tentativas); só o executor muda.
"""

from decimal import Decimal

from trader.models.costs import SIMULATED
from trader.paper.executor import (
    DEFAULT_ACCOUNT_RENT_LAMPORTS,
    DEFAULT_FEE_LAMPORTS,
    SimulatedExecutor,
)
from trader.paper.wallet import SimulatedWallet
from trader.providers.jupiter.async_jupiter_svc import (
    DEFAULT_MAX_PRICE_IMPACT_PCT,
    DEFAULT_MAX_SLIPPAGE_BPS,
    AsyncJupiterProvider,
)


def paper_provider(
    wallet: SimulatedWallet,
    jupiter_client=None,  # AsyncJupiterClient ou substituto (replay)
    max_price_impact_pct: Decimal | None = DEFAULT_MAX_PRICE_IMPACT_PCT,
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
    fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    account_rent_lamports: int = DEFAULT_ACCOUNT_RENT_LAMPORTS,
    cost_source: str = SIMULATED,
) -> AsyncJupiterProvider[SimulatedExecutor]:
    executor = SimulatedExecutor(
        wallet, fee_lamports, account_rent_lamports, cost_source
    )
    return AsyncJupiterProvider(
        executor, jupiter_client, max_price_impact_pct, max_slippage_bps
    )
