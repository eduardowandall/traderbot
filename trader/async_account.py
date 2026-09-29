"""`AsyncAccount`: a conta de um bucket (um par, uma posição por vez).

Cuida do que depende da carteira e do par: saldos (com cache), a reserva de
SOL para taxas, o teto do bucket, as intenções de compra e venda e a
conversão do fill em `Order`. A posição e o PnL ficam no `PositionBook`
(`self.book`, camada core); a execução passa por `execute_trade`.
"""

import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.execution import DuplicateIntentError, PolicyDeniedError, TradeGateway
from trader.execution.fills import Fill, execute_trade
from trader.market.prices import PriceOracle, usd_snapshot
from trader.models.book import PositionBook
from trader.models.costs import trade_rates, with_sol_usd
from trader.models.intent import IntentSide, TradeIntent, with_idempotency_key
from trader.models.position import Position
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider

from .models import SOLANA_MINTS, Order, OrderSide

# saldos lidos do provider valem por este tempo (invalidados a cada ordem)
BALANCE_CACHE_TTL = timedelta(minutes=3)
SOL = SOLANA_MINTS.get_by_symbol("SOL").pubkey


class AsyncAccount:
    def __init__(
        self,
        provider: AsyncJupiterProvider,
        input_mint: Pubkey,
        output_mint: Pubkey,
        gateway: TradeGateway,
        account_id: str | None = None,
        source: str = "strategy",
        clock: Callable[[], datetime] = datetime.now,
        spend_cap: Callable[[], Decimal] | None = None,
        # preços USD (Price API) para pares sem stablecoin ou sem SOL;
        # None: esses valores ficam desconhecidos (backtest, testes)
        prices: PriceOracle | None = None,
    ):
        self.provider = provider
        self.prices = prices
        # teto de gasto por compra (orçamento do bucket, na moeda de entrada);
        # None = só o saldo da carteira limita
        self.spend_cap = spend_cap
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

        # o feed de preços é em USD: fills e valores só batem com ele quando
        # o input_mint é uma stablecoin de dólar
        quote = SOLANA_MINTS.get(input_mint)
        self._quote_is_usd = quote is not None and quote.is_usd_stable
        self._quote_is_sol = input_mint == SOL
        self._token_is_sol = output_mint == SOL
        self.book = PositionBook(SOLANA_MINTS.symbol_of(input_mint))
        # o que o trade não precifica sozinho: o token gasto na compra (se não
        # é stablecoin) e o SOL dos custos (se o par não tem SOL)
        self._priced_mints: set[str] = set()
        if not self._quote_is_usd:
            self._priced_mints.add(str(input_mint))
        if not (self._quote_is_sol or self._token_is_sol):
            self._priced_mints.add(str(SOL))

        self.balances = None
        self.balances_last_update: datetime | None = None

    def __repr__(self):
        return f"{self.__class__.__name__}({self.account_id}, {self.book.position})"

    # --- saldos ------------------------------------------------------------

    async def get_balance(self, mint: Pubkey) -> Decimal:
        # Cache para evitar chamadas repetidas ao RPC; expira após
        # BALANCE_CACHE_TTL e é invalidado a cada ordem executada
        # (`_execute_order`)
        if self._balances_stale():
            self.balances = await self.provider.get_account_balance()
            self.balances_last_update = self.clock()
        for balance in self.balances or []:
            if balance.mint == mint:
                self.logger.debug(
                    f"get_balance: {str(balance.mint)=} {balance.available=}"
                )
                return balance.available
        return Decimal("0.0")

    def _balances_stale(self) -> bool:
        if not self.balances or self.balances_last_update is None:
            return True
        return self.balances_last_update < self.clock() - BALANCE_CACHE_TTL

    async def get_spendable_balance(self, mint: Pubkey) -> Decimal:
        """Saldo que pode ser gasto; para SOL desconta a reserva de taxas.

        A reserva é do local de execução (`provider.native_fee_reserve`):
        0.02 SOL on-chain e em paper, zero no backtest (sem taxas de rede).
        """
        balance = await self.get_balance(mint)
        if mint == SOL:
            balance -= self.provider.native_fee_reserve
        return max(balance, Decimal("0"))

    # --- estado do ledger --------------------------------------------------

    def restore_from_ledger(self) -> None:
        """Reconstrói o livro (posição aberta e PnL) do ledger, após reinício."""
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
        )
        entry = state.open_entry
        if entry is not None:
            self.logger.warning(
                f"Posição restaurada do ledger: {entry.quantity} @ {entry.price} "
                f"(intenção {state.entry_intent_id})"
            )

    async def reconcile_position(self) -> bool:
        """Confere a posição do ledger contra o saldo da carteira.

        Retorna False (e registra no ledger) se a carteira tem menos do que a
        posição registrada; a posição não é alterada automaticamente.
        """
        if self.book.position is None:
            return True
        expected = self.book.position.entry_order.quantity
        balance = await self.get_balance(self.output_mint)
        # tolerância para taxas/arredondamento
        if balance >= expected * Decimal("0.99"):
            return True
        self.logger.warning(
            f"Carteira tem {balance} mas o ledger registra posição de {expected}"
        )
        self.gateway.add_event(
            "reconcile_mismatch",
            {
                "account": self.account_id,
                "expected": expected,
                "wallet_balance": balance,
            },
        )
        return False

    # --- fill -> Order -----------------------------------------------------

    def _fill_amounts(self, fill: Fill) -> tuple[Decimal, Decimal, Decimal]:
        """(quantidade do token, preço em cotação por token, valor em cotação).

        Usa os valores efetivamente movidos on-chain quando conhecidos; senão
        os da quote.
        """
        in_raw, out_raw = fill.amounts()
        token_raw, quote_raw = (
            (out_raw, in_raw)
            if fill.result.output_mint == str(self.output_mint)
            else (in_raw, out_raw)
        )
        quantity = SOLANA_MINTS.raw_to_ui(str(self.output_mint), token_raw)
        quote_amount = SOLANA_MINTS.raw_to_ui(str(self.input_mint), quote_raw)
        if quantity <= 0:
            raise ValueError(f"Swap sem quantidade executada: {fill.result}")
        return quantity, quote_amount / quantity, quote_amount

    def _to_order(
        self,
        fill: Fill,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        filled_quantity, fill_price, quote_amount = self._fill_amounts(fill)
        rates = trade_rates(
            price,
            fill_price,
            self._quote_is_usd,
            self._quote_is_sol,
            self._token_is_sol,
        )
        rates = with_sol_usd(rates, usd.get(str(SOL)))
        return Order(
            order_id=fill.result.signature,
            input_mint=str(self.input_mint),
            output_mint=str(self.output_mint),
            quantity=filled_quantity,
            # preço na unidade do feed (USD): o fill só está em USD quando o
            # input_mint é stablecoin; senão (ex: USDC-SOL) usa o preço do sinal,
            # para o PnL não comparar SOL com USD
            price=fill_price if self._quote_is_usd else price,
            side=side,
            timestamp=self.clock(),
            requested_quantity=requested_quantity,
            requested_price=price,
            fill_price=fill_price,
            quote_amount=quote_amount,
            quote_usd=rates.quote_usd,
            sol_usd=rates.sol_usd,
            sol_in_quote=rates.sol_in_quote,
            costs=fill.costs,
        )

    async def _execute_order(
        self,
        intent: TradeIntent,
        call,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
        usd: dict[str, Decimal],
    ) -> Order:
        """Executa a intenção e converte o fill em `Order`.

        `usd` são os preços tirados antes do trade: depois de EXECUTED nada
        pode falhar, então nenhum preço é buscado aqui.
        """
        try:
            fill = await execute_trade(self.gateway, self.provider, intent, call)
            # a carteira mudou: o próximo get_balance relê
            self.balances = None
            return self._to_order(fill, side, requested_quantity, price, usd)
        except PolicyDeniedError, DuplicateIntentError:
            raise  # recusa esperada; o bot registra como aviso
        except Exception as ex:
            self.logger.error(f"Erro ao executar ordem: {str(ex)}")
            raise

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

        Venda: quantidade x preço do sinal (o feed é em USD em todo par).
        Compra: valor gasto x preço USD do token gasto. As estratégias
        dimensionam a compra pelo saldo do input_mint, então só o valor gasto
        está na unidade certa; sem preço do input (Price API fora) fica
        desconhecido, e a política recusa (`allow_unknown_notional`).
        """
        if side == IntentSide.SELL:
            return quantity * price
        if self._quote_is_usd:
            return spend_amount
        rate = usd.get(str(self.input_mint))
        return None if rate is None else spend_amount * rate

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
        )
        return with_idempotency_key(intent, idempotency_key)

    # --- ordens ------------------------------------------------------------

    async def can_buy(self) -> None:
        """Verifica se é possível executar uma compra"""
        if self.book.position is not None:
            raise ValueError(
                "Não é possível executar compra no momento. Já existe posicão"
            )

        balance = await self.get_balance(self.input_mint)
        self.logger.debug(f"can_buy: input_mint={str(self.input_mint)} {balance=}")
        if balance < Decimal("0.01"):  # Mínimo para operar
            raise ValueError(
                "Não é possível executar compra no momento. Sem valor minimo"
            )

    async def can_sell(self) -> Position:
        """Verifica se é possível vender e devolve a posição a fechar."""
        position = self.book.position
        if position is None:
            raise ValueError(
                "Não é possível executar venda no momento. Sem posicão de compra"
            )

        balance = await self.get_balance(self.output_mint)
        self.logger.debug(f"can_sell: output_mint={str(self.output_mint)} {balance=}")
        if balance < Decimal("0.00001"):  # Mínimo para vender
            raise ValueError(
                "Não é possível executar venda no momento. Sem valor minimo"
            )
        return position

    async def place_order(
        self,
        price: Decimal,
        side: OrderSide,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        if side == OrderSide.BUY:
            return await self.buy(price, quantity, rationale, idempotency_key)
        if side == OrderSide.SELL:
            # a venda tem chave fixa por posição (ver `sell`)
            return await self.sell(price, quantity, rationale)

        raise ValueError("Invalid Order Side.")

    async def _buy_limit(self) -> Decimal:
        """Quanto uma compra pode gastar: saldo gastável, limitado pelo bucket."""
        if self.spend_cap is not None:
            # buckets dividem a carteira: o saldo em cache pode já ter sido
            # gasto por outro bucket
            self.balances = None
        spendable = await self.get_spendable_balance(self.input_mint)
        if spendable <= 0:
            raise ValueError(
                "Não é possível executar compra no momento. Saldo reservado para taxas"
            )
        cap = spendable if self.spend_cap is None else self.spend_cap()
        if cap <= 0:
            raise ValueError("Não é possível executar compra: orçamento esgotado")
        return min(spendable, cap)

    async def buy(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        await self.can_buy()

        limit = await self._buy_limit()
        if quantity * price > limit:
            self.logger.warning(
                f"Compra de {quantity * price} acima do disponível {limit}; "
                "ajustando a quantidade"
            )
            quantity = limit / price

        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.BUY,
            price,
            quantity,
            quantity * price,
            usd,
            idempotency_key=idempotency_key,
            rationale=rationale,
        )
        order = await self._execute_order(
            intent,
            lambda: self.provider.buy(
                self.input_mint,
                self.output_mint,
                type_order="market",
                quantity=quantity,
                price=price,
            ),
            OrderSide.BUY,
            quantity,
            price,
            usd,
        )
        self.logger.debug(f"ORDER PLACED: {asdict(order)}", extra=asdict(order))
        self.gateway.record_fill(intent.intent_id, order)
        self.book.open(order)
        return order

    async def sell(
        self, price: Decimal, quantity: Decimal, rationale: str | None = None
    ) -> Order:
        position = await self.can_sell()

        # a quantidade recebida na compra pode ser menor que a pedida
        # (slippage/taxas); nunca tenta vender mais do que a carteira tem
        available = await self.get_spendable_balance(self.output_mint)
        if available <= 0:
            raise ValueError(
                "Não é possível executar venda no momento. Saldo reservado para taxas"
            )
        if quantity > available:
            self.logger.warning(
                f"Quantidade de venda {quantity} maior que o saldo {available}; "
                "vendendo o saldo disponível"
            )
            quantity = available

        usd = await self._usd_snapshot()
        intent = self._intent(
            IntentSide.SELL,
            price,
            quantity,
            quantity,
            usd,
            # uma venda por posição: evita vender duas vezes a mesma entrada
            idempotency_key=f"{self.account_id}:sell:{position.entry_order.order_id}",
            rationale=rationale,
        )
        order = await self._execute_order(
            intent,
            lambda: self.provider.sell(
                self.input_mint,
                self.output_mint,
                type_order="market",
                quantity=quantity,
            ),
            OrderSide.SELL,
            quantity,
            price,
            usd,
        )
        self.logger.debug(
            f"ORDER PLACED: order={asdict(order)} position={asdict(position)}",
            extra=asdict(order),
        )
        closed = self.book.close(order)
        self.gateway.record_fill(
            intent.intent_id, order, closed.realized_usd, closed.pnl
        )
        return order
