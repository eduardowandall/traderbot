"""Relatórios do ledger: PnL e custos por conta (bucket)."""

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from trader.ledger.store import LedgerStore, _int
from trader.models import SOLANA_MINTS
from trader.models.costs import LAMPORTS_PER_SOL
from trader.models.intent import IntentStatus


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
    failed_fee_lamports: int = 0  # taxas de transações que falharam
    unknown_costs: int = 0  # pernas cujos custos reais não foram obtidos

    def __post_init__(self) -> None:
        # linhas antigas não têm quote_mint: o par está no nome da conta
        # (ex: "paper:SOL-USDC" -> USDC)
        if self.quote_symbol == "?" and "-" in self.account:
            self.quote_symbol = self.account.rsplit("-", 1)[1]

    @property
    def paid_sol(self) -> Decimal:
        paid = (
            self.fee_lamports
            + self.rent_lamports
            + self.other_lamports
            + self.failed_fee_lamports
        )
        return Decimal(paid) / LAMPORTS_PER_SOL

    def add(self, row: sqlite3.Row) -> None:
        self.trades += 1
        if row["quote_mint"]:
            self.quote_symbol = SOLANA_MINTS.symbol_of(row["quote_mint"])
        self.fee_lamports += _int(row["fee_lamports"])
        self.priority_fee_lamports += _int(row["priority_fee_lamports"])
        self.rent_lamports += _int(row["rent_lamports"])
        self.other_lamports += _int(row["other_lamports"])
        self.unknown_costs += row["costs_source"] in (None, "quote")
        if row["realized_pnl_usd"] is not None:
            self._add_closed(row)

    def _add_closed(self, row: sqlite3.Row) -> None:
        self.closed += 1
        self.net_usd += Decimal(row["realized_pnl_usd"])
        if row["gross_pnl_quote"] is None:
            self.incomplete += 1  # ordem antiga, sem valores nativos
            return
        gross = Decimal(row["gross_pnl_quote"])
        self.gross_quote += gross
        self.costs_sol += Decimal(row["pnl_costs_sol"] or "0")
        net = row["net_pnl_quote"]
        self.net_quote += gross if net is None else Decimal(net)
        self.incomplete += 0 if row["pnl_complete"] else 1


class Reports(LedgerStore):
    def pnl_totals(self, account: str) -> AccountPnL:
        return self.pnl_report(account).get(account) or AccountPnL(account)

    def pnl_report(self, account: str | None = None) -> dict[str, AccountPnL]:
        """PnL e custos por conta, a partir das intenções executadas."""
        query = "SELECT * FROM intents WHERE status = ?"
        params: tuple = (str(IntentStatus.EXECUTED),)
        if account is not None:
            query += " AND account = ?"
            params += (account,)
        report: dict[str, AccountPnL] = {}
        for row in self.conn.execute(query + " ORDER BY created_at", params):
            report.setdefault(row["account"], AccountPnL(row["account"])).add(row)
        self._add_failed_fees(report, account)
        return report

    def _add_failed_fees(self, report: dict, account: str | None) -> None:
        rows = self.conn.execute(
            "SELECT account, fee_lamports FROM intents WHERE status != ? "
            "AND fee_lamports IS NOT NULL",
            (str(IntentStatus.EXECUTED),),
        ).fetchall()
        for row in rows:
            if account is None or row["account"] == account:
                entry = report.setdefault(row["account"], AccountPnL(row["account"]))
                entry.failed_fee_lamports += row["fee_lamports"]

    def total_realized_pnl(self, account: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT realized_pnl_usd FROM intents WHERE account = ? "
            "AND realized_pnl_usd IS NOT NULL",
            (account,),
        ).fetchall()
        return sum((Decimal(r["realized_pnl_usd"]) for r in rows), Decimal("0"))
