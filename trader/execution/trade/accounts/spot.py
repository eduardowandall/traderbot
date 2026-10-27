"""`SpotAccount`: a conta spot de um bucket (A7, um `BucketAccount`).

O que é do swap, por cima da `BaseAccount` (A23): a compra e a venda no
`Venue` do modo e a conferência da carteira numa venda (o token fica nela).
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.prices import PriceOracle
from trader.execution.models.book import remainder_entry
from trader.execution.models.intent import IntentSide
from trader.execution.models.venue import PostTrade, Venue
from trader.execution.trade.accounts.base import BaseAccount
from trader.execution.trade.accounts.wallet import WalletBalances
from trader.execution.trade.gateway.fills import Fill, record_fill_safely
from trader.execution.trade.gateway.gateway import TradeGateway
from trader.execution.trade.gateway.orders import order_from_fill
from trader.shared.models import Order, OrderSide
from trader.shared.models.position import Position


class WalletShortfallError(ValueError):
    """A carteira tem menos do token do que a venda pede (A5).

    A venda é recusada, não limitada ao saldo: o dono confere a carteira.
    """

    def __init__(self, mint: str, quantity: Decimal, available: Decimal, entry_id: str):
        super().__init__(
            f"venda de {quantity} recusada: a carteira tem só {available} "
            f"gastável de {mint}"
        )
        self.mint = mint
        self.quantity = quantity
        self.available = available
        self.entry_id = entry_id


class SpotAccount(BaseAccount):
    def __init__(
        self,
        venue: Venue,
        input_mint: Pubkey,
        output_mint: Pubkey,
        gateway: TradeGateway,
        account_id: str | None = None,
        source: str = "strategy",
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
        prices: PriceOracle | None = None,
        # o saldo da carteira, dividido pelos buckets do mesmo serviço
        wallet: WalletBalances | None = None,
    ):
        super().__init__(
            input_mint,
            output_mint,
            gateway,
            wallet or WalletBalances(venue, clock),
            account_id=account_id,
            source=source,
            clock=clock,
            prices=prices,
        )
        self.venue = venue

    @property
    def post_trade(self) -> PostTrade:
        return self.venue

    def _to_order(
        self,
        fill: Fill,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        # nunca levanta: o swap já está EXECUTED (ver `gateway/orders.py`)
        return order_from_fill(
            fill,
            self._quote,
            self._token,
            side,
            self.clock(),
            signal_price=price,
            usd=usd,
            requested_quantity=requested_quantity,
        )

    # --- ordens ------------------------------------------------------------

    def _sellable(self, balance: Decimal) -> Position:
        """Verifica se é possível vender e devolve a posição a fechar."""
        position = self.book.position
        if position is None:
            raise ValueError(
                "Não é possível executar venda no momento. Sem posicão de compra"
            )
        self.logger.debug(f"sell: output_mint={str(self.output_mint)} {balance=}")
        if balance < Decimal("0.00001"):  # Mínimo para vender
            raise ValueError(
                "Não é possível executar venda no momento. Sem valor minimo"
            )
        return position

    async def buy(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
        limit: Decimal | None = None,
    ) -> Order:
        """Compra `quantity` do token, gastando `quantity * price` do input.

        `limit` é o que o bucket ainda pode gastar, no token de cotação (None: só a carteira
        limita). O valor gasto é calculado uma vez e vai na intenção.
        """
        limit = await self._buy_limit(limit)
        spend = quantity * price
        if spend > limit:
            self.logger.warning(
                f"Compra de {spend} acima do disponível {limit}; ajustando"
            )
            spend = limit
            quantity = spend / price

        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.BUY,
            price,
            quantity,
            spend,
            usd,
            idempotency_key=idempotency_key,
            rationale=rationale,
        )
        order = await self._execute_order(
            intent,
            lambda: self.venue.open(self.input_mint, self.output_mint, spend),
            OrderSide.BUY,
            price,
            usd,
        )
        self._log_fill("ORDER PLACED", order)
        record_fill_safely(self.gateway, intent.intent_id, order)
        self.book.open(order)
        return order

    async def sell(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        """Vende até `quantity` da posição (nunca mais que ela).

        Se a carteira não cobre a venda, ela é recusada (`WalletShortfallError`,
        A5). A chave padrão é uma por posição (`<conta>:sell:<entrada>:<qtd>`);
        quem pede pode fixar outra (o strategy-runner, para um reenvio não
        repetir).
        """
        # uma leitura nova da carteira (A5): o cache pode ser de antes da compra
        balance = await self.wallet.fresh(self.output_mint)
        position = self._sellable(balance)
        entry = position.entry_order
        # nunca mais do que a posição: o pedido vem do cliente, e vendas não
        # passam pelas regras de orçamento
        quantity = min(quantity, entry.quantity)
        available = self.wallet.spendable(self.output_mint, balance)
        if quantity > available:
            raise WalletShortfallError(
                str(self.output_mint), quantity, available, entry.order_id
            )

        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.SELL,
            price,
            quantity,
            quantity,
            usd,
            # uma venda por posição: evita vender duas vezes a mesma entrada
            idempotency_key=idempotency_key
            or f"{self.account_id}:sell:{entry.order_id}:{entry.quantity}",
            rationale=rationale,
            closes_position=remainder_entry(entry, quantity) is None,
        )
        order = await self._execute_order(
            intent,
            lambda: self.venue.close(self.input_mint, self.output_mint, quantity),
            OrderSide.SELL,
            price,
            usd,
        )
        self._log_fill("ORDER PLACED", order, position=asdict(position))
        order, closed = self.book.settle_sell(order)
        record_fill_safely(
            self.gateway, intent.intent_id, order, closed.realized_usd, closed.pnl
        )
        return order
