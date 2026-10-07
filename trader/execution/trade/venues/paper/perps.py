"""`SimulatedPerpsVenue`: Jupiter Perps em paper (A8).

Uma posição por mercado e lado (como na Jupiter), guardada em `perps` no
`paper-wallet.json`; o colateral sai e volta pelo token de cotação da
carteira simulada, e cada perna paga a taxa de rede em SOL como o spot do
paper. O preço é o do oráculo do processo (o hub): o preço spot faz o papel
do oráculo do venue.

As contas são as de `trader/shared/models/perp.py`: 0.06% do tamanho em
cada ponta mais um impacto linear no tamanho, o empréstimo por hora sobre o
tamanho (pago no fechamento) e a liquidação quando o que sobra do colateral
chega a 0.2% do tamanho. Com `borrow_rates` (A10: a custody da Jupiter, ao
vivo), a taxa de empréstimo é a do mercado na abertura e fica na posição;
uma leitura que falha usa a padrão.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Protocol

from trader.execution.market.prices import PriceOracle, price_fn
from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.perp import PerpTerms
from trader.execution.models.venue import Liquidation
from trader.execution.trade.venues.paper.executor import DEFAULT_FEE_LAMPORTS
from trader.execution.trade.venues.paper.wallet import (
    InsufficientFundsError,
    SimulatedWallet,
)
from trader.shared.logging_config import error_text
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import SIMULATED, TradeCosts
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.perp import (
    BPS,
    MAINTENANCE_RATE,
    PERP_FEE_RATE,
    ZERO,
    PerpFill,
    liquidation_price,
    perp_from_dict,
    perp_to_dict,
)

# empréstimo por hora, em bps do tamanho (A10 lê a taxa de verdade)
DEFAULT_BORROW_BPS_HOUR = Decimal(1)
# a taxa ao vivo vale por isto (a utilização muda devagar) e a leitura,
# feita sob o lock de ordens, espera no máximo isto
BORROW_RATE_TTL = timedelta(minutes=5)
BORROW_READ_TIMEOUT_SECONDS = 5
# impacto: bps do tamanho a cada 10 mil USD de tamanho
DEFAULT_IMPACT_BPS_PER_10K = Decimal(1)


logger = logging.getLogger(__name__)


class BorrowRates(Protocol):
    """De onde vem a taxa de empréstimo (o `JupiterPerpsReader`, A10)."""

    async def borrow_bps_hour(self, mint: str) -> Decimal: ...

    async def aclose(self) -> None: ...


def position_key(terms: PerpTerms) -> str:
    return f"{terms.market_mint}:{terms.direction}"


class SimulatedPerpsVenue:
    def __init__(
        self,
        wallet: SimulatedWallet,
        prices: PriceOracle | None,
        fee_lamports: int = DEFAULT_FEE_LAMPORTS,
        priority_fee_lamports: int = 0,
        borrow_bps_hour: Decimal = DEFAULT_BORROW_BPS_HOUR,
        impact_bps_per_10k: Decimal = DEFAULT_IMPACT_BPS_PER_10K,
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
        # a taxa de verdade do mercado (A10); None: `borrow_bps_hour` sempre
        borrow_rates: BorrowRates | None = None,
    ):
        self.wallet = wallet
        self.price_of = price_fn(prices)
        # como no spot do paper: a taxa total por perna (base + o teto da priority)
        self.fee_lamports = fee_lamports + priority_fee_lamports
        self.priority_fee_lamports = priority_fee_lamports
        self.borrow_bps_hour = borrow_bps_hour
        self.impact_bps_per_10k = impact_bps_per_10k
        self.clock = clock
        self.borrow_rates = borrow_rates
        # mint -> (taxa, quando foi lida): a última leitura boa
        self._rates: dict[str, tuple[Decimal, datetime]] = {}

    def __repr__(self):
        return f"{self.__class__.__name__}({self.wallet.path})"

    # --- abrir e fechar ---------------------------------------------------------

    async def open_perp(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        price = await self._price(terms.market_mint)
        borrow = await self._borrow_rate(terms.market_mint)
        fill = self._entry(terms, price, collateral, borrow)
        quote, market = SOLANA_MINTS[collateral_mint], SOLANA_MINTS[terms.market_mint]
        posted = quote.ui_to_raw(collateral)
        size_raw = market.ui_to_raw(fill.size_usd / price)
        record = {
            "fill": perp_to_dict(fill),
            "opened_at": self.clock().isoformat(),
            "collateral_mint": collateral_mint,
            "size_raw": size_raw,
        }
        signature = _signature()
        moves = {collateral_mint: -posted, SOL_MINT: -self.fee_lamports}
        self.wallet.apply_perp(position_key(terms), record, moves, signature)
        return self._result(signature, collateral_mint, terms, posted, size_raw, fill)

    async def close_perp(
        self, collateral_mint: str, terms: PerpTerms, key: str
    ) -> ExecutionResult:
        held = position_key(terms)
        record = self.wallet.perps().get(held)
        if record is None:
            raise SwapRejectedError(f"nenhuma posição perp aberta em {held}")
        fill = self._exit(record, await self._price(terms.market_mint))
        returned = SOLANA_MINTS[collateral_mint].ui_to_raw(fill.collateral_usd)
        signature = _signature()
        moves = {collateral_mint: returned, SOL_MINT: -self.fee_lamports}
        self.wallet.apply_perp(held, None, moves, signature)
        return self._result(
            signature, collateral_mint, terms, record["size_raw"], returned, fill, True
        )

    async def liquidations(self, open_: Sequence[PerpTerms]) -> list[Liquidation]:
        """Das posições `open_`, as que chegaram à margem de manutenção.

        Só as pedidas: a de um bucket que nenhum `connect` abriu fica para
        quando ele abrir (o restore a traz, a varredura confere).
        """
        found = []
        positions = self.wallet.perps()
        for terms in open_:
            key = position_key(terms)
            record = positions.get(key)
            if record is None:
                continue
            fill = self._exit(record, await self._price(terms.market_mint))
            if fill.liquidated:
                found.append(self._liquidate(record, terms, fill))
        return found

    async def acknowledge(self, terms: PerpTerms) -> None:
        """Esquece a posição liquidada (o colateral fica com o venue)."""
        try:
            self.wallet.apply_perp(position_key(terms), None, {})
        except InsufficientFundsError:
            pass  # já esquecida

    def equity_at(self, price: Decimal) -> Decimal:
        """O que fechar as posições abertas a `price` devolveria (0 se liquidada).

        Para o patrimônio do replay (A9), que tem um mercado só.
        """
        return sum(
            (self._exit(r, price).collateral_usd for r in self.wallet.perps().values()),
            ZERO,
        )

    async def place_stop(
        self, terms: PerpTerms, fill: PerpFill, key: str, announce
    ) -> str | None:
        return None  # o paper não guarda stops: o da spec (e a liquidação) bastam

    async def stop_left(self, terms: PerpTerms) -> str | None:
        return None

    async def has_position(self, terms: PerpTerms) -> bool:
        return position_key(terms) in self.wallet.perps()

    def _liquidate(self, record: dict, terms: PerpTerms, fill: PerpFill) -> Liquidation:
        # o venue fica com o colateral: nada volta, e não há taxa de rede.
        # A posição só sai com `acknowledge`, depois de o ledger registrar
        result = self._result(
            f"liquidation-{uuid.uuid4().hex}",
            record["collateral_mint"],
            terms,
            record["size_raw"],
            0,
            fill,
            True,
        )
        return Liquidation(terms, replace(result, costs=TradeCosts(SIMULATED)))

    # --- contas -----------------------------------------------------------------

    def _fees(self, size: Decimal) -> Decimal:
        """Taxa de uma ponta: 0.06% do tamanho + o impacto linear."""
        impact_bps = size / BPS * self.impact_bps_per_10k  # BPS: 10 mil
        return size * PERP_FEE_RATE + size * impact_bps / BPS

    async def _borrow_rate(self, mint: str) -> Decimal:
        """A taxa do mercado (A10): lida no máximo a cada `BORROW_RATE_TTL`.

        A abertura roda sob o lock de ordens do serviço: uma leitura vale por
        alguns minutos (a utilização muda devagar) e espera no máximo
        `BORROW_READ_TIMEOUT_SECONDS`. Se falha, vale a última boa; sem
        nenhuma, a padrão.
        """
        cached = self._rates.get(mint)
        now = self.clock()
        if self.borrow_rates is None or (cached and now - cached[1] < BORROW_RATE_TTL):
            return cached[0] if cached else self.borrow_bps_hour
        try:
            rate = await asyncio.wait_for(
                self.borrow_rates.borrow_bps_hour(mint), BORROW_READ_TIMEOUT_SECONDS
            )
        except Exception as ex:
            fallback = cached[0] if cached else self.borrow_bps_hour
            logger.warning(
                f"Taxa de empréstimo da Jupiter indisponível ({error_text(ex)}); "
                f"usando {fallback} bps/h"
            )
            return fallback
        self._rates[mint] = (rate, now)
        return rate

    def _entry(
        self, terms: PerpTerms, price: Decimal, collateral: Decimal, borrow: Decimal
    ) -> PerpFill:
        size = collateral * terms.leverage
        fees = self._fees(size)
        if fees >= collateral:
            raise SwapRejectedError(f"colateral {collateral} não cobre as taxas {fees}")
        fill = PerpFill(
            direction=terms.direction,
            leverage=terms.leverage,
            price=price,
            size_usd=size,
            collateral_usd=collateral - fees,
            fees_usd=fees,
            borrow_bps_hour=borrow,
        )
        # a mesma conta da liquidação de verdade (`_exit`): taxa com impacto
        return replace(fill, liquidation_price=liquidation_price(fill, fees))

    def _exit(self, record: dict, price: Decimal) -> PerpFill:
        """A saída a `price` agora: o que volta, ou a liquidação (nada volta)."""
        entry = perp_from_dict(record["fill"])
        assert entry is not None
        opened_at = datetime.fromisoformat(record["opened_at"])
        borrow = entry.borrow_at(opened_at, self.clock())
        fees = self._fees(entry.size_usd)
        equity = entry.equity(price, borrow, fees)
        liquidated = equity <= entry.size_usd * MAINTENANCE_RATE
        return replace(
            entry,
            price=price,
            collateral_usd=ZERO if liquidated else equity,
            fees_usd=fees,
            borrow_usd=borrow,
            liquidation_price=None,
            liquidated=liquidated,
        )

    async def _price(self, market_mint: str) -> Decimal:
        price = await self.price_of(market_mint)
        if not price:
            symbol = SOLANA_MINTS.symbol_of(market_mint)
            raise SwapRejectedError(f"sem preço de {symbol} para a perp")
        return price

    def _result(
        self,
        signature: str,
        collateral_mint: str,
        terms: PerpTerms,
        in_amount: int,
        out_amount: int,
        fill: PerpFill,
        closing: bool = False,
    ) -> ExecutionResult:
        # entrada: colateral -> mercado; saída: mercado -> colateral
        spend, receive = collateral_mint, terms.market_mint
        if closing:
            spend, receive = receive, spend
        costs = TradeCosts(
            source=SIMULATED,
            fee_lamports=self.fee_lamports,
            priority_fee_lamports=self.priority_fee_lamports,
        )
        return ExecutionResult(
            signature, spend, receive, in_amount, out_amount, costs=costs, perp=fill
        )

    # --- depois da execução (nada a buscar: o paper já sabe) -------------------

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts:
        return result.costs or TradeCosts(SIMULATED)

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int:
        return 0

    async def aclose(self) -> None:
        if self.borrow_rates is not None:
            await self.borrow_rates.aclose()


def _signature() -> str:
    return f"paper-perp-{uuid.uuid4().hex}"
