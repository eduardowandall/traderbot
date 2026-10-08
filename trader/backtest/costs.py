"""Custos do backtest medidos na Jupiter agora (B9), no lugar de padrões fixos.

- **Taxa do par:** uma quote de compra do tamanho de trade da spec e uma de
  venda do que ela devolve. A perda da ida e volta é spread + taxas de pool +
  impacto de preço nesse tamanho; `fee_bps` por perna é metade dela. Num par
  líquido (SOL-USDC) dá perto de zero; num token fino, dezenas de bps.
- **Taxa de rede por perna:** a base (5000 lamports) mais o teto da priority
  fee da política (`max_priority_fee_lamports`), ao preço do SOL agora.

São os custos de agora, não os da época dos candles ou ticks reproduzidos.
Quem quer um backtest offline e repetível passa `--fee-bps` e
`--network-fee-usd`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from trader.execution.market.jupiter.quote import JupiterQuoteResponse
from trader.execution.models.mode import RunningMode
from trader.execution.trade.policy import load_policy
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import BASE_FEE_LAMPORTS, BPS, LAMPORTS_PER_SOL
from trader.shared.models.mints import SOL_MINT
from trader.strategy.spec.models import StrategySpec

ZERO = Decimal("0")
# precisão do que é medido: um centésimo de bp, um milionésimo de USD
BPS_STEP = Decimal("0.01")
USD_STEP = Decimal("0.000001")


class Quotes(Protocol):
    """O que a medição usa do `AsyncJupiterClient`."""

    async def get_quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50
    ) -> JupiterQuoteResponse: ...

    async def get_usd_prices(self, mints: list[str]) -> dict[str, Decimal]: ...


@dataclass(frozen=True)
class MeasuredCosts:
    size_usd: Decimal  # o tamanho de trade da spec
    round_trip_bps: Decimal  # perda de comprar e vender de volta, agora
    fee_bps: Decimal  # por perna: metade da ida e volta
    sol_usd: Decimal
    priority_fee_lamports: int  # o teto da política
    network_fee_usd: Decimal  # por perna: base + teto, ao preço do SOL


def network_fee_usd(priority_fee_lamports: int, sol_usd: Decimal) -> Decimal:
    """Taxa de rede de uma perna em USD: a base mais o teto da priority fee."""
    lamports = Decimal(BASE_FEE_LAMPORTS + priority_fee_lamports)
    return lamports / LAMPORTS_PER_SOL * sol_usd


async def measure_costs(
    client: Quotes, spec: StrategySpec, priority_fee_lamports: int
) -> MeasuredCosts:
    token, quote = SOLANA_MINTS.get_pair(spec.symbol)
    prices = await client.get_usd_prices(sorted({SOL_MINT, quote.mint}))
    sol_usd = _price(prices, SOL_MINT)
    quote_usd = Decimal(1) if quote.is_usd_stable else _price(prices, quote.mint)
    size_usd = spec.sizing.max_usd(spec.budget_usd)
    spent = quote.ui_to_raw(size_usd / quote_usd)
    if spent <= 0:
        raise ValueError(f"tamanho de trade {size_usd} USD pequeno demais para medir")
    buy = await client.get_quote(quote.mint, token.mint, spent)
    sell = await client.get_quote(token.mint, quote.mint, int(buy.outAmount))
    # devolver mais do que gastou (raro) não vira taxa negativa
    lost = max(ZERO, 1 - Decimal(int(sell.outAmount)) / Decimal(spent))
    network = network_fee_usd(priority_fee_lamports, sol_usd)
    return MeasuredCosts(
        size_usd=size_usd,
        round_trip_bps=(lost * BPS).quantize(BPS_STEP),
        fee_bps=(lost * BPS / 2).quantize(BPS_STEP),
        sol_usd=sol_usd,
        priority_fee_lamports=priority_fee_lamports,
        network_fee_usd=network.quantize(USD_STEP),
    )


def _price(prices: dict[str, Decimal], mint: str) -> Decimal:
    price = prices.get(mint)
    if not price:
        raise ValueError(f"Price API sem preço para {SOLANA_MINTS.symbol_of(mint)}")
    return price


async def resolve_costs(
    make_client: Callable[[], Any],
    spec: StrategySpec,
    fee_bps: Decimal | None,
    network_fee_usd: Decimal | None,
) -> tuple[Decimal, Decimal, MeasuredCosts | None]:
    """Os custos dados; o que faltar de taxa ou rede, medido agora.

    A priority fee é o teto da política real (o que um trade real pagaria).
    """
    if fee_bps is not None and network_fee_usd is not None:
        return fee_bps, network_fee_usd, None
    cap = load_policy(mode=str(RunningMode.REAL)).max_priority_fee_lamports
    client = make_client()
    try:
        measured = await measure_costs(client, spec, cap)
    finally:
        await client.aclose()
    return (
        measured.fee_bps if fee_bps is None else fee_bps,
        measured.network_fee_usd if network_fee_usd is None else network_fee_usd,
        measured,
    )
