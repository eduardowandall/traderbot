"""Relatórios do ledger: PnL e custos por conta (bucket).

Taxas de transações que falharam na rede (`failed_tx_fee`, eventos: não há
intenção executada para elas) entram nos custos e saem do `net_usd`, como o
livro do bucket faz em memória.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trader.execution.models.intent import IntentSide, IntentStatus
from trader.execution.trade.ledger.store import LedgerStore, _int, _window
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import RoundTripCosts

FAILED_TX_FEE = "failed_tx_fee"
DAILY_REPORT = "daily_report"


@dataclass
class AccountPnL:
    """Totais de uma conta: PnL nativo das posições fechadas e custos pagos."""

    account: str
    quote_symbol: str = "?"
    trades: int = 0  # pernas executadas (compras e vendas)
    closed: int = 0  # posições fechadas
    incomplete: int = 0  # fechadas sem custos convertidos (ou sem dados nativos)
    gross_quote: Decimal = Decimal("0")
    net_quote: Decimal = Decimal("0")
    costs_sol: Decimal = Decimal("0")  # custos das posições fechadas
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

    def add(self, row: sqlite3.Row) -> None:
        self.trades += 1
        if row["quote_mint"]:
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
        self.failed_fee_lamports += int(payload["fee_lamports"])
        usd = payload.get("fee_usd")
        if usd is None:
            self.unknown_costs += 1  # sem preço do SOL
            return
        self.failed_fee_usd += Decimal(usd)
        self.net_usd -= Decimal(usd)


class Reports(LedgerStore):
    def pnl_totals(
        self,
        account: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> AccountPnL:
        """PnL e custos da conta (no intervalo `[start, end)`, UTC, se dado)."""
        totals = AccountPnL(account)
        window, params = _window("created_at", start, end)
        for row in self.conn.execute(
            f"SELECT * FROM intents WHERE status = ? AND account = ?{window} "
            "ORDER BY created_at",
            (str(IntentStatus.EXECUTED), account, *params),
        ):
            totals.add(row)
        window, params = _window("ts", start, end)
        for row in self.conn.execute(
            "SELECT payload FROM events WHERE type = ? "
            f"AND json_extract(payload, '$.account') = ?{window} ORDER BY id",
            (FAILED_TX_FEE, account, *params),
        ):
            totals.add_failed_fee(json.loads(row["payload"]))
        return totals

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
        for row in self.conn.execute(
            f"SELECT * FROM intents WHERE status = ? AND account = ?{window} "
            "ORDER BY created_at",
            (str(IntentStatus.EXECUTED), account, *params),
        ):
            if row["side"] == str(IntentSide.BUY):
                entry = row
                continue
            leg = None if entry is None else _sell_cost(entry, row)
            if leg is not None and _since(row, start):
                totals.add(*leg, closes=bool(row["closes_position"]))
            if row["closes_position"]:
                entry = None
        return totals

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


def _sell_cost(entry: _Fields, sell: _Fields) -> tuple[Decimal, Decimal] | None:
    """(custo, gasto) em USD de uma venda contra a compra da posição.

    Venda parcial: a fração do que a compra recebeu (valores raw). No token de
    cotação quando a venda tem o PnL nativo (convertido pelo preço USD da
    cotação na venda); senão, em USD direto.
    """
    if not (entry["price"] and sell["price"] and entry["out_amount"]):
        return None
    share = Decimal(sell["in_amount"] or entry["out_amount"]) / entry["out_amount"]
    move = Decimal(sell["price"]) / Decimal(entry["price"]) - 1
    net_quote = sell["net_pnl_quote"]
    if net_quote is not None:
        spend = Decimal(entry["spend_amount"]) * share
        quote_usd = Decimal(sell["quote_usd_price"] or "1")
        return (spend * move - Decimal(net_quote)) * quote_usd, spend * quote_usd
    if entry["notional_usd"] is None or sell["realized_pnl_usd"] is None:
        return None
    notional = Decimal(entry["notional_usd"]) * share
    return notional * move - Decimal(sell["realized_pnl_usd"]), notional
