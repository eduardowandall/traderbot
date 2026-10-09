"""`TradeService`: executa ordens de estratégias, cada uma no seu bucket.

Camada de execução. Um bucket é uma conta lógica (`BucketAccount`; hoje o
`SpotAccount`) com
`account_id = "<modo>:<nome>"` (o ledger já separa posição e PnL por conta)
e, opcionalmente, um orçamento em USD:

    disponível = min(saldo gastável, max(0, orçamento + min(0, PnL realizado)))

Ou seja, prejuízo realizado reduz o que o bucket ainda pode gastar; lucro não
aumenta. Os buckets dividem a carteira: um cache de saldo só
(`WalletBalances`), as ordens uma por vez (`asyncio.Lock`), e os orçamentos
abertos precisam caber na carteira. Ao abrir o primeiro bucket, as posições de
todos os buckets do ledger são conferidas contra a carteira; faltando token,
compras dele ficam bloqueadas. Uma venda que a carteira não cobre é recusada
e avisada (A5), e também bloqueia compras do token. A política do gateway continua valendo para
cada ordem, por cima do bucket. Um bucket encerrado sem posição fecha a conta
do token e o rent volta para quem o pagou (A15), se nada mais precisa dela.

Uma spec com `market` (A8) tem um `PerpAccount`, no `PerpVenue` do modo
(paper; no real, só com `perps_enabled`); uma posição por mercado e lado. A
varredura do `serve` (`check_perps`) pergunta ao venue quais posições ele
fechou sozinho (liquidação, o stop dele) e registra cada uma no bucket dela;
depois põe colateral nas que chegaram perto da liquidação
(`market.add_collateral`, A12). Na primeira abertura de um bucket de perp, as
posições do venue são conferidas contra as do ledger numa leitura só (A12).

O modo (real/paper) é só do serviço: quem pede ordens nunca o conhece.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.bucket import BucketAccount
from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.perp import PerpTerms
from trader.execution.models.reconcile import (
    Market,
    MismatchKind,
    market_of,
    reconcile_perps,
)
from trader.execution.models.rent import RentRefund
from trader.execution.models.venue import Liquidation, PerpVenue, Venue
from trader.execution.trade.accounts.perp import PerpAccount
from trader.execution.trade.accounts.spot import SpotAccount, WalletShortfallError
from trader.execution.trade.accounts.wallet import WalletBalances
from trader.execution.trade.gateway import (
    DuplicateIntentError,
    PolicyDeniedError,
    TradeGateway,
)
from trader.execution.trade.gateway.resolve import IntentResolver
from trader.execution.trade.ledger import events
from trader.execution.trade.trading_service.rent import RentRefunds
from trader.shared.models import Order, OrderSide
from trader.shared.models.perp import PerpFill
from trader.shared.notification.notification_service import (
    NotificationService,
    Notifier,
)
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    OrderRequest,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
# tolerância da reconciliação (taxas, arredondamento)
RECONCILE_TOLERANCE = Decimal("0.99")


@dataclass
class _Bucket:
    account: BucketAccount
    budget_usd: Decimal | None
    status: BucketStatus = BucketStatus.ACTIVE
    # prejuízo realizado que encerra o bucket (o `max_loss_usd` da spec)
    max_loss_usd: Decimal | None = None
    # vendendo a sobra de um bucket encerrado: a venda passa por
    # `submit_order` de novo, que não pode tentar fechar outra vez
    closing: bool = False
    # já tentou fechar a conta do token (A15): uma vez por processo
    rent_checked: bool = False
    # um bucket de perp (A8): o mercado, o lado e a alavancagem
    perp: PerpTerms | None = None


def remaining_budget(budget_usd: Decimal, realized_usd: Decimal) -> Decimal:
    """Orçamento que sobra: prejuízo realizado consome, lucro não soma."""
    return max(ZERO, budget_usd + min(ZERO, realized_usd))


class TradeService:
    def __init__(
        self,
        venue: Venue,
        gateway: TradeGateway,
        mode: str | None = None,
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
        # preços USD para pares sem stablecoin/SOL; None no backtest e testes
        prices: PriceOracle | None = None,
        # avisos ao dono (Telegram, no `serve`); o padrão não envia nada
        notifier: Notifier | None = None,
        # perps (A8): None recusa buckets de perp (real até a A11, backtest)
        perps: PerpVenue | None = None,
    ):
        self.venue = venue
        self.perps = perps
        self.notifier = notifier or NotificationService()
        self.prices = prices
        # no backtest, `TradeGateway.in_memory()`
        self.gateway = gateway
        self.mode = mode
        self.clock = clock
        self.wallet = WalletBalances(venue, clock)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        # tokens com menos na carteira do que as posições do ledger
        self._blocked: set[str] = set()
        self._reconciled = False
        # (conta, entrada) cuja venda a carteira não cobriu: um aviso só
        self._shortfalls: set[tuple[str, str]] = set()
        self._resolver = IntentResolver(gateway, venue, prices, perps)
        # (mercado, lado) que o venue tem e o ledger não: compras recusadas
        self._blocked_perps: set[Market] = set()
        self._perps_reconciled = False

    def account_id(self, name: str) -> str:
        return self._prefix + name

    @property
    def _prefix(self) -> str:
        """O começo das contas deste modo no ledger."""
        return f"{self.mode}:" if self.mode else ""

    async def open_bucket(
        self,
        name: str,
        input_mint: str,
        output_mint: str,
        budget_usd: Decimal | None = None,
        source: str = "strategy",
        max_loss_usd: Decimal | None = None,
        perp: PerpTerms | None = None,
    ) -> None:
        """Cria o bucket e restaura sua posição/PnL do ledger.

        Só lê saldos se há o que conferir: posições no ledger (reconciliação,
        uma vez por serviço) ou um orçamento (alocação). `perp`: um bucket de
        perp (A8) nesse mercado e lado.
        """
        if name in self._buckets:
            raise ValueError(f"bucket {name} já está aberto")
        account = self._account(name, input_mint, output_mint, source, perp)
        bucket = _Bucket(account, budget_usd, max_loss_usd=max_loss_usd, perp=perp)
        self.gateway.open_account(account.account_id)  # antes do restore (ttl)
        account.restore_from_ledger()
        if perp is not None and not self._perps_reconciled:
            await self._reconcile_perps()
            self._perps_reconciled = True
        if not self._reconciled:
            await self._reconcile_wallet()
            self._reconciled = True
        await self._check_allocation(name, bucket)
        self._buckets[name] = bucket
        self._check_max_loss(name, bucket)  # já restaurado além do limite?

    def _account(
        self,
        name: str,
        input_mint: str,
        output_mint: str,
        source: str,
        perp: PerpTerms | None,
    ) -> SpotAccount:
        args = (Pubkey.from_string(input_mint), Pubkey.from_string(output_mint))
        kwargs = {
            "gateway": self.gateway,
            "account_id": self.account_id(name),
            "source": source,
            "clock": self.clock,
            "prices": self.prices,
            "wallet": self.wallet,
        }
        if perp is None:
            return SpotAccount(self.venue, *args, **kwargs)
        if self.perps is None:
            raise ValueError(f"bucket {name}: perps não disponíveis neste modo")
        return PerpAccount(self.venue, self.perps, perp, *args, **kwargs)

    async def _reconcile_perps(self) -> None:
        """As perps do venue contra as do ledger, numa leitura só (D10, A12).

        Uma posição que o venue tem e nenhum bucket abriu (aberta à mão, um
        envio sem desfecho) recusa as compras de perp nesse mercado e lado no
        processo; uma que o ledger tem e o venue não é registrada pela
        varredura como uma saída do venue quando o bucket dela abre. As duas
        viram um `perp_mismatch` para o dono conferir.
        """
        assert self.perps is not None  # só com um bucket de perp
        ledger = self.gateway.ledger.open_perp_markets(self._prefix)
        venue = await self.perps.open_markets()
        for found in reconcile_perps(ledger, venue):
            market = (found.market_mint, found.direction)
            if found.kind == MismatchKind.UNKNOWN_TO_LEDGER:
                self._blocked_perps.add(market)
            logger.error(
                f"Perp {found.direction} de {found.market_mint}: {found.kind}; "
                "o dono confere a posição na Jupiter"
            )
            self.gateway.add_event(
                events.PERP_MISMATCH,
                {
                    "market": found.market_mint,
                    "direction": str(found.direction),
                    "kind": str(found.kind),
                },
            )

    async def check_perps(self) -> dict[str, Order]:
        """A varredura das perps (A8, A12, A20), numa leitura do venue sob o
        lock de ordens: registra as saídas que o venue fez, depois põe
        colateral nas que seguem e chegaram perto da liquidação.

        Cada saída entra no ledger antes de o venue esquecer a posição
        (`acknowledge`): uma gravação que falha deixa a posição lá, e a
        próxima varredura tenta de novo. Devolve a ordem de saída de cada
        bucket fechado.
        """
        if self.perps is None:
            return {}
        async with self._lock:
            by_terms = {t: name for name, t in self._open_perps().items()}
            if not by_terms:
                return {}
            swept = await self.perps.sweep(list(by_terms))
            booked = await self._book_exits(swept.exits, by_terms)
            for terms, held in swept.held.items():
                await self._top_up(by_terms[terms], held)
        return booked

    async def _book_exits(
        self, exits: list[Liquidation], by_terms: dict[PerpTerms, str]
    ) -> dict[str, Order]:
        assert self.perps is not None
        booked = {}
        for liq in exits:
            name = by_terms[liq.terms]
            order = await self._book(name, liq)
            await self.perps.acknowledge(liq.terms)
            if order is not None:
                booked[name] = order
        return booked

    async def _top_up(self, name: str, held: PerpFill) -> None:
        """Colateral a mais (`market.add_collateral`), só num bucket ativo; uma
        falha não para os outros (a próxima varredura tenta de novo)."""
        bucket = self._buckets[name]
        account = bucket.account
        assert isinstance(account, PerpAccount)
        if account.terms.top_up is None or bucket.status != BucketStatus.ACTIVE:
            return
        price = await self.quote_usd(account.terms.market_mint)
        if price is None:
            return

        async def cap() -> Decimal | None:
            return (await self._quote_cap(bucket))[0]

        try:
            await account.top_up(price, held, cap)
        except Exception as ex:
            logger.error(f"Colateral a mais em {name} falhou: {ex}")

    def _open_perps(self) -> dict[str, PerpTerms]:
        """Os buckets de perp com posição aberta -> os termos de cada um."""
        return {
            name: bucket.perp
            for name, bucket in self._buckets.items()
            if bucket.perp is not None and bucket.account.book.position is not None
        }

    async def _book(self, name: str, liq: Liquidation) -> Order | None:
        bucket = self._buckets[name]
        account = bucket.account
        assert isinstance(account, PerpAccount)
        order = await account.book_liquidation(liq)
        if order is None:
            return None
        self._liquidated(name, account, order, liq.liquidated)
        self._check_max_loss(name, bucket)
        return order

    def _liquidated(
        self, name: str, account: PerpAccount, order: Order, liquidated: bool
    ) -> None:
        """Uma saída que o venue fez: liquidação (nada voltou) ou o stop dele."""
        price = order.price  # o do oráculo (`perp_order_from_fill`)
        what = (
            "perp liquidada; o colateral foi perdido"
            if liquidated
            else f"o stop do venue fechou a perp, {order.quote_amount} USDC de volta"
        )
        logger.error(f"Bucket {name}: {what} (a {price})")
        self.gateway.add_event(
            events.PERP_LIQUIDATED if liquidated else events.PERP_VENUE_EXIT,
            {
                "account": account.account_id,
                "market": account.terms.market_mint,
                "direction": str(account.terms.direction),
                "price": price,
                "returned": order.quote_amount,
            },
        )
        self.notifier.send_message(f"[!] {account.account_id}: {what} (a {price})")

    async def resolve_intents(self) -> None:
        """Resolve intenções sem desfecho (A3) e restaura os buckets delas.

        Sob o lock de ordens: nenhuma intenção deste processo está em
        execução enquanto isso roda.
        """
        async with self._lock:
            accounts = await self._resolver.run()
        if not accounts:
            return
        self.wallet.invalidate()
        self._restore_accounts(accounts)

    def _restore_accounts(self, accounts) -> None:
        """Relê do ledger os buckets dessas contas (algo mudou fora de uma ordem)."""
        for name, bucket in self._buckets.items():
            if bucket.account.account_id in accounts:
                bucket.account.restore_from_ledger()
                # uma venda resolvida (ou um rent devolvido) muda o realizado
                self._check_max_loss(name, bucket)

    async def _reconcile_wallet(self) -> None:
        """Por token: a soma das posições abertas do ledger cabe na carteira?

        Uma falta (tokens vendidos por fora, um fill mal registrado) vira um
        evento `reconcile_mismatch` e bloqueia compras do token neste processo;
        o dono confere a carteira. Vendas continuam enquanto a carteira as cobre.
        """
        for mint, expected in self.gateway.open_positions(self._prefix).items():
            balance = await self.wallet.fresh(mint)  # A15: não o índice só
            if balance >= expected * RECONCILE_TOLERANCE:
                continue
            logger.warning(
                f"Carteira tem {balance} de {mint}, mas os buckets somam {expected}: "
                "compras bloqueadas"
            )
            self._block_buys(
                events.RECONCILE_MISMATCH,
                {"mint": mint, "expected": expected, "wallet_balance": balance},
            )

    def _block_buys(self, event: str, payload: dict) -> None:
        """A carteira tem menos de `payload["mint"]` que o ledger: sem compras dele."""
        self._blocked.add(payload["mint"])
        self.gateway.add_event(event, payload)

    async def _check_allocation(self, name: str, new: _Bucket) -> None:
        """Os orçamentos abertos (na mesma moeda) precisam caber na carteira.

        O que um bucket já comprou conta pelo custo: orçamento restante <=
        saldo livre + custo das posições abertas, somando todos os buckets.
        """
        if new.budget_usd is None:
            return
        quote = new.account.input_mint
        same = [
            b
            for b in (*self._buckets.values(), new)
            if b.account.input_mint == quote and b.budget_usd is not None
        ]
        allocated = sum((self._cap(b) or ZERO for b in same), ZERO)
        held = sum((b.account.committed() for b in same), ZERO)
        free = await new.account.get_spendable_balance(quote)
        quote_usd = await self.quote_usd(quote)
        if quote_usd is None:
            raise ValueError(
                f"bucket {name}: sem preço USD de {quote} para conferir o orçamento"
            )
        if allocated > (free + held) * quote_usd:
            raise ValueError(
                f"bucket {name}: orçamentos de {allocated} USD passam do que a "
                f"carteira tem ({free} livres + {held} em posições, a {quote_usd} USD)"
            )

    def _check_max_loss(self, name: str, bucket: _Bucket) -> None:
        limit = bucket.max_loss_usd
        realized = bucket.account.book.realized_usd
        if limit is None or bucket.status != BucketStatus.ACTIVE or realized > -limit:
            return
        bucket.status = BucketStatus.RETIRING
        logger.warning(f"Bucket {name}: prejuízo {realized} atingiu o limite {limit}")
        self.gateway.add_event(
            events.BUCKET_MAX_LOSS,
            {
                "account": bucket.account.account_id,
                "realized_usd": realized,
                "limit": limit,
            },
        )

    def retire(self, name: str, reason: str) -> None:
        """Encerra o bucket (sem compras); quem executa vende o que sobrou."""
        bucket = self._bucket(name)
        if bucket.status == BucketStatus.RETIRING:
            return
        bucket.status = BucketStatus.RETIRING
        logger.warning(f"Bucket {name} encerrado: {reason}")
        self.gateway.add_event(
            events.BUCKET_RETIRED,
            {"account": bucket.account.account_id, "reason": reason},
        )

    def _bucket(self, name: str) -> _Bucket:
        try:
            return self._buckets[name]
        except KeyError:
            raise ValueError(f"bucket {name} não está aberto") from None

    def _cap(self, bucket: _Bucket) -> Decimal | None:
        """O que o bucket ainda pode gastar; None sem orçamento (a carteira toda)."""
        if bucket.budget_usd is None:
            return None
        return remaining_budget(bucket.budget_usd, bucket.account.book.realized_usd)

    async def quote_usd(self, mint: Pubkey | str) -> Decimal | None:
        """USD por unidade de um token de cotação (1 em USDC/USDT)."""
        return (await usd_snapshot(self.prices, [str(mint)])).get(str(mint))

    async def _quote_cap(
        self, bucket: _Bucket
    ) -> tuple[Decimal | None, Decimal | None]:
        """(teto no token de cotação, quote_usd). Teto None: sem orçamento.

        O orçamento é em USD; sem o preço do token de cotação o teto é 0 (a
        compra não passa sem saber se cabe).
        """
        quote_usd = await self.quote_usd(bucket.account.input_mint)
        cap = self._cap(bucket)
        if cap is None:
            return None, quote_usd
        return (cap / quote_usd if quote_usd else ZERO), quote_usd

    async def get_bucket(self, name: str) -> BucketSnapshot:
        bucket = self._bucket(name)
        account = bucket.account
        available = await account.get_spendable_balance(account.input_mint)
        cap, quote_usd = await self._quote_cap(bucket)
        if cap is not None:
            available = min(available, cap)
        return BucketSnapshot(
            bucket=name,
            available=available,
            quote_usd=quote_usd,
            position=account.book.position,
            realized_usd=account.book.realized_usd,
            budget_usd=bucket.budget_usd,
            status=bucket.status,
            pnl_summary=account.book.summary(),
            opened_at=account.opened_at,
            last_exit_at=account.last_exit_at,
            last_exit_price=account.last_exit_price,
        )

    async def submit_order(self, name: str, request: OrderRequest) -> OrderReply:
        bucket = self._bucket(name)
        if request.side == OrderSide.BUY and bucket.status != BucketStatus.ACTIVE:
            return OrderReply.of_rejection(f"bucket {name} encerrado: sem compras")
        async with self._lock:
            reply = await self._place(bucket, request)
        self._check_max_loss(name, bucket)
        await self._close_if_retiring(name, bucket, request.price)
        return reply

    async def _close_if_retiring(
        self, name: str, bucket: _Bucket, price: Decimal
    ) -> None:
        """Um bucket encerrado com sobra aberta (venda parcial) vende o resto.

        Uma tentativa por ordem: se a venda falhar (reserva de SOL, tamanho
        mínimo, breaker, RPC), a próxima ordem tenta de novo.
        """
        if bucket.status == BucketStatus.ACTIVE or bucket.account.book.position is None:
            return
        if bucket.closing:
            return
        logger.warning(f"Bucket {name} encerrado com posição aberta: vendendo o resto")
        bucket.closing = True
        try:
            await self.close_bucket(name, price)
        finally:
            bucket.closing = False

    async def _place(self, bucket: _Bucket, request: OrderRequest) -> OrderReply:
        """Executa e classifica o desfecho; nenhuma exceção de execução escapa."""
        try:
            order = await self._execute(bucket, request)
        except (PolicyDeniedError, DuplicateIntentError) as ex:
            return OrderReply.of_denial(_reasons(ex))
        except (ValueError, SwapRejectedError) as ex:
            # nada foi executado: saldo, orçamento, impacto de preço...
            return OrderReply.of_rejection(str(ex))
        except Exception as ex:
            logger.error(f"Erro ao executar ordem de {bucket.account.account_id}: {ex}")
            return OrderReply.of_error(f"{type(ex).__name__}: {ex}")
        return OrderReply.of_fill(order)

    async def _execute(self, bucket: _Bucket, request: OrderRequest) -> Order:
        account = bucket.account
        if request.side == OrderSide.BUY and str(account.output_mint) in self._blocked:
            raise ValueError(
                f"compras de {account.output_mint} bloqueadas: a carteira tem menos "
                "do que as posições do ledger (reconcile_mismatch)"
            )
        if request.side == OrderSide.SELL:
            return await self._sell(account, request)
        perp = bucket.perp
        if perp is not None and market_of(perp) in self._blocked_perps:
            raise ValueError(
                f"compras da perp {perp.direction} bloqueadas: o venue tem uma "
                "posição que o ledger não conhece (perp_mismatch)"
            )
        cap, _ = await self._quote_cap(bucket)
        return await account.buy(
            request.price,
            request.quantity,
            request.rationale,
            request.idempotency_key,
            limit=cap,
        )

    async def _sell(self, account: BucketAccount, request: OrderRequest) -> Order:
        try:
            return await account.sell(
                request.price,
                request.quantity,
                request.rationale,
                request.idempotency_key,
            )
        except WalletShortfallError as ex:
            self._shortfall(account, ex)
            raise

    def _shortfall(self, account: BucketAccount, ex: WalletShortfallError) -> None:
        """Venda recusada por falta na carteira: bloqueia compras e avisa uma vez.

        A estratégia continua pedindo a venda; ela passa quando o token voltar.
        """
        key = (account.account_id, ex.entry_id)
        if key in self._shortfalls:
            return
        self._shortfalls.add(key)
        logger.error(f"{account.account_id}: {ex}; compras do token bloqueadas")
        self._block_buys(
            events.SELL_SHORTFALL,
            {
                "account": account.account_id,
                "mint": ex.mint,
                "quantity": ex.quantity,
                "wallet_available": ex.available,
            },
        )
        self.notifier.send_message(
            f"[!] {account.account_id}: {ex}. Confira a carteira"
        )

    async def close_bucket(self, name: str, price: Decimal) -> OrderReply | None:
        """Vende a posição aberta do bucket; None se não há posição."""
        position = self._bucket(name).account.book.position
        if position is None:
            return None
        request = OrderRequest(
            OrderSide.SELL,
            position.entry_order.quantity,
            price,
            rationale=f"bucket {name} encerrado",
        )
        return await self.submit_order(name, request)

    # --- rent de volta (A15) ----------------------------------------------------

    @property
    def _rent(self) -> RentRefunds:
        return RentRefunds(self.gateway, self.venue, self.prices, self._prefix)

    async def close_token_account(self, name: str) -> RentRefund | None:
        """Bucket encerrado e sem posição: fecha a conta do token dele.

        Uma tentativa por bucket neste processo (um erro antes do envio deixa
        tentar de novo), só se nada mais precisa da conta: nenhum outro bucket
        ativo usa o token, e o ledger deixa (`RentRefunds.payer`). O rent volta
        para o bucket que o pagou, que pode não ser este.
        """
        bucket = self._bucket(name)
        if bucket.rent_checked or not _retired_flat(bucket):
            return None
        refused = self.gateway.send_refusals()
        if refused:
            # a mesma trava dos swaps; a próxima varredura tenta de novo
            logger.warning(
                f"Conta do token de {name} fica aberta: {'; '.join(refused)}"
            )
            return None
        mint = str(bucket.account.output_mint)
        payer = None if self._mint_in_use(name, mint) else self._rent.payer(mint)
        refund = None
        if payer is not None:
            async with self._lock:
                refund = await self._rent.close(payer, mint)
            if refund is not None:
                self._restore_accounts({payer})
        bucket.rent_checked = True
        return refund

    def _mint_in_use(self, name: str, mint: str) -> bool:
        return any(
            other != name
            and bucket.status == BucketStatus.ACTIVE
            and mint
            in (str(bucket.account.input_mint), str(bucket.account.output_mint))
            for other, bucket in self._buckets.items()
        )

    async def resolve_rent_refunds(self) -> None:
        """Fechamentos enviados sem desfecho (processo morto, RPC sem resposta)."""
        rent = self._rent
        for sent in rent.pending():
            async with self._lock:
                payer = await rent.resolve(sent)
            if payer is not None:
                self._restore_accounts({payer})

    async def aclose(self) -> None:
        await self.venue.aclose()
        if self.perps is not None:
            await self.perps.aclose()


def _retired_flat(bucket: _Bucket) -> bool:
    return (
        bucket.status == BucketStatus.RETIRING and bucket.account.book.position is None
    )


def _reasons(ex: PolicyDeniedError | DuplicateIntentError) -> tuple[str, ...]:
    return ex.reasons if isinstance(ex, PolicyDeniedError) else (str(ex),)
