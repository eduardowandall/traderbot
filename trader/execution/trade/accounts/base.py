"""`BaseAccount`: o que as contas de bucket dividem (A23).

Um par, uma posição por vez. Aqui fica o que não depende de onde a ordem
executa: o livro (`self.book`, camada core) e o restore do ledger, as
leituras da carteira (`WalletBalances`, com a reserva de SOL), as intenções e
a execução de uma intenção até virar `Order` (`execute_trade`). O teto do
bucket chega pronto em `buy(limit=...)`, no token de cotação (o
`TradeService` calcula).

Quem executa é da subclasse: o `SpotAccount` troca no `Venue` (o swap), o
`PerpAccount` abre e fecha no `PerpVenue`; nenhuma das duas conhece a outra.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.book import PositionBook
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import (
    SPENDING_SIDES,
    IntentSide,
    TradeIntent,
    with_idempotency_key,
)
from trader.execution.models.venue import PostTrade
from trader.execution.trade.accounts.wallet import WalletBalances
from trader.execution.trade.gateway.fills import Fill, execute_trade
from trader.execution.trade.gateway.gateway import AccountState, TradeGateway
from trader.execution.trade.gateway.orders import priced_mints
from trader.shared.models import SOLANA_MINTS, Order, OrderSide
from trader.shared.models.costs import (
    LAMPORTS_PER_SOL,
    FailedTxFee,
    describe_costs,
    outside_cost_quote,
)
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.perp import describe_fill


class BaseAccount(ABC):
    def __init__(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        gateway: TradeGateway,
        wallet: WalletBalances,
        account_id: str | None = None,
        source: str = "strategy",
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
        # preços USD (Price API) para pares sem stablecoin ou sem SOL;
        # None: esses valores ficam desconhecidos (backtest, testes)
        prices: PriceOracle | None = None,
    ):
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

        # o saldo da carteira, dividido pelos buckets do mesmo serviço
        self.wallet = wallet
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
        return self.wallet.spendable(mint, await self.get_balance(mint))

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
        state = self.gateway.restore(self.account_id, str(self.input_mint))
        totals = state.totals
        self.book = PositionBook.restored(
            self.book.quote_symbol,
            realized_usd=state.realized_usd,
            gross_quote=totals.gross_quote,
            net_quote=totals.net_quote,
            costs_sol=totals.costs_sol,
            incomplete=totals.incomplete,
            entry=state.open_position,
            failed_fee_sol=Decimal(totals.failed_fee_lamports) / LAMPORTS_PER_SOL,
            rent_refund_sol=Decimal(totals.rent_refund_lamports) / LAMPORTS_PER_SOL,
            perp=(
                totals.perp_closes,
                totals.perp_fees_quote,
                totals.perp_borrow_quote,
            ),
        )
        self.opened_at = state.opened_at
        self.last_exit_at = state.last_exit_at
        self.last_exit_price = state.last_exit_price
        return state

    @property
    @abstractmethod
    def post_trade(self) -> PostTrade:
        """Quem busca os custos depois de executar: o venue da subclasse."""

    # --- fill -> Order -----------------------------------------------------

    @abstractmethod
    def _to_order(
        self,
        fill: Fill,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        """O fill como `Order`; nunca levanta (a perna já está EXECUTED)."""

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

    def _log_fill(self, label: str, order: Order, **detail) -> None:
        """Um fill numa linha em INFO (console e arquivo do `serve`, A32); a
        ordem inteira (e `detail`) fica no arquivo, em DEBUG."""
        self.logger.info(f"{label}: {fill_line(order)}", extra=asdict(order))
        whole = " ".join(f"{k}={v}" for k, v in detail.items())
        self.logger.debug(f"{label}: order={asdict(order)} {whole}".rstrip())

    def _charge_failed_fee(self, fee: FailedTxFee) -> None:
        self.book.charge(fee, outside_cost_quote(self._quote, fee.sol, fee.usd))

    def _charge_outside(self, lamports: int, usd: Decimal | None) -> None:
        """Um custo sem trade pago em SOL (o stop do venue) no livro (A30)."""
        sol = Decimal(lamports) / LAMPORTS_PER_SOL
        quote = outside_cost_quote(self._quote, sol, usd)
        self.book.charge_outside(sol, quote, usd)

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
            if side in SPENDING_SIDES
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

    async def _buy_limit(self, cap: Decimal | None) -> Decimal:
        """Quanto uma compra pode gastar: saldo gastável, limitado pelo bucket.

        Uma leitura de saldo só, nova (A5): toda ordem relê a carteira.
        """
        if self.book.position is not None:
            raise ValueError(
                "Não é possível executar compra no momento. Já existe posicão"
            )
        spendable = await self._wallet_room()
        if cap is None:
            return spendable
        if cap <= 0:
            raise ValueError("Não é possível executar compra: orçamento esgotado")
        return min(spendable, cap)

    async def _wallet_room(self) -> Decimal:
        """O que a carteira deixa gastar do token de cotação, lido agora (A5)."""
        return self._spendable_input(await self.wallet.fresh(self.input_mint))

    def _spendable_input(self, balance: Decimal) -> Decimal:
        if balance < Decimal("0.01"):  # mínimo para operar
            raise ValueError(
                "Não é possível executar compra no momento. Sem valor minimo"
            )
        spendable = self.wallet.spendable(self.input_mint, balance)
        if spendable <= 0:
            raise ValueError(
                "Não é possível executar compra no momento. Saldo reservado para taxas"
            )
        return spendable

    @abstractmethod
    async def buy(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
        limit: Decimal | None = None,
    ) -> Order:
        """Entra: `limit` é o que o bucket ainda pode gastar (None: a carteira)."""

    @abstractmethod
    async def sell(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        """Sai da posição, ou de `quantity` dela."""


def fill_line(order: Order) -> str:
    """Lado, quantidade, preço, a perna da perp, os custos e a assinatura."""
    token = SOLANA_MINTS.symbol_of(order.output_mint)
    parts = [f"{order.side.upper()} {order.quantity:.8f} {token} @ ${order.price:.4f}"]
    if order.perp is not None:
        parts.append(describe_fill(order.perp))
    parts.append(describe_costs(order.costs, order.sol_usd))
    parts.append(f"tx {order.order_id}")
    return "; ".join(parts)
