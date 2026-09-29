"""`swap`: um swap manual, no bucket `manual` do modo."""

import asyncio

import typer

from trader.cli.common import parse_decimal, warn
from trader.models import SOLANA_MINTS, Order
from trader.models.costs import describe_costs
from trader.models.mode import RunningMode
from trader.providers.jupiter.async_jupiter_svc import DEFAULT_MAX_PRICE_IMPACT_PCT
from trader.trading_service.protocol import OrderReply, SwapRequest
from trader.trading_service.service import TradeService
from trader.wiring import build_trade_service


def swap(
    mode: RunningMode = typer.Argument(
        RunningMode.DRY, help="Modo de execucão do bot."
    ),
    symbol_in: str = typer.Argument(..., help="Symbol to spend (ex: SOL)"),
    symbol_out: str = typer.Argument(..., help="Symbol to receive (ex: USDC)"),
    quantity: str = typer.Argument(..., help="Amount of symbol_in to swap"),
    slippage_bps: int = typer.Option(
        50, min=0, max=1000, help="Slippage tolerance in basis points"
    ),
    max_price_impact: str = typer.Option(
        str(DEFAULT_MAX_PRICE_IMPACT_PCT),
        help="Impacto de preço máximo aceito, em % (ex: 1 = 1%)",
    ),
    idempotency_key: str | None = typer.Option(
        None, help="Chave para não repetir o mesmo swap (padrão: aleatória)"
    ),
):
    """
    Executa um swap manual entre dois símbolos.

    Exemplos:
        uv run main.py swap dry JUP USDC 1000
        uv run main.py swap real SOL USDC 0.5 --slippage-bps 100
    """

    amount = parse_decimal(quantity, "quantity")
    if amount <= 0:
        raise typer.BadParameter("quantity deve ser maior que zero")
    max_impact = parse_decimal(max_price_impact, "max-price-impact")
    request = SwapRequest(
        spend_mint=SOLANA_MINTS.get_by_symbol(symbol_in).mint,
        receive_mint=SOLANA_MINTS.get_by_symbol(symbol_out).mint,
        amount=amount,
        slippage_bps=slippage_bps,
        idempotency_key=idempotency_key,
    )

    service = build_trade_service(
        mode, on_wallet_created=warn, max_price_impact_pct=max_impact
    )
    with service.gateway:
        reply = asyncio.run(_swap(service, request))
    if reply.order is None:
        for reason in reply.reasons or (reply.error or "",):
            typer.echo(f"Swap {reply.status}: {reason}", err=True)
        raise typer.Exit(1)
    typer.echo(_format_swap(reply.order))


async def _swap(service: TradeService, request: SwapRequest) -> OrderReply:
    try:
        return await service.swap(request)
    finally:
        await service.aclose()


def _format_swap(order: Order) -> str:
    spend = SOLANA_MINTS.symbol_of(order.input_mint)
    receive = SOLANA_MINTS.symbol_of(order.output_mint)
    return (
        f"Swap executado: {order.order_id}\n"
        f"  gasto {order.quote_amount} {spend} -> recebido {order.quantity} "
        f"{receive} ({order.fill_price:.9g} {spend}/{receive})\n"
        f"  {describe_costs(order.costs, order.sol_usd)}"
    )
