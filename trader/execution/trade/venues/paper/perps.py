"""`SimulatedPerpsVenue`: Jupiter Perps em paper (A8).

Uma posição por mercado e lado (como na Jupiter), guardada em `perps` no
`paper-wallet.json`; o colateral sai e volta pelo token de cotação da
carteira simulada, e cada envio paga a taxa de rede em SOL como o spot do
paper. O preço é o do oráculo do processo (o hub): o preço spot faz o papel
do oráculo do venue.

As contas são as de `trader/shared/models/perp.py`: 0.06% do tamanho em
cada ponta mais um impacto linear no tamanho, o empréstimo por hora sobre o
tamanho (pago no fechamento) e a liquidação quando o que sobra do colateral
chega a 0.2% do tamanho. Com `borrow_rates` (A10: a custody da Jupiter, ao
vivo, pelo `PerpsFeed`), a taxa de empréstimo é a do mercado na abertura e
fica na posição; uma leitura que falha usa a padrão.

A12: a ordem de stop do venue fica no registro da posição (`place_stop`) e
dispara na varredura (`liquidations`), como o keeper faria; uma posição pode
fechar em parte e receber colateral; cada envio é gravado antes
(`announce_send`, com o fill calculado), e `resolve_send` o refaz.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Protocol

from trader.execution.market.perps.reader import collateral_mint as borrowed_from
from trader.execution.market.prices import PriceOracle, price_fn
from trader.execution.models.errors import SwapRejectedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import (
    PerpSend,
    PerpSendKind,
    SentTx,
    announce_send,
)
from trader.execution.models.perp import PerpTerms, stop_level
from trader.execution.models.venue import Liquidation, PerpSweep
from trader.execution.trade.venues.paper.executor import DEFAULT_FEE_LAMPORTS
from trader.execution.trade.venues.paper.wallet import (
    InsufficientFundsError,
    SimulatedWallet,
)
from trader.shared.logging_config import error_text
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import SIMULATED, TradeCosts
from trader.shared.models.direction import Direction
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
    scaled,
    with_collateral,
)

# empréstimo por hora, em bps do tamanho (A10 lê a taxa de verdade)
DEFAULT_BORROW_BPS_HOUR = Decimal(1)
# a taxa ao vivo vale por isto (a utilização muda devagar) e a leitura,
# feita sob o lock de ordens, espera no máximo isto
BORROW_RATE_TTL = timedelta(minutes=5)
BORROW_READ_TIMEOUT_SECONDS = 5
# impacto: bps do tamanho a cada 10 mil USD de tamanho
DEFAULT_IMPACT_BPS_PER_10K = Decimal(1)
ONE = Decimal(1)


logger = logging.getLogger(__name__)


class BorrowRates(Protocol):
    """De onde vem a taxa de empréstimo (o `PerpsFeed`, A10, A12)."""

    async def borrow_bps_hour(self, mint: str) -> Decimal: ...

    async def aclose(self) -> None: ...


def position_key(terms: PerpTerms) -> str:
    return f"{terms.market_mint}:{terms.direction}"


def _market_of(key: str) -> tuple[str, Direction]:
    mint, direction = key.split(":")
    return mint, Direction(direction)


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
        # como no spot do paper: a taxa total por envio (base + o teto da priority)
        self.fee_lamports = fee_lamports + priority_fee_lamports
        self.priority_fee_lamports = priority_fee_lamports
        self.borrow_bps_hour = borrow_bps_hour
        self.impact_bps_per_10k = impact_bps_per_10k
        self.clock = clock
        self.borrow_rates = borrow_rates
        # mint -> (taxa, quando foi lida): a última leitura boa
        self._rates: dict[str, tuple[Decimal, datetime]] = {}
        # posição -> raw do colateral que volta: saídas do venue (o stop dele,
        # uma liquidação) informadas e ainda não registradas (`acknowledge`)
        self._exits: dict[str, int] = {}

    def __repr__(self):
        return f"{self.__class__.__name__}({self.wallet.path})"

    # --- abrir, fechar, colateral ---------------------------------------------

    async def open_perp(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        price = await self._price(terms.market_mint)
        # a custody de onde a posição toma emprestado (USDC num vendido, A18)
        borrow = await self._borrow_rate(borrowed_from(terms))
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
        result = self._result(
            _signature(), collateral_mint, terms, posted, size_raw, fill
        )
        self._announce(result, PerpSendKind.OPEN)
        moves = {collateral_mint: -posted, SOL_MINT: -self.fee_lamports}
        self.wallet.apply_perp(position_key(terms), record, moves, result.signature)
        return result

    async def close_perp(
        self,
        collateral_mint: str,
        terms: PerpTerms,
        key: str,
        fraction: Decimal = ONE,
    ) -> ExecutionResult:
        held = position_key(terms)
        record = self._record(held)
        fraction = min(fraction, ONE)
        fill = scaled(
            self._exit(record, await self._price(terms.market_mint)), fraction
        )
        returned = SOLANA_MINTS[collateral_mint].ui_to_raw(fill.collateral_usd)
        size_raw = int(record["size_raw"] * fraction)
        result = self._result(
            _signature(), collateral_mint, terms, size_raw, returned, fill, True
        )
        self._announce(result, PerpSendKind.CLOSE)
        moves = {collateral_mint: returned, SOL_MINT: -self.fee_lamports}
        rest = None if fraction >= ONE else _rest(record, fraction)
        self.wallet.apply_perp(
            held, rest, moves, result.signature, amend=rest is not None
        )
        return result

    async def add_collateral(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        """O colateral da posição cresce; o tamanho, não (A12)."""
        held = position_key(terms)
        record = self._record(held)
        entry = perp_from_dict(record["fill"])
        assert entry is not None
        grown = with_collateral(entry, collateral, self._fees(entry.size_usd))
        posted = SOLANA_MINTS[collateral_mint].ui_to_raw(collateral)
        result = self._result(_signature(), collateral_mint, terms, posted, 0, grown)
        self._announce(result, PerpSendKind.ADD)
        moves = {collateral_mint: -posted, SOL_MINT: -self.fee_lamports}
        amended = {**record, "fill": perp_to_dict(grown)}
        self.wallet.apply_perp(held, amended, moves, result.signature, amend=True)
        return result

    def _held(self, record: dict) -> PerpFill:
        """A posição agora: o colateral menos o empréstimo até aqui, e a
        liquidação desse colateral."""
        entry = perp_from_dict(record["fill"])
        assert entry is not None
        borrow = entry.borrow_at(
            datetime.fromisoformat(record["opened_at"]), self.clock()
        )
        return with_collateral(entry, -borrow, self._fees(entry.size_usd))

    async def check_fresh(self, terms: PerpTerms) -> None:
        return None  # o preço é o do hub, que já recusa um velho (`StalePriceError`)

    # --- a ordem de stop no venue (D7, A12) -------------------------------------

    async def place_stop(
        self, terms: PerpTerms, fill: PerpFill, key: str
    ) -> ExecutionResult:
        """Guarda o nível no registro da posição; a varredura o confere."""
        held = position_key(terms)
        record = self._record(held)
        level = stop_level(terms, fill)
        signature = _signature()
        order = f"paper-stop:{held}"
        result = ExecutionResult(
            signature,
            record["collateral_mint"],
            terms.market_mint,
            0,
            0,
            costs=self._costs(),
            venue_order=order,
        )
        self._announce(result, PerpSendKind.STOP, request=order)
        amended = {**record, "stop": str(level)}
        moves = {SOL_MINT: -self.fee_lamports}
        self.wallet.apply_perp(held, amended, moves, signature, amend=True)
        return result

    async def stop_left(self, terms: PerpTerms, order: str) -> bool:
        return False  # o stop do paper vive no registro: some com a posição

    async def cancel_stop(
        self, terms: PerpTerms, order: str, key: str
    ) -> ExecutionResult:
        return ExecutionResult(
            _signature(), "", terms.market_mint, 0, 0, venue_order=order
        )

    async def has_position(self, terms: PerpTerms) -> bool:
        return position_key(terms) in self.wallet.perps()

    async def open_markets(self) -> list[tuple[str, Direction]]:
        return [_market_of(key) for key in self.wallet.perps()]

    # --- saídas que o venue fez sozinho -----------------------------------------

    async def sweep(self, open_: Sequence[PerpTerms]) -> PerpSweep:
        """Das posições `open_` (uma leitura da carteira), as que o venue fecha
        agora: na margem de manutenção (liquidada, nada volta) ou além do stop
        dele (o que valem); as outras, como estão.

        Só as pedidas: a de um bucket que nenhum `connect` abriu fica para
        quando ele abrir (o restore a traz, a varredura confere).
        """
        swept = PerpSweep([], {})
        positions = self.wallet.perps()
        for terms in open_:
            record = positions.get(position_key(terms))
            if record is not None:
                await self._sweep_one(swept, terms, record)
        return swept

    async def _sweep_one(
        self, swept: PerpSweep, terms: PerpTerms, record: dict
    ) -> None:
        fill = self._exit(record, await self._price(terms.market_mint))
        if fill.liquidated or _stopped(record, fill):
            key = position_key(terms)
            swept.exits.append(self._venue_exit(key, record, terms, fill))
        else:
            swept.held[terms] = self._held(record)

    async def acknowledge(self, terms: PerpTerms) -> None:
        """A saída está no ledger: a posição sai, e o que valia volta."""
        key = position_key(terms)
        returned = self._exits.pop(key, 0)
        record = self.wallet.perps().get(key)
        if record is None:
            return  # já esquecida
        moves = {record["collateral_mint"]: returned} if returned else {}
        try:
            self.wallet.apply_perp(key, None, moves)
        except InsufficientFundsError:
            pass  # outro processo já a tirou

    def equity_at(self, price: Decimal) -> Decimal:
        """O que fechar as posições abertas a `price` devolveria (0 se liquidada).

        Para o patrimônio do replay (A9), que tem um mercado só.
        """
        return sum(
            (self._exit(r, price).collateral_usd for r in self.wallet.perps().values()),
            ZERO,
        )

    def _venue_exit(
        self, key: str, record: dict, terms: PerpTerms, fill: PerpFill
    ) -> Liquidation:
        # o keeper fecha: nada de taxa de rede nossa. A posição só sai com
        # `acknowledge`, depois de o ledger registrar
        returned = SOLANA_MINTS[record["collateral_mint"]].ui_to_raw(
            fill.collateral_usd
        )
        self._exits[key] = returned
        kind = "liquidation" if fill.liquidated else "venue-stop"
        result = self._result(
            f"{kind}-{uuid.uuid4().hex}",
            record["collateral_mint"],
            terms,
            record["size_raw"],
            returned,
            fill,
            True,
        )
        return Liquidation(terms, replace(result, costs=TradeCosts(SIMULATED)))

    # --- resolução (A3, A12) ----------------------------------------------------

    async def resolve_send(
        self, sent: SentTx, terms: PerpTerms
    ) -> ExecutionResult | None:
        """O envio está no registro da carteira (quem chama já conferiu): o
        resultado é o que foi gravado antes dele."""
        perp = sent.perp
        if perp is None:
            raise ValueError(f"envio {sent.signature} sem o pedido da perp")
        return ExecutionResult(
            sent.signature,
            sent.input_mint,
            sent.output_mint,
            sent.in_amount,
            sent.out_amount,
            costs=self._costs(),
            perp=perp_from_dict(perp.fill),
            venue_order=perp.request,
        )

    # --- contas -----------------------------------------------------------------

    def _record(self, key: str) -> dict:
        record = self.wallet.perps().get(key)
        if record is None:
            raise SwapRejectedError(f"nenhuma posição perp aberta em {key}")
        return record

    def _fees(self, size: Decimal) -> Decimal:
        """Taxa de uma ponta: 0.06% do tamanho + o impacto linear."""
        impact_bps = size / BPS * self.impact_bps_per_10k  # BPS: 10 mil
        return size * PERP_FEE_RATE + size * impact_bps / BPS

    async def _borrow_rate(self, mint: str) -> Decimal:
        """A taxa da custody `mint` (A10; a do colateral, A18): lida no
        máximo a cada `BORROW_RATE_TTL`.

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

    def _costs(self) -> TradeCosts:
        return TradeCosts(
            source=SIMULATED,
            fee_lamports=self.fee_lamports,
            priority_fee_lamports=self.priority_fee_lamports,
        )

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
        return ExecutionResult(
            signature,
            spend,
            receive,
            in_amount,
            out_amount,
            costs=self._costs(),
            perp=fill,
        )

    def _announce(
        self,
        result: ExecutionResult,
        kind: PerpSendKind,
        request: str | None = None,
    ) -> None:
        """Grava o envio antes de aplicá-lo (A3), com o fill já calculado: a
        resolução refaz o resultado a partir dele (A12)."""
        send = PerpSend(kind, request=request, fill=perp_to_dict(result.perp))
        announce_send(
            SentTx(
                result.signature,
                result.input_mint,
                result.output_mint,
                result.in_amount,
                result.out_amount,
                sent_at=time.time(),
                perp=send,
            )
        )

    # --- depois da execução (nada a buscar: o paper já sabe) -------------------

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts:
        return result.costs or TradeCosts(SIMULATED)

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int:
        return 0

    async def aclose(self) -> None:
        if self.borrow_rates is not None:
            await self.borrow_rates.aclose()


def _stopped(record: dict, fill: PerpFill) -> bool:
    """O preço passou do stop do venue (num vendido, subindo; num comprado,
    caindo)?"""
    level = record.get("stop")
    if level is None:
        return False
    return fill.direction.sign * (fill.price - Decimal(level)) <= 0


def _rest(record: dict, fraction: Decimal) -> dict:
    """O registro do que fica depois de fechar `fraction` (A12)."""
    entry = perp_from_dict(record["fill"])
    assert entry is not None
    keep = ONE - fraction
    return {
        **record,
        "fill": perp_to_dict(scaled(entry, keep)),
        "size_raw": record["size_raw"] - int(record["size_raw"] * fraction),
    }


def _signature() -> str:
    return f"paper-perp-{uuid.uuid4().hex}"
