"""De um fill para um `Order`, sem nunca levantar.

Roda depois de o swap estar EXECUTED: uma exceção aqui perderia o registro
de um trade que aconteceu (a conta compraria de novo, ou ficaria sem poder
vender). Por isso tudo tem um valor de reserva: valores efetivos -> valores
da quote -> ordem com quantidade 0 e um ERROR no log.

Convenção do `Order`: `input_mint` é o token de cotação e `output_mint` o
token negociado, nos dois lados; `quantity` é do token, `quote_amount` da
cotação.
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from trader.execution.fills import Fill
from trader.models import Mint, Order, OrderSide
from trader.models.costs import TradeRates, trade_rates, with_sol_usd
from trader.models.mints import SOL_MINT

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


def priced_mints(quote: Mint, token: Mint) -> set[str]:
    """O que pedir à Price API antes do trade: lados sem stablecoin e o SOL."""
    return {m.mint for m in (quote, token) if not m.is_usd_stable} | {SOL_MINT}


def order_from_fill(
    fill: Fill,
    quote: Mint,
    token: Mint,
    side: OrderSide,
    now: datetime,
    signal_price: Decimal | None = None,
    usd: Mapping[str, Decimal] | None = None,
    requested_quantity: Decimal | None = None,
) -> Order:
    usd = usd or {}
    quantity, quote_amount = _amounts(fill, quote, token)
    fill_price = quote_amount / quantity if quantity > 0 else ZERO
    price_usd = _token_usd(quote, token, fill_price, signal_price, usd)
    rates = _rates(quote, token, price_usd, fill_price, usd)
    return Order(
        order_id=fill.result.signature,
        input_mint=quote.mint,
        output_mint=token.mint,
        quantity=quantity,
        price=fill_price if price_usd is None else price_usd,
        side=side,
        timestamp=now,
        requested_quantity=requested_quantity,
        requested_price=signal_price,
        fill_price=fill_price,
        quote_amount=quote_amount,
        quote_usd=rates.quote_usd,
        sol_usd=rates.sol_usd,
        sol_in_quote=rates.sol_in_quote,
        costs=fill.costs,
    )


def _amounts(fill: Fill, quote: Mint, token: Mint) -> tuple[Decimal, Decimal]:
    """(quantidade do token, valor em cotação): efetivos, senão os da quote."""
    for in_raw, out_raw in (fill.amounts(), _quoted(fill)):
        token_raw, quote_raw = _by_side(fill, token, in_raw, out_raw)
        if token_raw > 0 and quote_raw > 0:
            return token.raw_to_ui(token_raw), quote.raw_to_ui(quote_raw)
        logger.warning(f"Fill sem valores efetivos válidos, usando a quote: {fill}")
    logger.error(f"Fill sem quantidade (swap já executado!): {fill}")
    return ZERO, ZERO


def _quoted(fill: Fill) -> tuple[int, int]:
    return fill.result.in_amount, fill.result.out_amount


def _by_side(fill: Fill, token: Mint, in_raw: int, out_raw: int) -> tuple[int, int]:
    # compra: recebe o token; venda: gasta o token
    if fill.result.output_mint == token.mint:
        return out_raw, in_raw
    return in_raw, out_raw


def _token_usd(
    quote: Mint,
    token: Mint,
    fill_price: Decimal,
    signal_price: Decimal | None,
    usd: Mapping[str, Decimal],
) -> Decimal | None:
    """USD por token: pelo trade quando há stablecoin, senão sinal ou API."""
    if quote.is_usd_stable:
        return fill_price
    if signal_price is not None:
        return signal_price  # o feed é em USD (mais fiel que "stable = 1")
    if token.is_usd_stable:
        return Decimal("1")
    quote_usd = usd.get(quote.mint)
    if quote_usd is not None:
        # coerente com o que o trade de fato pagou
        return quote_usd * fill_price
    return usd.get(token.mint)


def _rates(
    quote: Mint,
    token: Mint,
    price_usd: Decimal | None,
    fill_price: Decimal,
    usd: Mapping[str, Decimal],
) -> TradeRates:
    if price_usd is None or fill_price <= 0:
        return TradeRates(None, usd.get(SOL_MINT), None)
    rates = trade_rates(
        price_usd,
        fill_price,
        quote.is_usd_stable,
        quote.mint == SOL_MINT,
        token.mint == SOL_MINT,
    )
    return with_sol_usd(rates, usd.get(SOL_MINT))
