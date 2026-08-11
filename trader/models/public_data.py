"""
Dataclasses para dados públicos da API Jupiter.
Estes dados não requerem autenticação para serem acessados.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class TickerData:
    buy: Decimal
    timestamp: datetime
    high: Decimal
    last: Decimal
    low: Decimal
    open: Decimal
    pair: str
    sell: Decimal
    vol: Decimal
    spread: Decimal | None = None
