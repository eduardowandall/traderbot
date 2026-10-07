"""O que uma intenção de perp pede além do swap (A8, D6).

Vai no `TradeIntent.perp` e no ledger (`intents.perp_json`, com
`intents.instrument = 'perp'`). Spot não tem: `perp` é None.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOLANA_MINTS
from trader.shared.spec.terms import PerpMarket

SPOT = "spot"
PERP = "perp"


@dataclass(frozen=True)
class PerpTerms:
    market_mint: str  # o token base do mercado (SOL)
    direction: Direction
    leverage: Decimal
    # o stop da spec em % (A11b: a ordem de stop do venue, D7); None: sem stop
    stop_pct: Decimal | None = None


def perp_terms_for(
    symbol: str, market: PerpMarket | None, stop_pct: Decimal | None = None
) -> PerpTerms | None:
    """Os termos de perp de um par `BASE-QUOTE` com `market` (None no spot).

    O mesmo para o `serve` (no `hello`) e o backtest: o mercado é a base.
    """
    if market is None:
        return None
    base, _ = SOLANA_MINTS.get_pair(symbol)
    return PerpTerms(base.mint, market.direction, market.leverage, stop_pct)


def terms_to_json(terms: PerpTerms | None) -> str | None:
    if terms is None:
        return None
    return json.dumps(
        {
            "market_mint": terms.market_mint,
            "direction": str(terms.direction),
            "leverage": str(terms.leverage),
            "stop_pct": None if terms.stop_pct is None else str(terms.stop_pct),
        },
        sort_keys=True,
    )


def terms_from_json(data: str | None) -> PerpTerms | None:
    if data is None:
        return None
    raw = json.loads(data)
    stop = raw.get("stop_pct")
    return PerpTerms(
        raw["market_mint"],
        Direction(raw["direction"]),
        Decimal(raw["leverage"]),
        None if stop is None else Decimal(stop),
    )
