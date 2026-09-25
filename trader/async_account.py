import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.execution import DuplicateIntentError, PolicyDeniedError, TradeGateway
from trader.ledger import order_from_json
from trader.models.costs import PnLResult, TradeCosts, trade_rates
from trader.models.intent import IntentSide, TradeIntent
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider

from .models import (
    SOLANA_MINTS,
    Order,
    OrderSide,
    Position,
    PositionType,
    SwapResult,
    TickerData,
)

# SOL mínimo mantido na carteira para taxas de transação e rent
DEFAULT_SOL_FEE_RESERVE = Decimal("0.02")


class AsyncAccount:
    """Classe responsável por gerenciar balanço, posições e execução de ordens"""

    def __init__(
        self,
        provider: AsyncJupiterProvider,
        input_mint: Pubkey,
        output_mint: Pubkey,
        sol_fee_reserve: Decimal = DEFAULT_SOL_FEE_RESERVE,
        gateway: TradeGateway | None = None,
        account_id: str | None = None,
        source: str = "strategy",
    ):
        self.provider = provider
        self.sol_fee_reserve = sol_fee_reserve
        # com gateway, toda ordem vira uma intenção avaliada pela política e
        # registrada no ledger; sem ele, executa direto no provider (testes)
        self.gateway = gateway
        self.account_id = account_id or f"{input_mint}:{output_mint}"
        self.source = source
        self.input_mint = input_mint
        self.output_mint = output_mint

        self.current_position: Position | None = None
        self.logger = logging.getLogger(self.__module__)
        self.total_pnl = Decimal("0.0")

        # o feed de preços é em USD: fills e valores só batem com ele quando
        # o input_mint é uma stablecoin de dólar
        quote = SOLANA_MINTS.get(input_mint)
        self._quote_is_usd = quote is not None and quote.is_usd_stable
        sol_mint = SOLANA_MINTS.get_by_symbol("SOL").pubkey
        self._quote_is_sol = input_mint == sol_mint
        self._token_is_sol = output_mint == sol_mint
        self.quote_symbol = SOLANA_MINTS.symbol_of(input_mint)

        # PnL nativo (token de cotação) das posições fechadas; total_pnl acima
        # é a estimativa líquida em USD
        self.total_gross_quote = Decimal("0")
        self.total_net_quote = Decimal("0")
        self.total_costs_sol = Decimal("0")
        self.incomplete_trades = 0

        self.balances = None
        self.balances_last_update = datetime.min

    def __repr__(self):
        return f"{self.__class__.__name__} for {self.input_mint=} and {self.output_mint=} with current_position on {self.current_position}"

    async def get_price(self, mint: Pubkey) -> Decimal:
        return await self.provider.get_price_ticker_data(mint)

    async def get_candles(self, mint: Pubkey) -> list[TickerData]:
        return await self.provider.get_candles(mint)

    async def get_balance(self, mint: Pubkey) -> Decimal:
        # Cache para evitar chamadas repetidas ao RPC; expira a cada 3 minutos
        # e é invalidado a cada ordem executada (`_execute_order`)
        if not self.balances or self.balances_last_update < datetime.now() - timedelta(
            minutes=3
        ):
            self.balances = await self.provider.get_account_balance()
            self.balances_last_update = datetime.now()
        for balance in self.balances:
            if balance.mint == mint:
                self.logger.debug(
                    f"get_balance: {str(balance.mint)=} {balance.available=}"
                )
                return balance.available
        return Decimal("0.0")

    async def get_spendable_balance(self, mint: Pubkey) -> Decimal:
        """Saldo que pode ser gasto; para SOL desconta a reserva de taxas."""
        balance = await self.get_balance(mint)
        if mint == SOLANA_MINTS.get_by_symbol("SOL").pubkey:
            balance -= self.sol_fee_reserve
        return max(balance, Decimal("0"))

    def _fill(
        self, result: SwapResult, costs: TradeCosts | None
    ) -> tuple[Decimal, Decimal, Decimal]:
        """(quantidade do token, preço em cotação por token, valor em cotação).

        Usa os valores efetivamente movidos on-chain quando conhecidos; senão
        os da quote.
        """
        in_raw, out_raw = _actual_amounts(result, costs)
        token_raw, quote_raw = (
            (out_raw, in_raw)
            if result.output_mint == str(self.output_mint)
            else (in_raw, out_raw)
        )
        quantity = SOLANA_MINTS.raw_to_ui(str(self.output_mint), token_raw)
        quote_amount = SOLANA_MINTS.raw_to_ui(str(self.input_mint), quote_raw)
        if quantity <= 0:
            raise ValueError(f"Swap sem quantidade executada: {result}")
        return quantity, quote_amount / quantity, quote_amount

    def _to_order(
        self,
        result: SwapResult,
        costs: TradeCosts | None,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
    ) -> Order:
        filled_quantity, fill_price, quote_amount = self._fill(result, costs)
        rates = trade_rates(
            price,
            fill_price,
            self._quote_is_usd,
            self._quote_is_sol,
            self._token_is_sol,
        )
        return Order(
            order_id=result.signature,
            input_mint=str(self.input_mint),
            output_mint=str(self.output_mint),
            quantity=filled_quantity,
            # preço na unidade do feed (USD): o fill só está em USD quando o
            # input_mint é stablecoin; senão (ex: USDC-SOL) usa o preço do sinal,
            # para o PnL não comparar SOL com USD
            price=fill_price if self._quote_is_usd else price,
            side=side,
            timestamp=datetime.now(),
            requested_quantity=requested_quantity,
            requested_price=price,
            fill_price=fill_price,
            quote_amount=quote_amount,
            quote_usd=rates.quote_usd,
            sol_usd=rates.sol_usd,
            sol_in_quote=rates.sol_in_quote,
            costs=costs,
        )

    def restore_from_ledger(self) -> None:
        """Recupera posição aberta e PnL realizado do ledger (após reinício)."""
        if self.gateway is None:
            return
        ledger = self.gateway.ledger
        self.total_pnl = ledger.total_realized_pnl(self.account_id)
        totals = ledger.pnl_totals(self.account_id)
        self.total_gross_quote = totals.gross_quote
        self.total_net_quote = totals.net_quote
        self.total_costs_sol = totals.costs_sol
        self.incomplete_trades = totals.incomplete
        last = ledger.last_executed_trade(self.account_id)
        if last and last.intent.side == IntentSide.BUY and last.order_json:
            entry = order_from_json(last.order_json)
            self.current_position = Position(
                type=PositionType.LONG, entry_order=entry, exit_order=None
            )
            self.logger.warning(
                f"Posição restaurada do ledger: {entry.quantity} @ {entry.price} "
                f"(intenção {last.intent.intent_id})"
            )

    async def reconcile_position(self) -> bool:
        """Confere a posição do ledger contra o saldo da carteira.

        Retorna False (e registra no ledger) se a carteira tem menos do que a
        posição registrada; a posição não é alterada automaticamente.
        """
        if self.gateway is None or self.current_position is None:
            return True
        expected = self.current_position.entry_order.quantity
        balance = await self.get_balance(self.output_mint)
        # tolerância para taxas/arredondamento
        if balance >= expected * Decimal("0.99"):
            return True
        self.logger.warning(
            f"Carteira tem {balance} mas o ledger registra posição de {expected}"
        )
        self.gateway.ledger.add_event(
            "reconcile_mismatch",
            {
                "account": self.account_id,
                "expected": expected,
                "wallet_balance": balance,
            },
        )
        return False

    async def _execute(self, intent: TradeIntent, call):
        if self.gateway is None:
            return await call()
        return await self.gateway.submit(intent, call)

    async def _execute_order(
        self,
        intent: TradeIntent,
        call,
        side: OrderSide,
        requested_quantity: Decimal,
        price: Decimal,
    ) -> Order:
        """Executa a intenção e converte o fill em `Order`."""
        try:
            result = await self._execute(intent, call)
            # a carteira mudou: o próximo get_balance relê
            self.balances = None
            # só depois de executada (e registrada) a intenção: buscar custos
            # nunca pode fazer o swap ser re-tentado ou marcado como falho
            costs = await self._fetch_costs(result)
            return self._to_order(result, costs, side, requested_quantity, price)
        except PolicyDeniedError, DuplicateIntentError:
            raise  # recusa esperada; o bot registra como aviso
        except Exception as ex:
            self.logger.error(f"Erro ao executar ordem: {str(ex)}")
            raise

    async def _fetch_costs(self, result: SwapResult) -> TradeCosts | None:
        costs = await self.provider.fetch_swap_costs(result)
        # providers de teste (mocks) não devolvem TradeCosts
        return costs if isinstance(costs, TradeCosts) else None

    def _record_order(
        self,
        intent: TradeIntent,
        order: Order,
        pnl: PnLResult | None = None,
        realized_usd: Decimal | None = None,
    ) -> None:
        if self.gateway is not None:
            self.gateway.ledger.attach_order(intent.intent_id, order, realized_usd, pnl)

    def _book(self, pnl: PnLResult | None) -> None:
        """Acumula o PnL nativo de uma posição fechada."""
        if pnl is None:
            self.incomplete_trades += 1
            return
        self.total_gross_quote += pnl.gross_quote
        self.total_costs_sol += pnl.costs_sol
        net = pnl.net_quote
        self.total_net_quote += pnl.gross_quote if net is None else net
        self.incomplete_trades += 0 if pnl.complete else 1

    def pnl_summary(self) -> str:
        """PnL realizado: líquido nativo, estimativa USD, bruto e custos."""
        flag = (
            f" [!] {self.incomplete_trades} trade(s) incompleto(s)"
            if self.incomplete_trades
            else ""
        )
        return (
            f"PNL líquido {self.total_net_quote:+.6f} {self.quote_symbol} "
            f"(~${self.total_pnl:+.4f}); bruto {self.total_gross_quote:+.6f}, "
            f"custos {self.total_costs_sol:.9f} SOL{flag}"
        )

    def _notional_usd(self, price: Decimal, quantity: Decimal) -> Decimal | None:
        """Valor do trade em USD, quando dá para saber.

        `price` é o preço de mercado do token em USD, mas as estratégias
        dimensionam `quantity` a partir do saldo do input_mint. Só quando o
        input_mint é stablecoin de dólar as duas unidades batem; nos demais
        pares (ex: USDC-SOL) o valor seria subestimado pelo preço do SOL e
        furaria os limites da política, então fica desconhecido.
        """
        return quantity * price if self._quote_is_usd else None

    def _intent(
        self,
        side: IntentSide,
        price: Decimal,
        quantity: Decimal,
        spend_amount: Decimal,
        idempotency_key: str | None = None,
    ) -> TradeIntent:
        spend, receive = (
            (self.input_mint, self.output_mint)
            if side == IntentSide.BUY
            else (self.output_mint, self.input_mint)
        )
        return TradeIntent(
            source=self.source,
            account=self.account_id,
            side=side,
            spend_mint=str(spend),
            receive_mint=str(receive),
            spend_amount=spend_amount,
            notional_usd=self._notional_usd(price, quantity),
            price=price,
            quantity=quantity,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
        )

    def get_position(self) -> Position | None:
        """Retorna a posição atual"""
        return self.current_position

    async def can_buy(self):
        """Verifica se é possível executar uma compra"""
        # Não pode comprar se já tem posição long
        if (
            self.current_position is not None
            and self.current_position.type == PositionType.LONG
        ):
            raise ValueError(
                "Não é possível executar compra no momento. Já existe posicão"
            )

        balance = await self.get_balance(self.input_mint)
        self.logger.debug(f"can_buy: input_mint={str(self.input_mint)} {balance=}")
        if balance < Decimal("0.01"):  # Mínimo para operar
            raise ValueError(
                "Não é possível executar compra no momento. Sem valor minimo"
            )

    async def can_sell(self):
        """Verifica se é possível executar uma venda"""
        # Só pode vender se tem posição long
        if (
            self.current_position is None
            or self.current_position.type != PositionType.LONG
        ):
            raise ValueError(
                "Não é possível executar venda no momento. Sem posicão de compra"
            )

        balance = await self.get_balance(self.output_mint)
        self.logger.debug(f"can_sell: output_mint={str(self.output_mint)} {balance=}")
        if balance < Decimal("0.00001"):  # Mínimo para vender
            raise ValueError(
                "Não é possível executar venda no momento. Sem valor minimo"
            )

    async def place_order(
        self, price: Decimal, side: OrderSide, quantity: Decimal
    ) -> Order:
        if side == OrderSide.BUY:
            return await self.buy(price, quantity)
        if side == OrderSide.SELL:
            return await self.sell(price, quantity)

        raise ValueError("Invalid Order Side.")

    async def buy(self, price: Decimal, quantity: Decimal) -> Order:
        await self.can_buy()

        spendable = await self.get_spendable_balance(self.input_mint)
        if spendable <= 0:
            raise ValueError(
                "Não é possível executar compra no momento. Saldo reservado para taxas"
            )
        if quantity * price > spendable:
            self.logger.warning(
                f"Compra de {quantity * price} acima do saldo disponível {spendable}; "
                "ajustando a quantidade"
            )
            quantity = spendable / price

        intent = self._intent(IntentSide.BUY, price, quantity, quantity * price)
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
        )
        self.logger.debug(f"ORDER PLACED: {asdict(order)}", extra=asdict(order))
        self._record_order(intent, order)
        self.current_position = Position(
            type=PositionType.LONG, entry_order=order, exit_order=None
        )
        return order

    async def sell(self, price: Decimal, quantity: Decimal) -> Order:
        await self.can_sell()
        position = self.current_position
        assert position  # garantido por can_sell

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

        intent = self._intent(
            IntentSide.SELL,
            price,
            quantity,
            quantity,
            # uma venda por posição: evita vender duas vezes a mesma entrada
            idempotency_key=f"{self.account_id}:sell:{position.entry_order.order_id}",
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
        )
        self.logger.debug(
            f"ORDER PLACED: order={asdict(order)} position={asdict(position)}",
            extra=asdict(order),
        )
        position.exit_order = order
        detail = position.realized_pnl_detail()
        realized = position.realized_pnl
        self._record_order(intent, order, detail, realized)
        self.total_pnl += realized
        self._book(detail)
        self.current_position = None
        return order

    def get_total_realized_pnl(self) -> Decimal:
        return self.total_pnl

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Retorna o PnL não realizado da posição atual"""
        if self.current_position:
            return self.current_position.unrealized_pnl(current_price)
        return Decimal("0.0")


def _actual_amounts(result: SwapResult, costs: TradeCosts | None) -> tuple[int, int]:
    """(entrada, saída) raw: efetivos quando conhecidos, senão os da quote."""
    if costs is None:
        return result.in_amount, result.out_amount
    in_raw = costs.actual_in_amount
    out_raw = costs.actual_out_amount
    return (
        result.in_amount if in_raw is None else in_raw,
        result.out_amount if out_raw is None else out_raw,
    )
