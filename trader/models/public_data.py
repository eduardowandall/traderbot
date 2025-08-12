"""
Dataclasses para dados públicos da API do Mercado Bitcoin.
Estes dados não requerem autenticação para serem acessados.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict


@dataclass
class TickerData:
    """Representa os dados de um ticker do Mercado Bitcoin"""

    buy: Decimal
    timestamp: datetime
    high: Decimal
    last: Decimal
    low: Decimal
    open: Decimal
    pair: str
    sell: Decimal
    vol: Decimal

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TickerData":
        """Cria uma instância TickerData a partir de um dicionário"""
        return cls(
            buy=Decimal(data["buy"]),
            timestamp=datetime.fromtimestamp(data["date"]),
            high=Decimal(data["high"]),
            last=Decimal(data["last"]),
            low=Decimal(data["low"]),
            open=Decimal(data["open"]),
            pair=data["pair"],
            sell=Decimal(data["sell"]),
            vol=Decimal(data["vol"]),
        )


@dataclass
class Candles:
    """Representa os dados de candles do Mercado Bitcoin"""

    close: list[Decimal]
    high: list[Decimal]
    low: list[Decimal]
    open: list[Decimal]
    timestamp: list[int]
    volume: list[Decimal]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Candles":
        """Cria uma instância Candles a partir de um dicionário"""
        return cls(
            close=[Decimal(v) for v in data["c"]],
            high=[Decimal(v) for v in data["h"]],
            low=[Decimal(v) for v in data["l"]],
            open=[Decimal(v) for v in data["o"]],
            volume=[Decimal(v) for v in data["v"]],
            timestamp=data["t"],
        )

    def get_ticker_from_index(self, index: int) -> TickerData:
        """Cria um TickerData a partir de um candle de índice `index`"""
        return TickerData(
            buy=self.close[index],
            timestamp=datetime.fromtimestamp(self.timestamp[index]),
            high=self.high[index],
            last=self.close[index],
            low=self.low[index],
            open=self.open[index],
            pair="BTC-BRL",
            sell=self.close[index],
            vol=self.volume[index],
        )
