"""
Dataclasses para dados públicos da API Jupiter.
Estes dados não requerem autenticação para serem acessados.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Interval(StrEnum):
    """Timeframe dos candles da Jupiter (e das barras das estratégias)."""

    SECOND_15 = "15_SECOND"
    MINUTE_1 = "1_MINUTE"
    HOUR_1 = "1_HOUR"

    @property
    def seconds(self) -> int:
        return _INTERVAL_SECONDS[self]


_INTERVAL_SECONDS = {
    Interval.SECOND_15: 15,
    Interval.MINUTE_1: 60,
    Interval.HOUR_1: 3600,
}


@dataclass
class TickerData:
    """Um candle (preços em USD); `last` é o fechamento."""

    timestamp: datetime
    high: Decimal
    last: Decimal
    low: Decimal
    open: Decimal
