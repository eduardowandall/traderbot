"""O que uma intenção de perp pede além do swap (A8, D6).

Vai no `TradeIntent.perp` e no ledger (`intents.perp_json`, com
`intents.instrument = 'perp'`). Spot não tem: `perp` é None.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOLANA_MINTS
from trader.shared.models.perp import PerpFill
from trader.shared.spec.terms import AddCollateral, PerpMarket

SPOT = "spot"
PERP = "perp"

# o rent que um pedido da Jupiter Perps trava até o keeper rodar: o pedido e
# a conta de USDC dele (medido no real, A11b run 2, G4)
PERP_REQUEST_LOCK_LAMPORTS = 3_723_640
# o rent da conta de uma posição (medido no real, 2026-10-12): a Jupiter
# nunca a fecha e a reusa, então só a primeira abertura de um mercado e lado
# o paga (A25)
PERP_POSITION_RENT_LAMPORTS = 1_747_520
# os envios de uma ida e volta que a carteira paga: abrir, o stop, fechar
OPEN_SENDS = 3


@dataclass(frozen=True)
class PerpTerms:
    market_mint: str  # o token base do mercado (SOL)
    direction: Direction
    leverage: Decimal
    # o stop da spec em % (A11b: a ordem de stop do venue, D7); None: sem stop
    stop_pct: Decimal | None = None
    # `market.add_collateral` da spec (A12): colateral a mais perto da liquidação
    top_up: AddCollateral | None = None


def stop_level(terms: PerpTerms, fill: PerpFill) -> Decimal:
    """O stop do venue: o da spec ou metade da distância até a liquidação, o
    que vier antes (D7); acima da entrada num vendido, abaixo num comprado.

    A spec de uma perp sempre tem stop (`check_perp`); sem ele (termos de
    teste), vale a metade até a liquidação. O fill da entrada sempre tem a
    liquidação (os dois venues a calculam).
    """
    if fill.liquidation_price is None:
        raise ValueError("stop do venue sem a liquidação da entrada")
    distance = abs(fill.liquidation_price / fill.price - 1) / 2
    if terms.stop_pct is not None:
        distance = min(terms.stop_pct / 100, distance)
    return fill.price * (1 - fill.direction.sign * distance)


def open_sol_lamports(position_rent: int, fee_per_send: int) -> int:
    """O SOL que uma abertura precisa além da reserva de taxas (A26, G4).

    O pedido de abrir trava `PERP_REQUEST_LOCK_LAMPORTS` até o keeper rodar;
    a ordem de stop trava o mesmo enquanto a posição está aberta, e o pedido
    de fechar trava outro ao lado dela: duas travas ao mesmo tempo. Mais o
    rent da conta da posição (`position_rent`, 0 se ela já existe) e os três
    envios a `fee_per_send` (a base mais o teto da priority fee).
    """
    return 2 * PERP_REQUEST_LOCK_LAMPORTS + position_rent + OPEN_SENDS * fee_per_send


def perp_terms_for(
    symbol: str, market: PerpMarket | None, stop_pct: Decimal | None = None
) -> PerpTerms | None:
    """Os termos de perp de um par `BASE-QUOTE` com `market` (None no spot).

    O mesmo para o `serve` (no `hello`) e o backtest: o mercado é a base.
    """
    if market is None:
        return None
    base, _ = SOLANA_MINTS.get_pair(symbol)
    return PerpTerms(
        base.mint, market.direction, market.leverage, stop_pct, market.add_collateral
    )


def terms_to_json(terms: PerpTerms | None) -> str | None:
    if terms is None:
        return None
    data = {
        "market_mint": terms.market_mint,
        "direction": str(terms.direction),
        "leverage": str(terms.leverage),
        "stop_pct": None if terms.stop_pct is None else str(terms.stop_pct),
    }
    if terms.top_up is not None:  # sem ela, o JSON de antes da A12
        data["top_up"] = terms.top_up.model_dump(mode="json")
    return json.dumps(data, sort_keys=True)


def terms_from_json(data: str | None) -> PerpTerms | None:
    if data is None:
        return None
    raw = json.loads(data)
    stop, rule = raw.get("stop_pct"), raw.get("top_up")
    return PerpTerms(
        raw["market_mint"],
        Direction(raw["direction"]),
        Decimal(raw["leverage"]),
        None if stop is None else Decimal(stop),
        None if rule is None else AddCollateral.model_validate(rule),
    )
