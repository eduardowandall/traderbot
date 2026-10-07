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

O modo (real/paper) é só do serviço: quem pede ordens nunca o conhece.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.bucket import BucketAccount
from trader.execution.models.errors import (
    SwapRejectedError,
    TransactionFailedOnChainError,
)
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.rent import RENT_REFUND, RENT_REFUND_SENT, RentRefund
from trader.execution.models.venue import Venue
from trader.execution.trade.gateway import (
    DuplicateIntentError,
    PolicyDeniedError,
    TradeGateway,
)
from trader.execution.trade.gateway.account import SpotAccount, WalletShortfallError
from trader.execution.trade.gateway.balances import WalletBalances
from trader.execution.trade.gateway.resolve import IntentResolver
from trader.shared.models import SOLANA_MINTS, Order, OrderSide
from trader.shared.models.mints import SOL_MINT
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
    ):
        self.venue = venue
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
        self._resolver = IntentResolver(gateway, venue, prices)

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
    ) -> None:
        """Cria o bucket e restaura sua posição/PnL do ledger.

        Só lê saldos se há o que conferir: posições no ledger (reconciliação,
        uma vez por serviço) ou um orçamento (alocação).
        """
        if name in self._buckets:
            raise ValueError(f"bucket {name} já está aberto")
        account = SpotAccount(
            self.venue,
            Pubkey.from_string(input_mint),
            Pubkey.from_string(output_mint),
            gateway=self.gateway,
            account_id=self.account_id(name),
            source=source,
            clock=self.clock,
            prices=self.prices,
            wallet=self.wallet,
        )
        bucket = _Bucket(account, budget_usd, max_loss_usd=max_loss_usd)
        self.gateway.open_account(account.account_id)  # antes do restore (ttl)
        account.restore_from_ledger()
        if not self._reconciled:
            await self._reconcile_wallet()
            self._reconciled = True
        await self._check_allocation(name, bucket)
        self._buckets[name] = bucket
        self._check_max_loss(name, bucket)  # já restaurado além do limite?

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
                "reconcile_mismatch",
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
            "bucket_max_loss",
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
            "bucket_retired", {"account": bucket.account.account_id, "reason": reason}
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
            "sell_shortfall",
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

    async def close_token_account(self, name: str) -> RentRefund | None:
        """Bucket encerrado e sem posição: fecha a conta do token dele.

        Uma tentativa por bucket neste processo (um erro antes do envio deixa
        tentar de novo), só se nada mais precisa da conta (`_rent_payer`). O
        rent volta para o bucket que o pagou, que pode não ser este.
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
        payer = self._rent_payer(name, mint)
        refund = None
        if payer is not None:
            async with self._lock:
                refund = await self._close(payer, mint)
        bucket.rent_checked = True
        return refund

    def _rent_payer(self, name: str, mint: str) -> str | None:
        """Quem pagou o rent da conta do token, se ela pode ser fechada.

        Não pode: SOL; outro bucket ativo usa o token; há posição aberta dele
        no ledger; um fechamento dele está pendente; nenhuma compra do bot
        abriu a conta (`Ledger.rent_payer`).
        """
        if mint == SOL_MINT or self._mint_in_use(name, mint):
            return None
        if mint in self.gateway.open_positions(self._prefix):
            return None
        ledger = self.gateway.ledger
        pending = ledger.pending_rent_refunds(self._prefix)
        if any(sent["mint"] == mint for sent in pending):
            return None
        return ledger.rent_payer(self._prefix, mint)

    def _mint_in_use(self, name: str, mint: str) -> bool:
        return any(
            other != name
            and bucket.status == BucketStatus.ACTIVE
            and mint
            in (str(bucket.account.input_mint), str(bucket.account.output_mint))
            for other, bucket in self._buckets.items()
        )

    async def _close(self, payer: str, mint: str) -> RentRefund | None:
        """Fecha e registra; uma transação que a rede recusou registra a taxa."""
        try:
            refund = await self.venue.close_token_account(
                mint, partial(self._announce_close, payer)
            )
        except TransactionFailedOnChainError as ex:
            # a conta continua aberta: só a taxa foi paga
            signature = ex.signature or ""
            fee = await self.venue.fetch_failed_fees([signature])
            refund = RentRefund(signature, mint, 0, fee)
        if refund is not None:
            await self._record_refund(payer, refund)
        return refund

    def _announce_close(self, payer: str, sent: SentTx) -> None:
        """Grava o envio antes dele (se falha, nada é enviado)."""
        self.gateway.add_event(
            RENT_REFUND_SENT,
            {"account": payer, "mint": sent.input_mint, **asdict(sent)},
        )

    async def _record_refund(self, payer: str, refund: RentRefund) -> None:
        sol_usd = await self.quote_usd(SOL_MINT)
        self.gateway.add_event(
            RENT_REFUND,
            {
                "account": payer,
                "mint": refund.mint,
                "signature": refund.signature,
                "refund_lamports": refund.refund_lamports,
                "fee_lamports": refund.fee_lamports,
                "sol_usd": sol_usd,
                "net_usd": None if sol_usd is None else refund.net_sol * sol_usd,
            },
        )
        logger.warning(
            f"Conta de {SOLANA_MINTS.symbol_of(refund.mint)} fechada "
            f"({refund.signature}): {refund.refund_lamports} lamports de rent "
            f"de volta para {payer}, taxa {refund.fee_lamports}"
        )
        self._restore_accounts({payer})

    async def resolve_rent_refunds(self) -> None:
        """Fechamentos enviados sem desfecho (processo morto, RPC sem resposta).

        Como as intenções (A3): a rede diz se entrou; pendente, fica para a
        próxima varredura.
        """
        for sent in self.gateway.ledger.pending_rent_refunds(self._prefix):
            async with self._lock:
                await self._resolve_close(sent)

    async def _resolve_close(self, sent: dict) -> None:
        tx = SentTx(**{f.name: sent[f.name] for f in fields(SentTx)})
        outcome = await self.venue.send_outcome(tx)
        if outcome == TxOutcome.PENDING:
            return
        expired = outcome == TxOutcome.EXPIRED
        fee = 0 if expired else await self.venue.fetch_failed_fees([tx.signature])
        rent = tx.out_amount if outcome == TxOutcome.LANDED else 0
        await self._record_refund(
            sent["account"], RentRefund(tx.signature, tx.input_mint, rent, fee)
        )

    async def aclose(self) -> None:
        await self.venue.aclose()


def _retired_flat(bucket: _Bucket) -> bool:
    return (
        bucket.status == BucketStatus.RETIRING and bucket.account.book.position is None
    )


def _reasons(ex: PolicyDeniedError | DuplicateIntentError) -> tuple[str, ...]:
    return ex.reasons if isinstance(ex, PolicyDeniedError) else (str(ex),)
