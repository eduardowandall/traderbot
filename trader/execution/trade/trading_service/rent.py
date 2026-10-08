"""O rent de volta (A15): fechar a conta de token que um bucket abriu.

`RentRefunds` decide pelo ledger se a conta pode fechar e quem pagou o rent,
fecha, grava o desfecho e resolve fechamentos que ficaram sem desfecho. Quem
decide *quando* (bucket encerrado, sem posição, nenhum outro bucket ativo
usando o token) e segura o lock de ordens é o `TradeService`.

No ledger são dois eventos: `rent_refund_sent`, gravado antes do envio (se a
gravação falha, nada é enviado), e `rent_refund`, com o desfecho.
"""

import logging
from dataclasses import asdict, dataclass, fields
from functools import partial

from trader.execution.market.prices import PriceOracle, usd_snapshot
from trader.execution.models.errors import TransactionFailedOnChainError
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.rent import RentRefund
from trader.execution.models.venue import Venue
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger.events import RENT_REFUND, RENT_REFUND_SENT
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.mints import SOL_MINT

logger = logging.getLogger(__name__)


@dataclass
class RentRefunds:
    gateway: TradeGateway
    venue: Venue
    prices: PriceOracle | None
    prefix: str  # as contas do modo no ledger

    def payer(self, mint: str) -> str | None:
        """Quem pagou o rent da conta do token, se o ledger deixa fechá-la.

        Não deixa: SOL; há posição aberta do token; um fechamento dele está
        pendente; nenhuma compra do bot abriu a conta (`Ledger.rent_payer`).
        """
        ledger = self.gateway.ledger
        if mint == SOL_MINT or mint in ledger.open_positions(self.prefix):
            return None
        if any(sent["mint"] == mint for sent in self.pending()):
            return None
        return ledger.rent_payer(self.prefix, mint)

    def pending(self) -> list[dict]:
        """Os fechamentos enviados ainda sem desfecho."""
        return self.gateway.ledger.pending_rent_refunds(self.prefix)

    async def close(self, payer: str, mint: str) -> RentRefund | None:
        """Fecha e grava; uma transação que a rede recusou grava só a taxa."""
        try:
            refund = await self.venue.close_token_account(
                mint, partial(self._announce, payer)
            )
        except TransactionFailedOnChainError as ex:
            # a conta continua aberta: só a taxa foi paga
            signature = ex.signature or ""
            fee = await self.venue.fetch_failed_fees([signature])
            refund = RentRefund(signature, mint, 0, fee)
        if refund is not None:
            await self._record(payer, refund)
        return refund

    async def resolve(self, sent: dict) -> str | None:
        """O desfecho de um fechamento pendente (como as intenções, A3).

        Devolve a conta que pagou, se o desfecho foi gravado; pendente fica
        para a próxima varredura.
        """
        tx = SentTx(**{f.name: sent[f.name] for f in fields(SentTx)})
        outcome = await self.venue.send_outcome(tx)
        if outcome == TxOutcome.PENDING:
            return None
        expired = outcome == TxOutcome.EXPIRED
        fee = 0 if expired else await self.venue.fetch_failed_fees([tx.signature])
        rent = tx.out_amount if outcome == TxOutcome.LANDED else 0
        payer = sent["account"]
        await self._record(payer, RentRefund(tx.signature, tx.input_mint, rent, fee))
        return payer

    def _announce(self, payer: str, sent: SentTx) -> None:
        """Grava o envio antes dele (se falha, nada é enviado)."""
        self.gateway.add_event(
            RENT_REFUND_SENT,
            {"account": payer, "mint": sent.input_mint, **asdict(sent)},
        )

    async def _record(self, payer: str, refund: RentRefund) -> None:
        sol_usd = (await usd_snapshot(self.prices, [SOL_MINT])).get(SOL_MINT)
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
