"""Swaps manuais: qualquer par, sem posição, no mesmo caminho dos buckets.

A intenção passa por `execute_trade` (gateway, depois custos) e o fill vira
um `Order` gravado com `gateway.record_fill`, então swaps manuais aparecem em
`pnl` e `ledger list` com os valores efetivos e os custos pagos.

No `Order` de um swap manual, `input_mint` é o token gasto e `output_mint`
o recebido (`side=BUY`: "gasta o input"); `quantity` é o recebido e
`quote_amount` o gasto.

Valores em USD: quando um dos lados é stablecoin, vêm do próprio trade; senão
da Price API (`usd`, um retrato tirado antes do trade). Sem nenhum dos dois,
ficam desconhecidos e `price` fica em unidades do token gasto.
"""

from datetime import datetime
from decimal import Decimal

from trader.execution import TradeGateway
from trader.execution.fills import Fill, execute_trade
from trader.market.prices import PriceOracle, usd_snapshot
from trader.models import SOLANA_MINTS, Mint, Order, OrderSide
from trader.models.costs import TradeRates, trade_rates, with_sol_usd
from trader.models.intent import IntentSide, TradeIntent, with_idempotency_key
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.trading_service.protocol import SwapRequest

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint


async def execute_swap(
    gateway: TradeGateway,
    provider: AsyncJupiterProvider,
    account: str,
    request: SwapRequest,
    source: str,
    now: datetime,
    prices: PriceOracle | None = None,
) -> Order:
    spend = SOLANA_MINTS[request.spend_mint]
    receive = SOLANA_MINTS[request.receive_mint]
    if spend.mint == receive.mint:
        raise ValueError("swap precisa de dois tokens diferentes")
    if request.amount <= 0:
        raise ValueError("a quantidade do swap deve ser maior que zero")
    # antes do trade: depois de EXECUTED nada pode falhar
    usd = await usd_snapshot(prices, _priced_mints(spend, receive))
    intent = _intent(account, request, source, spend, usd)
    raw_amount = spend.ui_to_raw(request.amount)
    fill = await execute_trade(
        gateway,
        provider,
        intent,
        lambda: provider.swap_with_details(
            spend.mint, receive.mint, raw_amount, request.slippage_bps
        ),
    )
    order = swap_order(fill, spend, receive, now, usd)
    gateway.record_fill(intent.intent_id, order)
    return order


def _priced_mints(spend: Mint, receive: Mint) -> set[str]:
    """Os lados que não são stablecoin, e o SOL dos custos."""
    return {m.mint for m in (spend, receive) if not m.is_usd_stable} | {SOL}


def _intent(
    account: str,
    request: SwapRequest,
    source: str,
    spend: Mint,
    usd: dict[str, Decimal],
) -> TradeIntent:
    rate = Decimal("1") if spend.is_usd_stable else usd.get(spend.mint)
    intent = TradeIntent(
        source=source,
        account=account,
        side=IntentSide.SWAP,
        spend_mint=spend.mint,
        receive_mint=request.receive_mint,
        spend_amount=request.amount,
        notional_usd=None if rate is None else request.amount * rate,
        rationale=request.rationale,
    )
    return with_idempotency_key(intent, request.idempotency_key)


def swap_order(
    fill: Fill,
    spend: Mint,
    receive: Mint,
    now: datetime,
    usd: dict[str, Decimal] | None = None,
) -> Order:
    """O fill de um swap manual como `Order` (valores efetivos quando conhecidos)."""
    usd = usd or {}
    in_raw, out_raw = fill.amounts()
    spent = spend.raw_to_ui(in_raw)
    received = receive.raw_to_ui(out_raw)
    if received <= 0:
        raise ValueError(f"Swap sem quantidade recebida: {fill.result}")
    fill_price = spent / received  # token gasto por token recebido
    price_usd = _received_usd(spend, receive, fill_price, usd)
    rates = _rates(spend, receive, price_usd, fill_price, usd)
    return Order(
        order_id=fill.result.signature,
        input_mint=spend.mint,
        output_mint=receive.mint,
        quantity=received,
        price=fill_price if price_usd is None else price_usd,
        side=OrderSide.BUY,
        timestamp=now,
        fill_price=fill_price,
        quote_amount=spent,
        quote_usd=rates.quote_usd,
        sol_usd=rates.sol_usd,
        sol_in_quote=rates.sol_in_quote,
        costs=fill.costs,
    )


def _received_usd(
    spend: Mint, receive: Mint, fill_price: Decimal, usd: dict[str, Decimal]
) -> Decimal | None:
    """USD por token recebido: pelo trade quando há stablecoin, senão a API.

    Sem stablecoin, prefere o preço do token gasto x o preço do fill, que é
    coerente com o que o trade de fato pagou.
    """
    if receive.is_usd_stable:
        return Decimal("1")
    if spend.is_usd_stable:
        return fill_price
    spend_usd = usd.get(spend.mint)
    if spend_usd is not None:
        return spend_usd * fill_price
    return usd.get(receive.mint)


def _rates(
    spend: Mint,
    receive: Mint,
    price_usd: Decimal | None,
    fill_price: Decimal,
    usd: dict[str, Decimal],
) -> TradeRates:
    if price_usd is None:
        return TradeRates(None, None, None)
    rates = trade_rates(
        price_usd,
        fill_price,
        spend.is_usd_stable,
        spend.mint == SOL,
        receive.mint == SOL,
    )
    return with_sol_usd(rates, usd.get(SOL))
