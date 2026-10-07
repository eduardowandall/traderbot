"""`SpotAccount`: a conta spot de um bucket (A7, um `BucketAccount`).

Um par, uma posição por vez. Cuida do que depende da carteira e do par:
saldos (com cache), a reserva de SOL para taxas, as intenções de compra e
venda e a conversão do fill em `Order`. O teto do bucket chega pronto em
`buy(limit=...)`, no token de cotação (o `TradeService` calcula). A posição
e o PnL ficam no `PositionBook` (`self.book`, camada core); a execução passa
por `execute_trade`, no `Venue` do modo.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.book import PositionBook, remainder_entry
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentSide, TradeIntent, with_idempotency_key
from trader.execution.models.venue import PostTrade, Venue
from trader.execution.trade.gateway.balances import WalletBalances
from trader.execution.trade.gateway.fills import (
    Fill,
    execute_trade,
    record_fill_safely,
)
from trader.execution.trade.gateway.gateway import AccountState, TradeGateway
from trader.execution.trade.gateway.orders import order_from_fill, priced_mints
from trader.shared.models import SOLANA_MINTS, Order, OrderSide
from trader.shared.models.costs import LAMPORTS_PER_SOL, FailedTxFee
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.position import Position

SOL = SOLANA_MINTS[SOL_MINT].pubkey


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


class SpotAccount:
    def __init__(
        self,
        venue: Venue,
        input_mint: Pubkey,
        output_mint: Pubkey,
        gateway: TradeGateway,
        account_id: str | None = None,
        source: str = "strategy",
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
        # preços USD (Price API) para pares sem stablecoin ou sem SOL;
        # None: esses valores ficam desconhecidos (backtest, testes)
        prices: PriceOracle | None = None,
        # o saldo da carteira, dividido pelos buckets do mesmo serviço
        wallet: WalletBalances | None = None,
    ):
        self.venue = venue
        # quem busca os custos depois de executar (a perp troca, A8)
        self.post_trade: PostTrade = venue
        self.prices = prices
        # relógio injetável: no backtest é o tempo do tick, para que
        # `Order.timestamp` (e o `max_hold` das estratégias) sigam o replay
        self.clock = clock
        # toda ordem vira uma intenção avaliada pela política e registrada no
        # ledger (no backtest, um gateway em memória: `TradeGateway.in_memory`)
        self.gateway = gateway
        self.account_id = account_id or f"{input_mint}:{output_mint}"
        self.source = source
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.logger = logging.getLogger(self.__module__)

        self._quote = SOLANA_MINTS[input_mint]
        self._token = SOLANA_MINTS[output_mint]
        self.book = PositionBook(self._quote.symbol)
        # o que o trade não precifica sozinho (Price API, antes do trade)
        self._priced_mints = priced_mints(self._quote, self._token)

        self.wallet = wallet or WalletBalances(venue, clock)
        # do ledger (restore): para a estratégia retomar depois de reiniciar
        self.opened_at: datetime | None = None
        self.last_exit_at: datetime | None = None
        self.last_exit_price: Decimal | None = None

    def __repr__(self):
        return f"{self.__class__.__name__}({self.account_id}, {self.book.position})"

    def committed(self) -> Decimal:
        """O custo da posição aberta no token de cotação (0 sem posição).

        É o que a alocação soma ao saldo livre: o que o bucket já comprou
        conta pelo que gastou.
        """
        position = self.book.position
        if position is None:
            return Decimal("0")
        return position.entry_order.quote_amount or Decimal("0")

    # --- saldos ------------------------------------------------------------

    async def get_balance(self, mint: Pubkey) -> Decimal:
        return await self.wallet.get(mint)

    async def get_spendable_balance(self, mint: Pubkey) -> Decimal:
        return self._spendable(mint, await self.get_balance(mint))

    def _spendable(self, mint: Pubkey, balance: Decimal) -> Decimal:
        """Saldo que pode ser gasto; para SOL desconta a reserva de taxas.

        A reserva é do local de execução (`venue.native_fee_reserve`):
        0.02 SOL on-chain e em paper, zero no backtest (sem taxas de rede).
        """
        if mint == SOL:
            balance -= self.venue.native_fee_reserve
        return max(balance, Decimal("0"))

    # --- estado do ledger --------------------------------------------------

    def restore_from_ledger(self) -> None:
        """Reconstrói o livro (posição aberta e PnL) do ledger, após reinício."""
        state = self._restore_quietly()
        entry = state.open_entry
        if entry is not None:
            self.logger.warning(
                f"Posição restaurada do ledger: {entry.quantity} @ {entry.price} "
                f"(intenção {state.entry_intent_id})"
            )

    def _restore_quietly(self) -> AccountState:
        state = self.gateway.restore(self.account_id)
        totals = state.totals
        self.book = PositionBook.restored(
            self.book.quote_symbol,
            realized_usd=state.realized_usd,
            gross_quote=totals.gross_quote,
            net_quote=totals.net_quote,
            costs_sol=totals.costs_sol,
            incomplete=totals.incomplete,
            entry=state.open_entry,
            failed_fee_sol=Decimal(totals.failed_fee_lamports) / LAMPORTS_PER_SOL,
            rent_refund_sol=Decimal(totals.rent_refund_lamports) / LAMPORTS_PER_SOL,
        )
        self.opened_at = state.opened_at
        self.last_exit_at = state.last_exit_at
        self.last_exit_price = state.last_exit_price
        return state

    # --- fill -> Order -----------------------------------------------------

    def _to_order(
        self,
        fill: Fill,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        # nunca levanta: o swap já está EXECUTED (ver `execution/orders.py`)
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

    async def _execute_order(
        self,
        intent: TradeIntent,
        call: Callable[[], Awaitable[ExecutionResult]],
        side: OrderSide,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        """Executa a intenção e converte o fill em `Order`.

        `usd` são os preços tirados antes do trade: depois de EXECUTED nada
        pode falhar, então nenhum preço é buscado aqui. Erros sobem sem log:
        quem classifica e registra é o `TradeService`.
        """
        fill = await execute_trade(
            self.gateway,
            self.post_trade,
            intent,
            call,
            sol_usd=usd.get(SOL_MINT),
            # tentativas que falharam na rede: a taxa sai do PnL do bucket
            on_failed_fee=self._charge_failed_fee,
        )
        # a carteira mudou: o próximo get_balance relê
        self.wallet.invalidate()
        requested = intent.quantity or Decimal("0")
        return self._to_order(fill, side, requested, price, usd)

    def _charge_failed_fee(self, fee: FailedTxFee) -> None:
        self.book.charge(fee)

    # --- intenções ---------------------------------------------------------

    def _notional_usd(
        self,
        side: IntentSide,
        price: Decimal,
        quantity: Decimal,
        spend_amount: Decimal,
        usd: dict[str, Decimal],
    ) -> Decimal | None:
        """Valor do trade em USD, quando dá para saber.

        O preço do sinal é no token de cotação, então os dois lados viram um
        valor em cotação (venda: quantidade x preço; compra: o valor gasto) e
        depois USD pelo preço do token de cotação. Sem esse preço (Price API
        fora) fica desconhecido, e a política recusa compras
        (`allow_unknown_notional`).
        """
        in_quote = quantity * price if side == IntentSide.SELL else spend_amount
        rate = usd.get(str(self.input_mint))
        return None if rate is None else in_quote * rate

    async def _usd_snapshot(self) -> dict[str, Decimal]:
        return await usd_snapshot(self.prices, self._priced_mints)

    def _intent(
        self,
        side: IntentSide,
        price: Decimal,
        quantity: Decimal,
        spend_amount: Decimal,
        usd: dict[str, Decimal],
        idempotency_key: str | None = None,
        rationale: str | None = None,
        closes_position: bool | None = None,
    ) -> TradeIntent:
        spend, receive = (
            (self.input_mint, self.output_mint)
            if side == IntentSide.BUY
            else (self.output_mint, self.input_mint)
        )
        intent = TradeIntent(
            source=self.source,
            account=self.account_id,
            side=side,
            spend_mint=str(spend),
            receive_mint=str(receive),
            spend_amount=spend_amount,
            notional_usd=self._notional_usd(side, price, quantity, spend_amount, usd),
            price=price,
            quantity=quantity,
            rationale=rationale,
            closes_position=closes_position,
        )
        return with_idempotency_key(intent, idempotency_key)

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

    async def _buy_limit(self, cap: Decimal | None) -> Decimal:
        """Quanto uma compra pode gastar: saldo gastável, limitado pelo bucket.

        Uma leitura de saldo só, nova (A5): toda ordem relê a carteira.
        """
        if self.book.position is not None:
            raise ValueError(
                "Não é possível executar compra no momento. Já existe posicão"
            )
        spendable = self._spendable_input(await self.wallet.fresh(self.input_mint))
        if cap is None:
            return spendable
        if cap <= 0:
            raise ValueError("Não é possível executar compra: orçamento esgotado")
        return min(spendable, cap)

    def _spendable_input(self, balance: Decimal) -> Decimal:
        if balance < Decimal("0.01"):  # mínimo para operar
            raise ValueError(
                "Não é possível executar compra no momento. Sem valor minimo"
            )
        spendable = self._spendable(self.input_mint, balance)
        if spendable <= 0:
            raise ValueError(
                "Não é possível executar compra no momento. Saldo reservado para taxas"
            )
        return spendable

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
        self.logger.info(f"ORDER PLACED: {asdict(order)}", extra=asdict(order))
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
        available = self._spendable(self.output_mint, balance)
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
        self.logger.info(
            f"ORDER PLACED: order={asdict(order)} position={asdict(position)}",
            extra=asdict(order),
        )
        order, closed = self.book.settle_sell(order)
        record_fill_safely(
            self.gateway, intent.intent_id, order, closed.realized_usd, closed.pnl
        )
        return order
