# report_terminal.py
from abc import ABC, abstractmethod
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import List

from rich import print
from rich.console import Console
from rich.table import Table

from trader.models.position import Position
from trader.models.public_data import TickerData

# Precisão para cálculos financeiros
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP

console = Console()


class ReportBase(ABC):
    """Base para relatórios de backtest"""

    @abstractmethod
    def generate_report(self, model: str = "summary"):
        raise NotImplementedError

    def add_position_history(self, position_history: List[Position]):
        self.position_history = position_history

    def add_ticker_history(self, ticker_history: List[TickerData]):
        self.ticker_history = ticker_history


class ReportData:
    """Classe para armazenar dados de relatório"""

    class Overview:
        periodo: str
        variacao_preco: Decimal
        total_operacoes: int
        operacoes_lucrativas: int
        operacoes_perdedoras: int

    class TradeStats:
        avg_win: Decimal
        avg_loss: Decimal
        payoff: Decimal
        expectancy: Decimal
        greatest_gain: Decimal
        greatest_loss: Decimal

    class RiskStats:
        capital_curve: List[Decimal]
        max_drawdown: Decimal
        volatility: Decimal
        sharpe: Decimal

    overview: Overview
    trade_stats: TradeStats
    risk_stats: RiskStats
    distributions_by_hour: dict
    distributions_by_weekday: dict


