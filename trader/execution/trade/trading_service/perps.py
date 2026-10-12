"""`PerpDesk`: o lado de perps do `TradeService` (A23; antes, dentro dele).

Monta e guarda as contas dos buckets de perp e responde por tudo que
pergunta ao `PerpVenue` por elas: a conferência das posições do venue contra
as do ledger na primeira abertura (D10, A12), a trava de compras num mercado
que o ledger não conhece (a conta a consulta em `buy`), a varredura das
posições abertas (as saídas que o venue fez sozinho, liquidação ou o stop
dele: ledger, evento, aviso, `acknowledge`) e o colateral a mais. Quem segura
o lock de ordens, os status e o `max_loss` é o `TradeService`.
"""

import logging
from collections.abc import Awaitable, Callable, Collection
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.execution.models.perp import PerpTerms
from trader.execution.models.reconcile import (
    Market,
    MismatchKind,
    market_of,
    reconcile_perps,
)
from trader.execution.models.venue import Liquidation, PerpVenue
from trader.execution.trade.accounts.perp import PerpAccount
from trader.execution.trade.accounts.wallet import WalletBalances
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger import events
from trader.shared.models import Order
from trader.shared.models.perp import PerpFill
from trader.shared.notification.notification_service import Notifier

logger = logging.getLogger(__name__)


class PerpDesk:
    def __init__(
        self, venue: PerpVenue, gateway: TradeGateway, notifier: Notifier, prefix: str
    ):
        self.venue = venue
        self.gateway = gateway
        self.notifier = notifier
        self.prefix = prefix  # as contas do modo no ledger
        # bucket -> a conta dele; só os que o serviço tem abertos são varridos
        self._accounts: dict[str, PerpAccount] = {}
        # (mercado, lado) que o venue tem e o ledger não: compras recusadas
        self._blocked: set[Market] = set()
        self._reconciled = False

    async def open_account(
        self,
        name: str,
        terms: PerpTerms,
        input_mint: Pubkey,
        output_mint: Pubkey,
        wallet: WalletBalances,
        **kw,
    ) -> PerpAccount:
        """A conta de perp do bucket `name`, depois da conferência (uma vez)."""
        account = PerpAccount(
            self.venue,
            terms,
            input_mint,
            output_mint,
            self.gateway,
            wallet,
            check_buy=self._check_buy,
            **kw,
        )
        await self._reconcile_once()
        self._accounts[name] = account
        return account

    async def _reconcile_once(self) -> None:
        """As perps do venue contra as do ledger, numa leitura só (D10, A12).

        Uma vez por serviço. Uma posição que o venue tem e nenhum bucket abriu
        (aberta à mão, um envio sem desfecho) recusa as compras de perp nesse
        mercado e lado no processo; uma que o ledger tem e o venue não é
        registrada pela varredura como uma saída do venue quando o bucket dela
        abre. As duas viram um `perp_mismatch` para o dono conferir.
        """
        if self._reconciled:
            return
        ledger = self.gateway.ledger.open_perp_markets(self.prefix)
        venue = await self.venue.open_markets()
        for found in reconcile_perps(ledger, venue):
            if found.kind == MismatchKind.UNKNOWN_TO_LEDGER:
                self._blocked.add((found.market_mint, found.direction))
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
        self._reconciled = True

    def _check_buy(self, terms: PerpTerms) -> None:
        """Levanta se as compras deste mercado e lado estão travadas."""
        if market_of(terms) in self._blocked:
            raise ValueError(
                f"compras da perp {terms.direction} bloqueadas: o venue tem uma "
                "posição que o ledger não conhece (perp_mismatch)"
            )

    async def sweep(
        self, names: Collection[str]
    ) -> tuple[dict[str, Order], dict[str, PerpFill]]:
        """Uma leitura do venue para as posições abertas dos buckets `names`.

        Registra as saídas que o venue fez (no ledger antes do `acknowledge`:
        uma gravação que falha deixa a posição lá, e a próxima varredura tenta
        de novo). Devolve a ordem de saída de cada bucket fechado e a posição,
        como o venue a tem, de cada um que segue aberto.
        """
        open_ = {
            name: account
            for name, account in self._accounts.items()
            if name in names and account.book.position is not None
        }
        if not open_:
            return {}, {}
        by_terms = {account.terms: name for name, account in open_.items()}
        swept = await self.venue.sweep(list(by_terms))
        booked = {}
        for liq in swept.exits:
            name = by_terms[liq.terms]
            order = await self._book_exit(name, open_[name], liq)
            if order is not None:
                booked[name] = order
        held = {by_terms[terms]: fill for terms, fill in swept.held.items()}
        return booked, held

    async def _book_exit(
        self, name: str, account: PerpAccount, liq: Liquidation
    ) -> Order | None:
        order = await account.book_liquidation(liq)
        if order is not None:
            self._report(name, account, order, liq.liquidated)
        await self.venue.acknowledge(liq.terms)
        return order

    def _report(
        self, name: str, account: PerpAccount, order: Order, liquidated: bool
    ) -> None:
        """Liquidação (nada voltou) ou o stop do venue: evento e aviso ao dono."""
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

    async def top_up(
        self,
        name: str,
        held: PerpFill,
        price_of: Callable[[str], Awaitable[Decimal | None]],
        cap: Callable[[], Awaitable[Decimal | None]],
    ) -> None:
        """Colateral a mais (`market.add_collateral`) no bucket `name`, se a
        spec pede; uma falha não para os outros (a próxima varredura tenta de
        novo). `price_of`: USD do mercado; `cap`: o que o orçamento permite."""
        account = self._accounts[name]
        if account.terms.top_up is None:
            return
        price = await price_of(account.terms.market_mint)
        if price is None:
            return
        try:
            await account.top_up(price, held, cap)
        except Exception as ex:
            logger.error(f"Colateral a mais em {name} falhou: {ex}")

    async def aclose(self) -> None:
        await self.venue.aclose()
