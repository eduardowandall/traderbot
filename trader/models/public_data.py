"""
Dataclasses para dados públicos da API do Mercado Bitcoin.
Estes dados não requerem autenticação para serem acessados.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict


@dataclass
class TickerData:
    """Representa os dados de um ticker do Mercado Bitcoin"""

    buy: Decimal
    date: int
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
            date=data["date"],
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

    c: list[str]
    h: list[str]
    last: list[str]
    o: list[str]
    t: list[int]
    v: list[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Candles":
        """Cria uma instância Candles a partir de um dicionário"""
        return cls(
            c=data["c"],
            h=data["h"],
            last=data["l"],
            o=data["o"],
            t=data["t"],
            v=data["v"],
        )