class ReportDataGenerator(ReportBase):
    """Gera dados de relatório"""

    def __init__(self):
        self.report_data = ReportData()

    def generate_report(self, model: str = "summary"):
        self.report_data.overview = ReportData.Overview()
        self.report_data.overview.periodo = (
            f"{self.ticker_history[0].timestamp} → {self.ticker_history[-1].timestamp}"
        )
        self.report_data.overview.variacao_preco = (
            self.ticker_history[-1].last - self.ticker_history[0].last
        )
        self.report_data.overview.total_operacoes = len(self.position_history)
        self.report_data.overview.operacoes_lucrativas = sum(
            1 for pos in self.position_history if pos.realized_pnl > 0
        )
        self.report_data.overview.operacoes_perdedoras = sum(
            1 for pos in self.position_history if pos.realized_pnl < 0
        )
        self.report_data.trade_stats = ReportData.TradeStats()
        self.report_data.trade_stats.avg_win = self._safe_mean(
            [pos.realized_pnl for pos in self.position_history if pos.realized_pnl > 0]
        )
        self.report_data.trade_stats.avg_loss = self._safe_mean(
            [pos.realized_pnl for pos in self.position_history if pos.realized_pnl < 0]
        )
        self.report_data.trade_stats.payoff = (
            abs(self.report_data.trade_stats.avg_win)
            / abs(self.report_data.trade_stats.avg_loss)
            if self.report_data.trade_stats.avg_loss != 0
            else Decimal("0")
        )
        self.report_data.trade_stats.expectancy = self._safe_mean(
            [pos.realized_pnl for pos in self.position_history]
        )
        self.report_data.trade_stats.greatest_gain = max(
            [pos.realized_pnl for pos in self.position_history], default=Decimal("0")
        )
        self.report_data.trade_stats.greatest_loss = min(
            [pos.realized_pnl for pos in self.position_history], default=Decimal("0")
        )
        self.report_data.risk_stats = ReportData.RiskStats()
        self.report_data.risk_stats.capital_curve = self._build_capital_curve()
        self.report_data.risk_stats.max_drawdown = self._calc_max_drawdown(
            self.report_data.risk_stats.capital_curve
        )
        self.report_data.risk_stats.volatility = self._safe_stdev(
            [pos.realized_pnl for pos in self.position_history]
        )
        mean_r = self._safe_mean([pos.realized_pnl for pos in self.position_history])
        # Sharpe: mean / volatility scaled by sqrt(N)
        sharpe = Decimal("0")
        n = len(self.position_history)
        if self.report_data.risk_stats.volatility != 0 and n > 0:
            sharpe = (mean_r / self.report_data.risk_stats.volatility) * Decimal(
                n
            ).sqrt()
        self.report_data.risk_stats.sharpe = sharpe
        self.report_data.distributions_by_hour = self._distribution_by_hour_dict()
        self.report_data.distributions_by_weekday = self._distribution_by_weekday_dict()

    def _safe_mean(self, values: List[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        s = sum(values, Decimal("0"))
        return s / Decimal(len(values))

    def _safe_stdev(self, values: List[Decimal]) -> Decimal:
        # amostral (denominator n-1). retorna 0 se n < 2
        n = len(values)
        if n < 2:
            return Decimal("0")
        mean = self._safe_mean(values)
        # variance as Decimal
        var = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(n - 1)
        # sqrt variance with Decimal
        return var.sqrt()

    def _build_capital_curve(self) -> List[Decimal]:
        c = Decimal("0")
        curve = []
        for pos in self.position_history:
            c += pos.realized_pnl
            curve.append(c)
        return curve

    def _calc_max_drawdown(self, curve: List[Decimal]) -> Decimal:
        if not curve:
            return Decimal("0")
        peak = curve[0]
        max_dd = Decimal("0")
        for value in curve:
            if value > peak:
                peak = value
            dd = peak - value
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _distribution_by_hour_dict(self) -> dict:
        hourly = defaultdict(lambda: Decimal("0"))
        for pos in self.position_history:
            hourly[pos.entry_order.timestamp.hour] += pos.realized_pnl
        return hourly

    def _distribution_by_weekday_dict(self) -> dict:
        week = defaultdict(lambda: Decimal("0"))
        for pos in self.position_history:
            week[pos.entry_order.timestamp.weekday] += pos.realized_pnl
        return week


class ReportTerminal(ReportBase):
    """Relatório no terminal"""

    def generate_report(self, model: str = "summary"):
        if model != "summary":
            raise ValueError("Apenas model='summary' suportado por enquanto")
        data_generator = ReportDataGenerator()
        data_generator.add_position_history(self.position_history)
        data_generator.add_ticker_history(self.ticker_history)
        data_generator.generate_report()
        self.report_data = data_generator.report_data
        self._generate_summary_report()

    # ---------- Public helpers ----------
    def get_total_realized_pnl(self) -> Decimal:
        return sum(
            (
                self.position_history[i].realized_pnl
                for i in range(len(self.position_history))
            ),
            Decimal("0"),
        )

    def get_unrealized_pnl(self) -> Decimal:
        last_pos = self.position_history[-1]
        if getattr(last_pos, "exit_order", None) is None:
            return last_pos.unrealized_pnl(self.ticker_history[-1].last)
        return Decimal("0")

    # ---------- Core report ----------
    def _generate_summary_report(self):
        console.print(
            "[bold green]========== RELATÓRIO DE EXECUÇÃO ==========[/bold green]"
        )

        if not getattr(self, "ticker_history", None) or not getattr(
            self, "position_history", None
        ):
            print("[red]Dados insuficientes para gerar relatório[/red]")
            return

        # Resumo geral
        self._print_overview()

        # Estatísticas por trade
        self._print_trade_stats()

        # Estatísticas de risco
        self._print_risk_stats()

        # Distribuições com heatmap
        self._print_distributions()

        # Hold vs Trading
        self._print_hold_vs_trading()

    # ---------- Sections ----------
    def _print_overview(self):
        console.print("\n[bold cyan]--- Resumo Geral ---[/bold cyan]")
        table = Table("Métrica", "Valor")
        table.add_row(
            "Período",
            f"{self.ticker_history[0].timestamp} → {self.ticker_history[-1].timestamp}",
        )
        table.add_row(
            "Variação de Preço", f"R$ {self.report_data.overview.variacao_preco:.2f}"
        )
        table.add_row(
            "Total de operações", str(self.report_data.overview.total_operacoes)
        )
        if self.report_data.overview.total_operacoes:
            pct_win = (
                Decimal(self.report_data.overview.operacoes_lucrativas)
                / Decimal(self.report_data.overview.total_operacoes)
                * Decimal("100")
            ).quantize(Decimal("0.1"))
        else:
            pct_win = Decimal("0")
        table.add_row(
            "Operações lucrativas",
            f"{self.report_data.overview.operacoes_lucrativas} ({pct_win}%)",
        )
        table.add_row(
            "Operações perdedoras", f"{self.report_data.overview.operacoes_perdedoras}"
        )
        console.print(table)

    def _print_trade_stats(self):
        console.print("\n[bold cyan]--- Estatísticas de Trade ---[/bold cyan]")

        table = Table("Métrica", "Valor")
        table.add_row(
            "Lucro médio por trade", f"R$ {self.report_data.trade_stats.avg_win:.2f}"
        )
        table.add_row(
            "Perda média por trade", f"R$ {self.report_data.trade_stats.avg_loss:.2f}"
        )
        table.add_row("Payoff Ratio", f"{self.report_data.trade_stats.payoff:.2f}")
        table.add_row(
            "Expectância (por trade)",
            f"R$ {self.report_data.trade_stats.expectancy:.2f}",
        )
        table.add_row(
            "Maior ganho (trade)",
            f"R$ {self.report_data.trade_stats.greatest_gain:.2f}",
        )
        table.add_row(
            "Maior perda (trade)",
            f"R$ {self.report_data.trade_stats.greatest_loss:.2f}",
        )
        console.print(table)

    def _print_risk_stats(self):
        console.print("\n[bold cyan]--- Estatísticas de Risco ---[/bold cyan]")

        table = Table("Métrica", "Valor")
        table.add_row(
            "Curva de capital (final)",
            f"R$ {self.report_data.risk_stats.capital_curve[-1]:.2f}"
            if self.report_data.risk_stats.capital_curve
            else "R$ 0.00",
        )
        table.add_row(
            "Max Drawdown", f"R$ {self.report_data.risk_stats.max_drawdown:.2f}"
        )
        table.add_row(
            "Volatilidade (stdev)", f"R$ {self.report_data.risk_stats.volatility:.2f}"
        )
        table.add_row("Sharpe (aprx)", f"{self.report_data.risk_stats.sharpe:.2f}")
        console.print(table)

    def _print_distributions(self):
        console.print("\n[bold cyan]--- Distribuição por Hora do Dia ---[/bold cyan]")
        table_hour = self._distribution_by_hour_table()
        console.print(table_hour)
        console.print("\n[bold cyan]--- Heatmap por Hora ---[/bold cyan]")
        console.print(
            self._distribution_heatmap(
                self.report_data.distributions_by_hour,
                labels=[f"{h:02d}h" for h in range(24)],
            )
        )

        console.print("\n[bold cyan]--- Distribuição por Dia da Semana ---[/bold cyan]")
        table_week = self._distribution_by_weekday_table()
        console.print(table_week)
        console.print("\n[bold cyan]--- Heatmap por Dia ---[/bold cyan]")
        weekday_labels = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
        console.print(
            self._distribution_heatmap(
                self.report_data.distributions_by_weekday, labels=weekday_labels
            )
        )

    def _print_hold_vs_trading(self):
        console.print("\n[bold cyan]--- Análise HOLD vs TRADING ---[/bold cyan]")
        first_entry_price = self.position_history[0].entry_order.price
        first_qty = self.position_history[0].entry_order.quantity
        final_price = self.ticker_history[-1].last

        initial_investment = first_entry_price * first_qty
        hold_pnl = (final_price - first_entry_price) * first_qty
        hold_pct = (
            (hold_pnl / initial_investment * Decimal("100"))
            if initial_investment != 0
            else Decimal("0")
        )
        actual_pnl = self.get_total_realized_pnl() + self.get_unrealized_pnl()
        actual_pct = (
            (actual_pnl / initial_investment * Decimal("100"))
            if initial_investment != 0
            else Decimal("0")
        )

        table = Table("Estratégia", "Retorno (R$)", "Retorno (%)")
        table.add_row("HOLD", f"R$ {hold_pnl:.2f}", f"{hold_pct:+.2f}%")
        table.add_row("TRADING", f"R$ {actual_pnl:.2f}", f"{actual_pct:+.2f}%")
        console.print(table)

        diff = (hold_pnl - actual_pnl).quantize(Decimal("0.01"))
        if diff != 0:
            winner = "HOLD" if diff > 0 else "TRADING"
            console.print(
                f"[bold yellow]{winner} foi MELHOR por R$ {abs(diff):.2f}[/bold yellow]"
            )
        else:
            console.print(
                "[bold yellow]EMPATE: Ambas estratégias tiveram o mesmo resultado[/bold yellow]"
            )

    def _distribution_by_hour_table(self) -> Table:
        d = self.report_data.distributions_by_hour
        table = Table("Hora", "Lucro (R$)")
        for hour in range(24):
            table.add_row(f"{hour:02d}h", f"R$ {d[hour]:.2f}")
        return table

    def _distribution_by_weekday_table(self) -> Table:
        d = self.report_data.distributions_by_weekday
        labels = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        table = Table("Dia", "Lucro (R$)")
        for i in range(7):
            table.add_row(labels[i], f"R$ {d[i]:.2f}")
        return table

    def _distribution_heatmap(
        self, data_dict: dict, labels: List[str], width: int = 30
    ) -> str:
        # Gera linhas com barra proporcional ao valor absoluto relativo ao maior valor absoluto
        # Retorna string pronta para print no console
        values = [data_dict.get(i, Decimal("0")) for i in range(len(labels))]
        if not values:
            return ""
        max_abs = max((abs(v) for v in values), default=Decimal("0"))
        if max_abs == 0:
            # sem variação
            lines = [
                f"{labels[i]:>5} | " + (" " * width) + f" R$ {values[i]:.2f}"
                for i in range(len(labels))
            ]
            return "\n".join(lines)

        out_lines = []
        for i, label in enumerate(labels):
            v = values[i]
            # tamanho da barra proporcional ao valor absoluto
            proportion = abs(v) / max_abs
            bar_len = int(
                (proportion * Decimal(width)).to_integral_value(rounding=ROUND_HALF_UP)
            )
            bar = "█" * bar_len
            sign = "+" if v >= 0 else "-"
            out_lines.append(f"{label:>5} | {sign}{bar:<{width}} R$ {v:.2f}")
        return "\n".join(out_lines)
