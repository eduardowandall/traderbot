"""Conversão dos candles crus do `datapi` da Jupiter (camada market).

Fica ao lado do cliente, e não em `trader.shared.market`, para que o provider de
execução possa reutilizá-la sem importar `trader.shared.market` (que importa o
cliente, que carrega `trader.execution.venues`: seria um ciclo).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from trader.shared.models.public_data import TickerData


def _dec(value: Any) -> Decimal:
    # o JSON traz floats: Decimal(str(x)) dá a forma decimal curta (0.1), não a
    # expansão binária exata (0.1000000000000000055511151231257827...)
    return Decimal(str(value))


def candles_to_tickers(candles: list[dict[str, Any]]) -> list[TickerData]:
    """Um `TickerData` por candle (preços em USD)."""
    return [
        TickerData(
            # hora local sem fuso, como sempre foi; `indicators.to_utc`
            # normaliza quando precisa comparar com ticks UTC
            timestamp=datetime.fromtimestamp(candle["time"]),
            high=_dec(candle["high"]),
            low=_dec(candle["low"]),
            open=_dec(candle["open"]),
            last=_dec(candle["close"]),
        )
        for candle in candles
    ]
