"""Relatório diário dos buckets de um modo, pelo notificador (Telegram).

Roda no processo de execução (`run` ou `serve`), o dono do ledger. A cada
minuto confere se o dia anterior (UTC) já foi relatado: um evento
`daily_report` com o dia fica no ledger, então reiniciar não reenvia. Por
bucket: fills do dia, custos pagos, PnL realizado no dia e no total, taxas de
transações que falharam, e a posição aberta marcada a mercado (Price API).
Um dia sem fills nem posições não envia nada, mas fica marcado.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from trader.bot.config import Notifier
from trader.execution import TradeGateway
from trader.ledger import AccountPnL
from trader.ledger.reports import DAILY_REPORT
from trader.market.prices import PriceOf
from trader.models import SOLANA_MINTS, Position
from trader.models.costs import RoundTripCosts, sol_text

logger = logging.getLogger(__name__)

CHECK_SECONDS = 60.0

# preço USD de um mint; None: sem preço


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BucketDay:
    """Um bucket no relatório: o dia, o total e a posição aberta."""

    name: str  # sem o prefixo do modo
    day: AccountPnL
    total: AccountPnL
    position: Position | None = None
    price: Decimal | None = None  # preço atual do token da posição
    # idas e voltas fechadas no dia: o custo de cada uma, tudo somado
    round_trips: RoundTripCosts = field(default_factory=RoundTripCosts)

    @property
    def active(self) -> bool:
        has_fills = self.day.trades or self.day.failed_tx
        return bool(has_fills) or self.position is not None


class DailyReporter:
    def __init__(
        self,
        gateway: TradeGateway,
        mode: str,
        notifier: Notifier,
        price_of: PriceOf | None = None,
        clock: Callable[[], datetime] = _utcnow,
        check_seconds: float = CHECK_SECONDS,
    ):
        self.gateway = gateway
        self.mode = mode
        self.notifier = notifier
        self.price_of = price_of
        self.clock = clock
        self.check_seconds = check_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.send_due()
            except Exception as ex:
                logger.error(f"Relatório diário falhou: {ex}")
            await asyncio.sleep(self.check_seconds)

    async def send_due(self) -> str | None:
        """Relata o dia anterior (UTC) uma vez só; devolve o texto enviado."""
        end = datetime.combine(self.clock().astimezone(UTC).date(), time(), UTC)
        start = end - timedelta(days=1)
        day = start.date().isoformat()
        if self.gateway.ledger.daily_report_sent(day):
            return None
        buckets = [b for b in await self.buckets(start, end) if b.active]
        text = format_report(self.mode, day, buckets) if buckets else None
        if text is not None:
            self.notifier.send_message(text)
        self.gateway.add_event(DAILY_REPORT, {"day": day, "buckets": len(buckets)})
        return text

    async def buckets(self, start: datetime, end: datetime) -> list[BucketDay]:
        prefix = f"{self.mode}:"
        return [
            await self._bucket(account, prefix, start, end)
            for account in self.gateway.ledger.accounts(prefix)
        ]

    async def _bucket(
        self, account: str, prefix: str, start: datetime, end: datetime
    ) -> BucketDay:
        state = self.gateway.restore(account)
        entry = state.open_entry
        position = None if entry is None else Position(entry)
        return BucketDay(
            name=account.removeprefix(prefix),
            day=self.gateway.ledger.pnl_totals(account, start, end),
            total=state.totals,
            position=position,
            price=None if entry is None else await self._price(entry.output_mint),
            round_trips=self.gateway.ledger.round_trip_costs(account, start, end),
        )

    async def _price(self, mint: str) -> Decimal | None:
        if self.price_of is None:
            return None
        try:
            return await self.price_of(mint)
        except Exception as ex:
            logger.warning(f"Sem preço de {mint} para o relatório: {ex}")
            return None


def format_report(mode: str, day: str, buckets: list[BucketDay]) -> str:
    lines = [f"Relatório {mode} de {day} (UTC)"]
    for bucket in buckets:
        lines += [f"{bucket.name}", _day_line(bucket.day)]
        if bucket.round_trips.count:
            lines.append(f"  {bucket.round_trips.describe()}")
        lines.append(_total_line(bucket.total))
        if bucket.position is not None:
            lines.append(_open_line(bucket.position, bucket.price))
    return "\n".join(lines)


def _day_line(day: AccountPnL) -> str:
    paid = day.fee_lamports + day.rent_lamports + day.other_lamports
    line = (
        f"  dia: {day.trades} fill(s), PnL realizado ~${day.net_usd:+.4f}, "
        f"custos {sol_text(paid)} SOL (~${day.costs_usd:.4f})"
    )
    if day.failed_tx:
        line += f", {day.failed_tx} tx falha(s) {sol_text(day.failed_fee_lamports)} SOL"
    return line


def _total_line(total: AccountPnL) -> str:
    flag = f" [!] {total.incomplete} incompleta(s)" if total.incomplete else ""
    return (
        f"  total: PnL ~${total.net_usd:+.4f} em {total.closed} posição(ões) "
        f"fechada(s){flag}"
    )


def _open_line(position: Position, price: Decimal | None) -> str:
    entry = position.entry_order
    token = SOLANA_MINTS.symbol_of(entry.output_mint)
    line = f"  aberto: {entry.quantity:.6f} {token} @ USD {entry.price:.6f}"
    if price is None:
        return line + "; marcação: sem preço"
    return line + f"; marcação ~${position.unrealized_usd(price):+.4f} a {price:.6f}"
