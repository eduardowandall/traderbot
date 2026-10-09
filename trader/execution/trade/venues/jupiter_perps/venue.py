"""`JupiterPerpsVenue`: a Jupiter Perps de verdade (A11b), só no `serve real`.

Cada ação é um pedido (`requests.py`) assinado e enviado pelo mesmo caminho
dos swaps (`OnChainExecutor.send_instructions`: programas conferidos, uma
simulação com a carteira, o envio gravado antes, a confirmação); depois um
keeper da Jupiter o executa ou recusa. O venue espera o keeper lendo a conta
do pedido e a da posição:

- a posição mudou como pedido: executado; o fill vem da conta da posição
  (abrir) ou do USDC que voltou para a carteira (fechar);
- o pedido sumiu e a posição não mudou: recusado (`SwapRejectedError`,
  REJECTED; o keeper devolve o colateral);
- nada em `KEEPER_TIMEOUT_SECONDS`: `TransactionSubmittedError` (UNCONFIRMED:
  o modo fica bloqueado até o dono conferir a posição).

A primeira abertura num mercado e lado cria a conta da posição; o rent dela
fica com a Jupiter (a conta não fecha, é reusada) e vai nos custos dessa
perna (A11c F1). A ordem de stop no venue (D7) é colocada pela conta logo
depois de a entrada estar no ledger (`place_stop`). Uma saída que o venue fez sozinho (o stop
disparou, uma liquidação) aparece na varredura (`liquidations`): a posição
sumiu, e o que voltou é o USDC que a última transação nela deu à carteira.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.perps.reader import (
    JLP_POOL,
    PERPS_PROGRAM,
    SHORT_COLLATERAL,
    USD_SCALE,
    JupiterPerpsReader,
    VenuePosition,
    open_position,
    position_address,
)
from trader.execution.models.errors import SwapRejectedError, TransactionSubmittedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, announce_send
from trader.execution.models.perp import PerpTerms
from trader.execution.models.venue import Liquidation
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.jupiter.provider import (
    AsyncJupiterProvider,
)
from trader.execution.trade.venues.jupiter_perps.requests import (
    MICRO_PER_LAMPORT,
    PerpRequest,
    budgeted,
    build_transaction,
    close_request,
    open_request,
    stop_request,
    token_account,
)
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import ONCHAIN, TradeCosts, priority_fee_lamports
from trader.shared.models.direction import Direction
from trader.shared.models.perp import PerpFill, liquidation_price

logger = logging.getLogger(__name__)

KEEPER_TIMEOUT_SECONDS = 60  # D8
KEEPER_POLL_SECONDS = 2
# depois de fechar, o keeper apaga a ordem de stop em segundos (no run 1,
# 1 s; A11c F3); a conferência roda sob o lock de ordens: curta
STOP_CLEANUP_SECONDS = 2
# o limite de unidades: o simulado com folga, nunca abaixo disto
MIN_COMPUTE_UNITS = 100_000
UNITS_MARGIN = Decimal("1.3")
# o preço por unidade nunca fica abaixo desta fração do teto: um pedido
# parado na fila é pior que pagar um pouco de prioridade
PRIORITY_FLOOR = Decimal("0.25")
DEFAULT_SLIPPAGE = Decimal("0.01")
PERPS = frozenset({str(PERPS_PROGRAM)})
USDC_MINT = SOLANA_MINTS[SHORT_COLLATERAL]

Announce = Callable[[SentTx], None]


class JupiterPerpsVenue:
    def __init__(
        self,
        executor: OnChainExecutor,
        reader: JupiterPerpsReader,
        provider: AsyncJupiterProvider,  # quotes e a leitura das taxas do spot
        max_priority_fee_lamports: int,
        slippage: Decimal = DEFAULT_SLIPPAGE,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        self.executor = executor
        self.reader = reader
        self.provider = provider
        self.max_priority_fee_lamports = max_priority_fee_lamports
        self.slippage = slippage
        self.sleep = sleep
        # a ordem de stop de cada posição (mercado, lado), para conferir depois
        self._stops: dict[tuple[str, Direction], Pubkey] = {}

    @property
    def owner(self) -> Pubkey:
        return self.executor.pubkey

    def __repr__(self):
        return f"{self.__class__.__name__}({self.owner})"

    # --- abrir e fechar ---------------------------------------------------------

    async def open_perp(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        # antes do envio: o preço e se a conta da posição já existe (F1;
        # uma falha aqui não envia nada)
        oracle, [before] = await asyncio.gather(
            self.reader.oracle_price(terms.market_mint),
            self.reader.lamports([position_address(self.owner, terms)]),
        )
        price = oracle.price
        posted = USDC_MINT.ui_to_raw(collateral)
        size = collateral * terms.leverage
        minimum = await self._swap_minimum(terms, posted)
        request = open_request(
            self.owner, terms, posted, size, price, self.slippage, key, minimum
        )
        signature = await self._send(request, posted, announce_send_required)
        position = await self._wait(request, signature, grows=True)
        assert position is not None  # `_wait` só volta com a posição aberta
        borrow, rent = await asyncio.gather(
            self._after_fill(
                self.reader.borrow_bps_hour(terms.market_mint),
                Decimal(0),
                "taxa de empréstimo",
            ),
            self._position_rent(request.position, before),
        )
        fill = _entry_fill(terms, position, collateral, borrow)
        return ExecutionResult(
            signature,
            collateral_mint,
            terms.market_mint,
            posted,
            _size_raw(terms, fill.size_usd, fill.price),
            costs=TradeCosts(ONCHAIN, rent_lamports=rent),
            perp=fill,
        )

    async def _position_rent(self, position: Pubkey, before: int | None) -> int:
        """O rent da conta que esta abertura criou (F1); 0 se ela já existia."""
        if before is not None:
            return 0
        [lamports] = await self._after_fill(
            self.reader.lamports([position]), [None], "rent da conta da posição"
        )
        return lamports or 0

    async def _after_fill[T](self, read: Awaitable[T], fallback: T, what: str) -> T:
        """Uma leitura depois do fill: nunca levanta (o envio já saiu e o
        fill aconteceu); sem ela, `fallback`, avisado."""
        try:
            return await read
        except Exception as ex:
            logger.warning(f"{what} não lido depois do fill ({ex}): {fallback}")
            return fallback

    async def close_perp(
        self, collateral_mint: str, terms: PerpTerms, key: str
    ) -> ExecutionResult:
        wallet = token_account(self.owner)
        position, oracle, before = await asyncio.gather(
            self.reader.position(self.owner, terms),
            self.reader.oracle_price(terms.market_mint),
            self.reader.token_amount(wallet),
        )
        if position is None:
            raise SwapRejectedError("nenhuma posição perp aberta na Jupiter")
        request = close_request(self.owner, terms, oracle.price, self.slippage, key)
        signature = await self._send(request, 0, announce_send_required)
        await self._wait(request, signature, grows=False)
        # sem a leitura: o que a posição valia ao preço do oráculo
        worth = int(_worth(terms, position, oracle.price) * USD_SCALE)
        after = await self._after_fill(
            self.reader.token_amount(wallet), before + worth, "saldo de USDC"
        )
        returned = max(after - before, 0)
        fill = _exit_fill(terms, position, oracle.price, Decimal(returned) / USD_SCALE)
        return ExecutionResult(
            signature,
            terms.market_mint,
            collateral_mint,
            _size_raw(terms, position.size_usd, position.price),
            returned,
            perp=fill,
        )

    async def _swap_minimum(self, terms: PerpTerms, posted: int) -> int | None:
        """Num comprado o keeper troca o USDC por SOL: o mínimo dessa troca."""
        if terms.direction != Direction.LONG:
            return None
        bps = int(self.slippage * 10_000)
        quote = await self.provider.jupiter_client.get_quote(
            SHORT_COLLATERAL, terms.market_mint, posted, bps
        )
        return int(quote.otherAmountThreshold)

    # --- a ordem de stop no venue (D7) --------------------------------------------

    async def place_stop(
        self, terms: PerpTerms, fill: PerpFill, key: str, announce: Announce
    ) -> str | None:
        """Coloca o stop da posição no venue; devolve o endereço do pedido."""
        level = stop_level(terms, fill)
        custody = await self.reader.custody(terms.market_mint)
        request = stop_request(self.owner, terms, level, custody, key)
        await self._send(request, 0, announce)
        self._stops[(terms.market_mint, terms.direction)] = request.request
        return str(request.request)

    async def stop_left(self, terms: PerpTerms) -> str | None:
        """A ordem de stop ainda no venue depois de fechar (o dono cancela)."""
        request = self._stops.pop((terms.market_mint, terms.direction), None)
        if request is None:
            return None
        # o keeper apaga a ordem logo depois do fechamento: só sobra se
        # continuar lá depois de mais um pouco (F3)
        if await self._gone(request):
            return None
        await self.sleep(STOP_CLEANUP_SECONDS)
        return None if await self._gone(request) else str(request)

    async def _gone(self, address: Pubkey) -> bool:
        [data] = await self.reader.accounts([address])
        return data is None

    async def has_position(self, terms: PerpTerms) -> bool:
        return await self.reader.position(self.owner, terms) is not None

    # --- saídas que o venue fez sozinho -------------------------------------------

    async def liquidations(self, open_: Sequence[PerpTerms]) -> list[Liquidation]:
        """As posições abertas no ledger que sumiram do venue (stop ou liquidação)."""
        addresses = [position_address(self.owner, t) for t in open_]
        accounts = await self.reader.accounts(addresses)
        gone = [
            (terms, address)
            for terms, address, data in zip(open_, addresses, accounts, strict=True)
            if open_position(address, data) is None
        ]
        return list(await asyncio.gather(*(self._venue_exit(*g) for g in gone)))

    async def _venue_exit(self, terms: PerpTerms, address: Pubkey) -> Liquidation:
        returned, oracle = await asyncio.gather(
            self.reader.last_payout(address, self.owner, SHORT_COLLATERAL),
            self.reader.oracle_price(terms.market_mint),
        )
        fill = PerpFill(
            direction=terms.direction,
            leverage=terms.leverage,
            price=oracle.price,
            size_usd=Decimal(0),
            collateral_usd=Decimal(returned) / USD_SCALE,
            fees_usd=Decimal(0),
            borrow_bps_hour=Decimal(0),
            liquidated=returned == 0,
        )
        result = ExecutionResult(
            f"venue-exit-{address}",
            terms.market_mint,
            SHORT_COLLATERAL,
            0,
            returned,
            costs=TradeCosts(ONCHAIN),
            perp=fill,
        )
        return Liquidation(terms, result)

    async def acknowledge(self, terms: PerpTerms) -> None:
        return None  # o venue já não tem a posição

    # --- envio ------------------------------------------------------------------

    async def _send(self, request: PerpRequest, spend: int, announce: Announce) -> str:
        """Simula sem assinar (as unidades), fixa a taxa e envia pelo executor."""
        unsigned = build_transaction(
            self.owner, request, self.max_priority_fee_lamports
        )
        (units, _), recent = await asyncio.gather(
            self.reader.simulate(bytes(unsigned)), self._recent_fee(request)
        )
        limit = max(int(units * UNITS_MARGIN), MIN_COMPUTE_UNITS)
        signed = await self.executor.send_instructions(
            budgeted(request, limit, self._unit_price(limit, recent)),
            partial(_sent, request),
            announce,
            SHORT_COLLATERAL,
            spend,
            PERPS,
        )
        return signed.signature

    async def _recent_fee(self, request: PerpRequest) -> int | None:
        """O percentil 75 recente na pool e na posição; None se não deu."""
        try:
            return await self.reader.priority_fee([JLP_POOL, request.position])
        except Exception as ex:
            logger.warning(f"Taxas recentes indisponíveis ({ex}); usando o teto")
            return None

    def _unit_price(self, limit: int, recent: int | None) -> int:
        """O preço recente entre um piso e o teto da política (a prioridade
        nunca passa de `max_priority_fee_lamports`); sem ele, o teto."""
        cap = self.max_priority_fee_lamports * MICRO_PER_LAMPORT // limit
        if recent is None:
            return cap
        return min(max(recent, int(cap * PRIORITY_FLOOR)), cap)

    async def _wait(
        self, request: PerpRequest, signature: str, grows: bool
    ) -> VenuePosition | None:
        """Espera o keeper: a posição (aberta ou fechada) ou a recusa."""
        for _ in range(KEEPER_TIMEOUT_SECONDS // KEEPER_POLL_SECONDS):
            pending, data = await self.reader.accounts(
                [request.request, request.position]
            )
            position = open_position(request.position, data)
            if (position is not None) == grows:
                return position
            if pending is None:
                raise SwapRejectedError(f"o keeper recusou o pedido {request.request}")
            await self.sleep(KEEPER_POLL_SECONDS)
        raise TransactionSubmittedError(
            f"pedido {request.request} sem execução do keeper em "
            f"{KEEPER_TIMEOUT_SECONDS} s: confira a posição na Jupiter",
            signature=signature,
        )

    # --- depois da execução -------------------------------------------------------

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts:
        """A taxa de rede do nosso pedido (a do keeper é dele), com o rent que
        a abertura já anotou. Nunca levanta."""
        costs = result.costs or TradeCosts(ONCHAIN)
        fee = await self.provider.fetch_fee(result.signature)
        if fee is None:
            return costs
        # uma assinatura (a nossa): o que passa da taxa base é prioridade (F4)
        priority = priority_fee_lamports(fee)
        return replace(costs, fee_lamports=fee, priority_fee_lamports=priority)

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int:
        return await self.provider.fetch_failed_fees(signatures)

    async def aclose(self) -> None:
        # o executor e o provider são do spot (ele fecha)
        await self.reader.aclose()


def announce_send_required(sent: SentTx) -> None:
    """O envio de uma intenção: gravado no ledger antes, ou nada é enviado."""
    announce_send(sent, required=True)


def _sent(request: PerpRequest, signed) -> SentTx:
    return SentTx(
        signed.signature,
        str(request.request),
        str(request.position),
        0,
        request.counter,
        signed.last_valid_block_height,
    )


def _size_raw(terms: PerpTerms, size_usd: Decimal, price: Decimal) -> int:
    return SOLANA_MINTS[terms.market_mint].ui_to_raw(size_usd / price)


def stop_level(terms: PerpTerms, fill: PerpFill) -> Decimal:
    """O stop do venue: o da spec ou metade da distância até a liquidação, o
    que vier antes (D7); acima da entrada num vendido, abaixo num comprado.

    A spec de uma perp sempre tem stop (`check_perp`) e o fill da entrada
    sempre tem a liquidação (`_entry_fill`).
    """
    if terms.stop_pct is None or fill.liquidation_price is None:
        raise ValueError("stop do venue sem o stop da spec ou sem a liquidação")
    to_liquidation = abs(fill.liquidation_price / fill.price - 1) / 2
    distance = min(terms.stop_pct / 100, to_liquidation)
    return fill.price * (1 - fill.direction.sign * distance)


def _entry_fill(
    terms: PerpTerms, position: VenuePosition, collateral: Decimal, borrow: Decimal
) -> PerpFill:
    """A entrada: o que a conta da posição mostra depois do keeper."""
    fill = PerpFill(
        direction=terms.direction,
        leverage=terms.leverage,
        price=position.price,
        size_usd=position.size_usd,
        collateral_usd=position.collateral_usd,
        fees_usd=collateral - position.collateral_usd,
        borrow_bps_hour=borrow,
    )
    return replace(fill, liquidation_price=liquidation_price(fill))


def _worth(terms: PerpTerms, position: VenuePosition, price: Decimal) -> Decimal:
    """O que a posição vale a `price`: o colateral mais o PnL (USD)."""
    held = PerpFill(
        terms.direction,
        terms.leverage,
        position.price,
        position.size_usd,
        position.collateral_usd,
        Decimal(0),
        Decimal(0),
    )
    return held.collateral_usd + held.pnl_usd(price)


def _exit_fill(
    terms: PerpTerms, position: VenuePosition, price: Decimal, returned: Decimal
) -> PerpFill:
    """A saída: o que voltou; taxas e empréstimo juntos, pela diferença do
    que a posição valia ao preço do oráculo."""
    expected = _worth(terms, position, price)
    return PerpFill(
        direction=terms.direction,
        leverage=terms.leverage,
        price=price,
        size_usd=position.size_usd,
        collateral_usd=returned,
        fees_usd=max(expected - returned, Decimal(0)),
        borrow_bps_hour=Decimal(0),
    )
