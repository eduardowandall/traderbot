import os
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional

from trader.account import Position


class BaseReport(ABC):
    """Classe base para persistência de dados do trading bot"""

    @abstractmethod
    def __init__(self, currency: str = "BTC-BRL"):
        pass

    @abstractmethod
    def save_iteration_data(
        self,
        timestamp: datetime,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position],
        unrealized_pnl: Decimal,
        realized_pnl: Decimal,
        position_signal: Optional[str] = None,
    ) -> None:
        """Salva dados de uma iteração do bot"""
        raise NotImplementedError


class Nullreport(BaseReport):
    """Implementação de persistência que não salva dados (padrão)"""

    def __init__(self, currency: str = "BTC-BRL"):  # noqa: ARG002
        """Inicializa o Nullreport - não faz nada"""
        pass

    def save_iteration_data(
        self,
        timestamp: datetime,  # noqa: ARG002
        symbol: str,  # noqa: ARG002
        current_price: Decimal,  # noqa: ARG002
        position: Optional[Position],  # noqa: ARG002
        unrealized_pnl: Decimal,  # noqa: ARG002
        realized_pnl: Decimal,  # noqa: ARG002
        position_signal: Optional[str] = None,  # noqa: ARG002
    ) -> None:
        """Não salva dados - implementação nula"""
        pass


class CsvReport(BaseReport):
    """Implementação de persistência em arquivo CSV"""

    def __init__(self, currency: str = "BTC-BRL"):
        # Criar diretório de dados se não existir
        data_dir = "report/data"
        os.makedirs(data_dir, exist_ok=True)

        # Configurar nome do arquivo baseado na moeda
        filename = f"trading_data_{currency.replace('-', '_')}_{datetime.now().timestamp()}.csv"
        self.filename = os.path.join(data_dir, filename)
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Garante que o arquivo existe e tem cabeçalho"""
        if not os.path.exists(self.filename):
            with open(self.filename, "w") as f:
                f.write(
                    "timestamp,symbol,price,position_type,position_quantity,position_entry_price,unrealized_pnl,realized_pnl,signal\n"
                )

    def save_iteration_data(
        self,
        timestamp: datetime,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position],
        unrealized_pnl: Decimal,
        realized_pnl: Decimal,
        position_signal: Optional[str] = None,
    ) -> None:
        """Salva dados da iteração em arquivo CSV"""
        position_type = position.type if position else ""
        position_quantity = position.entry_order.quantity if position else Decimal("0")
        position_entry_price = position.entry_order.price if position else Decimal("0")
        signal = position_signal or ""

        # Arredondar valores monetários para 2 casas decimais
        current_price_rounded = round(float(current_price), 2)
        position_entry_price_rounded = round(float(position_entry_price), 2)
        unrealized_pnl_rounded = round(float(unrealized_pnl), 2)
        realized_pnl_rounded = round(float(realized_pnl), 2)

        line = (
            f"{timestamp.isoformat()},"
            f"{symbol},"
            f"{current_price_rounded:.2f},"
            f"{position_type},"
            f"{position_quantity},"
            f"{position_entry_price_rounded:.2f},"
            f"{unrealized_pnl_rounded:.2f},"
            f"{realized_pnl_rounded:.2f},"
            f"{signal}\n"
        )

        with open(self.filename, "a") as f:
            f.write(line)


# Mapeamento de persistências disponíveis
report_CLASSES = {
    "null": Nullreport,
    "csv": CsvReport,
}


def get_report_cls(report_name: str):
    """Retorna a classe de persistência baseada no nome"""
    if report_name not in report_CLASSES:
        available = ", ".join(report_CLASSES.keys())
        raise ValueError(
            f"Persistência '{report_name}' não encontrada. Disponíveis: {available}"
        )

    return report_CLASSES[report_name]


def list_report_options() -> list[str]:
    """Retorna lista de opções de persistência disponíveis"""
    return list(report_CLASSES.keys())
