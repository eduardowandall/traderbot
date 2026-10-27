"""Relatórios do ledger: PnL e custos por conta (bucket).

Taxas de transações que falharam na rede (`failed_tx_fee`, eventos: não há
intenção executada para elas) e do stop do venue (`perp_stop_fee`) entram nos
custos e saem do PnL, nativo e em USD, como o livro do bucket faz em memória
(A30). O rent devolvido ao fechar uma conta de token (`rent_refund`, A15) faz
o caminho inverso.

Numa perp, as taxas do venue e o empréstimo de cada fechamento (A31) saem das
pernas (`order_json`): a entrada é seguida por colateral a mais e vendas
parciais, como o livro faz, sem coluna nova.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trader.execution.models.book import added_entry, remainder_entry
from trader.execution.models.intent import (
    POSITION_SIDES,
    TRADE_SIDES,
    IntentSide,
    IntentStatus,
)
from trader.execution.models.perp import terms_from_json
from trader.execution.trade.ledger.events import (
    DAILY_REPORT,
    FAILED_TX_FEE,
    PERP_STOP_FEE,
    RENT_REFUND,
    RENT_REFUND_SENT,
)
from trader.execution.trade.ledger.store import LedgerStore, _int, _window
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import (
    LAMPORTS_PER_SOL,
    RoundTripCosts,
    lamports_usd,
    outside_cost_quote,
)
from trader.shared.models.order import Order, order_from_json
from trader.shared.models.position import Position

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass
class AccountPnL:
    """Totais de uma conta: PnL nativo das posições fechadas e custos pagos."""

    account: str
    quote_symbol: str = "?"
    # converte os custos fora das pernas (A30); o dos fills vence
    quote_mint: str | None = None
    trades: int = 0  # pernas executadas (compras e vendas)
    closed: int = 0  # posições fechadas
    # fechadas sem custos convertidos (ou sem dados nativos), mais custos fora
    # das pernas sem conversão para o token de cotação
    incomplete: int = 0
    gross_quote: Decimal = Decimal("0")
    net_quote: Decimal = Decimal("0")
    # custos das posições fechadas e os de fora das pernas (A30)
    costs_sol: Decimal = Decimal("0")
    net_usd: Decimal = Decimal("0")
    # tudo que foi pago em SOL (inclui posições abertas)
    fee_lamports: int = 0
    priority_fee_lamports: int = 0
    rent_lamports: int = 0
    other_lamports: int = 0
    costs_usd: Decimal = Decimal("0")  # das pernas com preço do SOL
    unknown_costs: int = 0  # pernas cujos custos reais não foram obtidos
    # transações que falharam na rede: só a taxa foi paga
    failed_tx: int = 0
    failed_fee_lamports: int = 0
    failed_fee_usd: Decimal = Decimal("0")
    # rent devolvido ao fechar contas de token (a taxa do fechamento está em
    # `fee_lamports`)
    rent_refund_lamports: int = 0
    # perps fechadas (A31): o que o venue cobrou, dentro de `gross_quote`
    perp_closes: int = 0
    perp_fees_quote: Decimal = Decimal("0")
    perp_borrow_quote: Decimal = Decimal("0")
    # a entrada perp aberta, seguida pelas pernas
    _perp_entry: Order | None = field(default=None, repr=False)

    def add(self, row: sqlite3.Row) -> None:
        self.trades += 1
        if row["quote_mint"]:
            self.quote_mint = row["quote_mint"]
            self.quote_symbol = SOLANA_MINTS.symbol_of(row["quote_mint"])
        self.fee_lamports += _int(row["fee_lamports"])
        self.priority_fee_lamports += _int(row["priority_fee_lamports"])
        self.rent_lamports += _int(row["rent_lamports"])
        self.other_lamports += _int(row["other_lamports"])
        if row["costs_usd"] is not None:
            self.costs_usd += Decimal(row["costs_usd"])
        self.unknown_costs += row["costs_source"] in (None, "quote")
        if row["realized_pnl_usd"] is not None:
            self._add_closed(row)
        if row["perp_json"] is not None and row["order_json"] is not None:
            self._follow_perp(
                IntentSide(row["side"]), order_from_json(row["order_json"])
            )

    def _follow_perp(self, side: IntentSide, order: Order) -> None:
        """Uma perna perp: abre, cresce (colateral a mais) ou fecha a entrada."""
        entry = self._perp_entry
        if side == IntentSide.BUY:
            self._perp_entry = order
        elif entry is not None and side == IntentSide.ADD:
            self._perp_entry = added_entry(entry, order)
        elif entry is not None and side == IntentSide.SELL:
            self._add_perp_close(entry, order)

    def _add_perp_close(self, entry: Order, exit_: Order) -> None:
        """As taxas e o empréstimo de um fechamento, como o livro os conta."""
        pnl = Position(entry, exit_).realized_pnl_detail()
        if pnl is not None and pnl.price_quote is not None:
            self.perp_closes += 1
            self.perp_fees_quote += pnl.perp_fees_quote or ZERO
            self.perp_borrow_quote += pnl.perp_borrow_quote or ZERO
        rest = None if exit_.closes_position else remainder_entry(entry, exit_.quantity)
        self._perp_entry = rest

    def _add_closed(self, row: sqlite3.Row) -> None:
        self.closed += 1
        self.net_usd += Decimal(row["realized_pnl_usd"])
        if row["gross_pnl_quote"] is None:
            self.incomplete += 1  # venda sem valores nativos
            return
        gross = Decimal(row["gross_pnl_quote"])
        self.gross_quote += gross
        self.costs_sol += Decimal(row["pnl_costs_sol"] or "0")
        net = row["net_pnl_quote"]
        self.net_quote += gross if net is None else Decimal(net)
        self.incomplete += 0 if row["pnl_complete"] else 1

    def add_failed_fee(self, payload: dict) -> None:
        """Um evento `failed_tx_fee`: custo do bucket sem trade."""
        self.failed_tx += len(payload.get("signatures") or ()) or 1
        lamports = int(payload["fee_lamports"])
        self.failed_fee_lamports += lamports
        usd = payload.get("fee_usd")
        self._outside(lamports, None if usd is None else Decimal(usd))
        if usd is None:
            self.unknown_costs += 1  # sem preço do SOL
            return
        self.failed_fee_usd += Decimal(usd)
        self.net_usd -= Decimal(usd)

    def add_stop_fee(self, payload: dict) -> None:
        """Um evento `perp_stop_fee`: a taxa do stop do venue (A11c F2)."""
        lamports = int(payload["fee_lamports"])
        self.fee_lamports += lamports
        usd = self._known(payload.get("fee_usd"))
        self._outside(lamports, usd)
        if usd is not None:
            self.costs_usd += usd
            self.net_usd -= usd

    def _outside(self, lamports: int, usd: Decimal | None) -> None:
        """Um custo fora das pernas no PnL nativo (A30; negativo: devolvido)."""
        sol = Decimal(lamports) / LAMPORTS_PER_SOL
        self.costs_sol += sol
        quote = SOLANA_MINTS.get(self.quote_mint or "")
        value = None if quote is None else outside_cost_quote(quote, sol, usd)
        if value is None:
            self.incomplete += 1
            return
        self.net_quote -= value

    def _known(self, usd: Any) -> Decimal | None:
        """O valor USD de um custo fora das pernas; sem preço do SOL, None e
        conta como desconhecido."""
        if usd is None:
            self.unknown_costs += 1
            return None
        return Decimal(usd)

    def add_rent_refund(self, payload: dict) -> None:
        """Um evento `rent_refund`: o rent volta, a taxa do fechamento sai."""
        refund = int(payload["refund_lamports"])
        fee = int(payload["fee_lamports"])
        self.rent_refund_lamports += refund
        self.fee_lamports += fee
        usd = payload.get("net_usd")
        self._outside(fee - refund, None if usd is None else -Decimal(usd))
        if usd is None:
            self.unknown_costs += 1  # sem preço do SOL
            return
        self.net_usd += Decimal(usd)
        self.costs_usd -= Decimal(usd)


class Reports(LedgerStore):
    def pnl_totals(
        self,
        account: str,
        start: datetime | None = None,
        end: datetime | None = None,
        quote_mint: str | None = None,
    ) -> AccountPnL:
        """PnL e custos da conta (no intervalo `[start, end)`, UTC, se dado).

        `quote_mint`: o token de cotação, se a conta ainda não tem fill que o
        diga (converte os custos fora das pernas, A30)."""
        totals = AccountPnL(account, quote_mint=quote_mint)
        window, params = _window("created_at", start, end)
        for row in self.conn.execute(
            f"SELECT * FROM intents WHERE status = ? AND account = ?{window} "
            "AND side IN (?, ?, ?) ORDER BY created_at",
            (str(IntentStatus.EXECUTED), account, *params, *map(str, POSITION_SIDES)),
        ):
            totals.add(row)
        add = {
            FAILED_TX_FEE: totals.add_failed_fee,
            RENT_REFUND: totals.add_rent_refund,
            PERP_STOP_FEE: totals.add_stop_fee,
        }
        for type_, payload in self._account_events(tuple(add), account, start, end):
            add[type_](payload)
        return totals

    def _account_events(
        self,
        types: tuple[str, ...],
        account: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, dict]]:
        """(tipo, payload) dos eventos da conta, em ordem, em `[start, end)`."""
        window, params = _window("ts", start, end)
        marks = ", ".join("?" for _ in types)
        rows = self.conn.execute(
            f"SELECT type, payload FROM events WHERE type IN ({marks}) "
            f"AND json_extract(payload, '$.account') = ?{window} ORDER BY id",
            (*types, account, *params),
        )
        return [(row["type"], json.loads(row["payload"])) for row in rows]

    def last_event(self, type_: str, account: str) -> dict | None:
        """O payload do último evento `type_` da conta (ex: o último stop do
        venue que ela colocou, A12); None se nenhum."""
        row = self.conn.execute(
            "SELECT payload FROM events WHERE type = ? "
            "AND json_extract(payload, '$.account') = ? ORDER BY id DESC LIMIT 1",
            (type_, account),
        ).fetchone()
        return None if row is None else json.loads(row["payload"])

    def rent_payer(self, prefix: str, mint: str) -> str | None:
        """A conta cuja compra abriu a conta de token de `mint`, se ainda aberta.

        A última compra executada (nas contas do prefixo) que pagou rent pelo
        token; None se nenhuma pagou, ou se um `rent_refund` do token devolveu
        o rent depois dela. Uma conta que o bot não abriu não tem pagador.
        """
        row = self.conn.execute(
            "SELECT account, updated_at FROM intents WHERE status = ? "
            "AND receive_mint = ? AND rent_lamports > 0 "
            "AND substr(account, 1, ?) = ? ORDER BY updated_at DESC LIMIT 1",
            (str(IntentStatus.EXECUTED), mint, len(prefix), prefix),
        ).fetchone()
        if row is None:
            return None
        refunded = self.conn.execute(
            "SELECT 1 FROM events WHERE type = ? AND ts > ? "
            "AND json_extract(payload, '$.mint') = ? "
            "AND json_extract(payload, '$.refund_lamports') > 0 LIMIT 1",
            (RENT_REFUND, row["updated_at"], mint),
        ).fetchone()
        return None if refunded else row["account"]

    def pending_rent_refunds(self, prefix: str) -> list[dict]:
        """Fechamentos enviados (`rent_refund_sent`) ainda sem `rent_refund`."""
        rows = self.conn.execute(
            "SELECT payload FROM events AS sent WHERE sent.type = ? "
            "AND substr(json_extract(sent.payload, '$.account'), 1, ?) = ? "
            "AND NOT EXISTS (SELECT 1 FROM events AS done WHERE done.type = ? "
            "AND json_extract(done.payload, '$.signature') = "
            "json_extract(sent.payload, '$.signature')) ORDER BY sent.id",
            (RENT_REFUND_SENT, len(prefix), prefix, RENT_REFUND),
        )
        return [json.loads(row["payload"]) for row in rows]

    def round_trip_costs(
        self,
        account: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> RoundTripCosts:
        """O custo das idas e voltas da conta com a venda em `[start, end)`.

        A compra pode ser de antes de `start`: a posição fecha na janela.
        """
        totals = RoundTripCosts()
        window, params = _window("created_at", None, end)
        entry = None
        # só compras e vendas: o custo de colateral a mais está na entrada, e o
        # de uma ordem no venue nos eventos (A12)
        for row in self.conn.execute(
            f"SELECT * FROM intents WHERE status = ? AND account = ?{window} "
            "AND side IN (?, ?) ORDER BY created_at",
            (str(IntentStatus.EXECUTED), account, *params, *map(str, TRADE_SIDES)),
        ):
            if row["side"] == str(IntentSide.BUY):
                entry = row
                continue
            if entry is not None and _since(row, start):
                _add_leg(totals, entry, row)
            if row["closes_position"]:
                entry = None
        self._outside_legs(totals, account, start, end)
        return totals

    def _outside_legs(
        self,
        totals: RoundTripCosts,
        account: str,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        """O que a ida e volta custou fora das pernas.

        O rent está no custo da ida e volta que abriu a conta e volta aqui
        (A15); a taxa do stop do venue entra aqui (A11c).
        """
        # tipo -> (o campo USD do evento, o sinal no custo)
        signed = {RENT_REFUND: ("net_usd", -1), PERP_STOP_FEE: ("fee_usd", 1)}
        for type_, payload in self._account_events(tuple(signed), account, start, end):
            field, sign = signed[type_]
            if payload.get(field) is not None:
                totals.charge(sign * Decimal(payload[field]))

    def daily_report_sent(self, day: str) -> bool:
        """Já há um `daily_report` do dia (`YYYY-MM-DD`, UTC)?"""
        row = self.conn.execute(
            "SELECT 1 FROM events WHERE type = ? "
            "AND json_extract(payload, '$.day') = ? LIMIT 1",
            (DAILY_REPORT, day),
        ).fetchone()
        return row is not None


# uma linha de `intents` (ou um dict com as mesmas colunas, nos testes)
_Fields = sqlite3.Row | Mapping[str, Any]


def _since(row: sqlite3.Row, start: datetime | None) -> bool:
    return start is None or row["created_at"] >= start.astimezone(UTC).isoformat()


def _add_leg(totals: RoundTripCosts, entry: _Fields, sell: _Fields) -> None:
    """Uma venda contra a compra da posição; o rent de uma perp, à parte (A25)."""
    leg = _sell_cost(entry, sell)
    if leg is None:
        return
    rent = _perp_rent_usd(entry, sell)
    totals.add(leg[0] - rent, leg[1], closes=bool(sell["closes_position"]))
    totals.deposit(rent)


def _perp_rent_usd(entry: _Fields, sell: _Fields) -> Decimal:
    """O rent de contas de posição perp que o PnL desta venda descontou, em USD.

    A parte da entrada que a venda fecha, mais o da própria venda; sem preço do
    SOL, o da outra perna (como o PnL faz). Spot: 0, o rent da conta do token
    volta no encerramento (A15).
    """
    if entry["perp_json"] is None:
        return ZERO
    share = _share(entry, sell)
    return _rent_usd(entry, sell, share) + _rent_usd(sell, entry, ONE)


def _rent_usd(leg: _Fields, other: _Fields, share: Decimal) -> Decimal:
    rent = _int(leg["rent_lamports"])
    sol_usd = leg["sol_usd_price"] or other["sol_usd_price"]
    usd = lamports_usd(rent, None if sol_usd is None else Decimal(sol_usd))
    return ZERO if not rent or usd is None else usd * share


def _share(entry: _Fields, sell: _Fields) -> Decimal:
    """A fração da entrada que a venda fecha (valores raw)."""
    return Decimal(sell["in_amount"] or entry["out_amount"]) / entry["out_amount"]


def _sell_cost(entry: _Fields, sell: _Fields) -> tuple[Decimal, Decimal] | None:
    """(custo, gasto) em USD de uma venda contra a compra da posição.

    Venda parcial: a fração do que a compra recebeu (valores raw). No token de
    cotação quando a venda tem o PnL nativo (convertido pelo preço USD da
    cotação na venda); senão, em USD direto.
    """
    if not (entry["price"] and sell["price"] and entry["out_amount"]):
        return None
    share = _share(entry, sell)
    leverage, sign = _exposure(entry)
    move = (Decimal(sell["price"]) / Decimal(entry["price"]) - 1) * sign
    net_quote = sell["net_pnl_quote"]
    if net_quote is not None:
        # numa perp, o gasto é o colateral: a exposição é ele x alavancagem
        spend = Decimal(entry["spend_amount"]) * share * leverage
        quote_usd = Decimal(sell["quote_usd_price"] or "1")
        return (spend * move - Decimal(net_quote)) * quote_usd, spend * quote_usd
    if entry["notional_usd"] is None or sell["realized_pnl_usd"] is None:
        return None
    # `notional_usd` de uma perp já é a exposição
    notional = Decimal(entry["notional_usd"]) * share
    return notional * move - Decimal(sell["realized_pnl_usd"]), notional


def _exposure(entry: _Fields) -> tuple[Decimal, Decimal]:
    """(alavancagem, sinal do lado) da entrada: (1, 1) no spot (A9)."""
    terms = terms_from_json(entry["perp_json"])
    if terms is None:
        return Decimal(1), Decimal(1)
    return terms.leverage, terms.direction.sign
