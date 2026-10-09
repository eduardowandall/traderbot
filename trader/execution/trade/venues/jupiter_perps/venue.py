"""`JupiterPerpsVenue`: a Jupiter Perps de verdade (A11b), só no `serve real`.

Cada ação é um pedido (`requests.py`) assinado e enviado pelo mesmo caminho
dos swaps (`OnChainExecutor.send_instructions`: programas conferidos, uma
simulação com a carteira, o envio gravado antes com o que ele pediu
(`SentTx.perp`, A12), o limite de unidades justo, a confirmação); depois um
keeper da Jupiter o executa ou recusa. O venue espera o keeper lendo a conta
do pedido e a da posição:

- a posição mudou como pedido: executado; o fill vem da conta da posição
  (abrir, colateral a mais) ou do USDC que voltou para a carteira (fechar,
  inteira ou uma parte, A12);
- o pedido sumiu e a posição não mudou: recusado (`SwapRejectedError`,
  REJECTED; o keeper devolve o colateral). A taxa e o rent que o pedido
  pagou entram como custo do bucket (A12: `fetch_failed_fees`);
- nada em `KEEPER_TIMEOUT_SECONDS`: `TransactionSubmittedError` (UNCONFIRMED:
  a resolução, A3, pergunta de novo a cada varredura, `resolve_send`).

Uma abertura recusa um oráculo parado antes de enviar (`PerpsFeed`). A
primeira abertura num mercado e lado cria a conta da posição; o rent dela
fica com a Jupiter (a conta não fecha, é reusada) e vem do `meta` da
transação do pedido (A11c F1, A12). A ordem de stop no venue (D7) é uma
intenção própria da conta (`place_stop`); a que sobra depois de um fechamento
é cancelada (`cancel_stop`). Uma saída que o venue fez sozinho (o stop
disparou, uma liquidação) aparece na varredura (`liquidations`): a posição
sumiu, e o que voltou é o USDC que a última transação nela deu à carteira.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.market.perps.feed import PerpsFeed
from trader.execution.market.perps.reader import (
    CUSTODIES,
    JLP_POOL,
    PERPS_PROGRAM,
    SHORT_COLLATERAL,
    USD_SCALE,
    VenuePosition,
    collateral_mint,
    open_position,
    position_address,
)
from trader.execution.market.perps.reader import (
    collateral_mint as borrowed_from,
)
from trader.execution.models.errors import SwapRejectedError, TransactionSubmittedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import (
    PerpSend,
    PerpSendKind,
    SentTx,
    announce_send,
)
from trader.execution.models.perp import PerpTerms, stop_level
from trader.execution.models.venue import Liquidation, PerpSweep
from trader.execution.trade.venues.jupiter.compute_budget import ComputeBudget
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.jupiter.provider import AsyncJupiterProvider
from trader.execution.trade.venues.jupiter_perps.requests import (
    Part,
    PerpRequest,
    cancel_request,
    close_request,
    open_request,
    stop_request,
    token_account,
)
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import (
    BASE_FEE_LAMPORTS,
    ONCHAIN,
    TradeCosts,
    priority_fee_lamports,
)
from trader.shared.models.direction import Direction
from trader.shared.models.perp import PerpFill, liquidation_price

logger = logging.getLogger(__name__)

KEEPER_TIMEOUT_SECONDS = 60  # D8
KEEPER_POLL_SECONDS = 2
# depois de fechar, o keeper apaga a ordem de stop em segundos (no run 1,
# 1 s; A11c F3); a conferência roda sob o lock de ordens: curta
STOP_CLEANUP_SECONDS = 2
# a transação confirmada chega ao índice do RPC em segundos (como o
# `get_confirmed_transaction` dos swaps, que espera ~11 s)
COSTS_READ_SECONDS = 12
DEFAULT_SLIPPAGE = Decimal("0.01")
PERPS = frozenset({str(PERPS_PROGRAM)})
USDC_MINT = SOLANA_MINTS[SHORT_COLLATERAL]
ONE = Decimal(1)
# os mercados da pool (a custody do USDC é só colateral) e os dois lados
MARKETS = [
    (mint, side)
    for mint in CUSTODIES
    if mint != SHORT_COLLATERAL
    for side in (Direction.LONG, Direction.SHORT)
]

Done = Callable[[VenuePosition | None], bool]


@dataclass(frozen=True)
class _Before:
    """O que um fechamento leu antes do envio, para o fill ao vivo: a parte
    que sai, o USDC da carteira e o preço do oráculo."""

    closed: VenuePosition
    usdc: int
    price: Decimal


class JupiterPerpsVenue:
    def __init__(
        self,
        executor: OnChainExecutor,
        feed: PerpsFeed,
        provider: AsyncJupiterProvider,  # as quotes de um comprado
        slippage: Decimal = DEFAULT_SLIPPAGE,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        self.executor = executor
        self.feed = feed
        self.reader = feed.reader
        self.provider = provider
        self.slippage = slippage
        self.sleep = sleep
        # as contas de posição possíveis da carteira (uma por mercado e lado)
        self._positions = {
            market: position_address(self.owner, _terms(*market)) for market in MARKETS
        }

    @property
    def owner(self) -> Pubkey:
        return self.executor.pubkey

    def __repr__(self):
        return f"{self.__class__.__name__}({self.owner})"

    # --- abrir, fechar, colateral ---------------------------------------------

    async def open_perp(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        # antes do envio: um oráculo parado recusa (nada sai)
        posted = USDC_MINT.ui_to_raw(collateral)
        oracle, minimum = await asyncio.gather(
            self.feed.fresh_price(terms.market_mint),
            self._swap_minimum(terms, posted),
        )
        request = open_request(
            self.owner,
            terms,
            posted,
            collateral * terms.leverage,
            oracle.price,
            self.slippage,
            key,
            minimum,
        )
        send = PerpSend(PerpSendKind.OPEN, str(request.request), str(request.position))
        sent = await self._send(terms, request, posted, send)
        return await self._wait(sent, terms)

    async def _after_fill[T](self, read: Awaitable[T], fallback: T, what: str) -> T:
        """Uma leitura depois do fill: nunca levanta (o envio já saiu e o
        fill aconteceu); sem ela, `fallback`, avisado."""
        try:
            return await read
        except Exception as ex:
            logger.warning(f"{what} não lido depois do fill ({ex}): {fallback}")
            return fallback

    async def close_perp(
        self,
        collateral_mint: str,
        terms: PerpTerms,
        key: str,
        fraction: Decimal = ONE,
    ) -> ExecutionResult:
        wallet = token_account(self.owner)
        position, oracle, before = await asyncio.gather(
            self.reader.position(self.owner, terms),
            self.feed.oracle_price(terms.market_mint),
            self.reader.token_amount(wallet),
        )
        if position is None:
            raise SwapRejectedError("nenhuma posição perp aberta na Jupiter")
        whole = fraction >= ONE
        closed = position if whole else _scaled(position, fraction)
        part = None if whole else Part(closed.size_usd, closed.collateral_usd)
        request = close_request(
            self.owner, terms, oracle.price, self.slippage, key, part
        )
        send = PerpSend(
            PerpSendKind.CLOSE,
            str(request.request),
            str(request.position),
            before=str(position.size_usd),
        )
        # o tamanho que sai fica no envio: a resolução fecha essa quantidade
        size_raw = _size_raw(terms, closed.size_usd, closed.price)
        sent = await self._send(terms, request, 0, send, size_raw)
        return await self._wait(sent, terms, _Before(closed, before, oracle.price))

    async def add_collateral(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        """Um pedido de aumento com tamanho 0: só o colateral entra (A12)."""
        posted = USDC_MINT.ui_to_raw(collateral)
        position, oracle, minimum = await asyncio.gather(
            self.reader.position(self.owner, terms),
            self.feed.oracle_price(terms.market_mint),
            self._swap_minimum(terms, posted),
        )
        if position is None:
            raise SwapRejectedError("nenhuma posição perp aberta na Jupiter")
        request = open_request(
            self.owner,
            terms,
            posted,
            Decimal(0),
            oracle.price,
            self.slippage,
            key,
            minimum,
        )
        send = PerpSend(
            PerpSendKind.ADD,
            str(request.request),
            str(request.position),
            before=str(position.collateral_usd),
        )
        sent = await self._send(terms, request, posted, send)
        return await self._wait(sent, terms)

    async def _held(self, terms: PerpTerms, position: VenuePosition) -> PerpFill:
        """A posição agora: o colateral menos o empréstimo que ela já deve (a
        Jupiter o cobra no fechamento), e a liquidação desse colateral."""
        # o empréstimo corre na custody do colateral (USDC num vendido, A12)
        custody = await self.feed.custody(collateral_mint(terms))
        owed = position.borrow_usd(custody)
        return _held_fill(
            terms, replace(position, collateral_usd=position.collateral_usd - owed)
        )

    async def check_fresh(self, terms: PerpTerms) -> None:
        """Um oráculo parado recusa antes da intenção (`StaleOracleError`)."""
        await self.feed.fresh_price(terms.market_mint)

    async def _swap_minimum(self, terms: PerpTerms, posted: int) -> int | None:
        """Num comprado o keeper troca o USDC por SOL: o mínimo dessa troca."""
        if terms.direction != Direction.LONG:
            return None
        bps = int(self.slippage * 10_000)
        quote = await self.provider.jupiter_client.get_quote(
            SHORT_COLLATERAL, terms.market_mint, posted, bps
        )
        return int(quote.otherAmountThreshold)

    # --- a ordem de stop no venue (D7) ------------------------------------------

    async def place_stop(
        self, terms: PerpTerms, fill: PerpFill, key: str
    ) -> ExecutionResult:
        """Coloca o stop da posição no venue; `venue_order` é o pedido dele."""
        level = stop_level(terms, fill)
        custody = await self.feed.custody(terms.market_mint)
        request = stop_request(self.owner, terms, level, custody, key)
        send = PerpSend(PerpSendKind.STOP, str(request.request), str(request.position))
        sent = await self._send(terms, request, 0, send)
        return _order_result(sent.signature, terms, str(request.request))

    async def stop_left(self, terms: PerpTerms, order: str) -> bool:
        """A ordem de stop `order` continua no venue depois de fechar?"""
        request = Pubkey.from_string(order)
        # o keeper apaga a ordem logo depois do fechamento: só sobra se
        # continuar lá depois de mais um pouco (F3)
        if await self._gone(request):
            return False
        await self.sleep(STOP_CLEANUP_SECONDS)
        return not await self._gone(request)

    async def cancel_stop(
        self, terms: PerpTerms, order: str, key: str
    ) -> ExecutionResult:
        """Cancela a ordem de stop que sobrou (A12); o rent dela volta."""
        request = cancel_request(self.owner, terms, Pubkey.from_string(order))
        send = PerpSend(PerpSendKind.CANCEL, order, str(request.position))
        sent = await self._send(terms, request, 0, send)
        return _order_result(sent.signature, terms, order)

    async def _gone(self, address: Pubkey) -> bool:
        [data] = await self.reader.accounts([address])
        return data is None

    async def has_position(self, terms: PerpTerms) -> bool:
        return await self.reader.position(self.owner, terms) is not None

    async def open_markets(self) -> list[tuple[str, Direction]]:
        """As posições abertas da carteira, numa leitura só (D10)."""
        markets = list(self._positions)
        accounts = await self.reader.accounts(list(self._positions.values()))
        return [
            market
            for market, data in zip(markets, accounts, strict=True)
            if open_position(self._positions[market], data) is not None
        ]

    # --- saídas que o venue fez sozinho -----------------------------------------

    async def sweep(self, open_: Sequence[PerpTerms]) -> PerpSweep:
        """As posições abertas no ledger numa leitura só: as que sumiram do
        venue (o stop dele, uma liquidação) e as outras, como estão."""
        addresses = [position_address(self.owner, t) for t in open_]
        accounts = await self.reader.accounts(addresses)
        gone, held = [], {}
        for terms, address, data in zip(open_, addresses, accounts, strict=True):
            position = open_position(address, data)
            if position is None:
                gone.append(self._venue_exit(terms, address))
            else:
                held[terms] = await self._held(terms, position)
        return PerpSweep(list(await asyncio.gather(*gone)), held)

    async def _venue_exit(self, terms: PerpTerms, address: Pubkey) -> Liquidation:
        returned, fill = await self._payout(terms, address)
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

    async def _payout(self, terms: PerpTerms, address: Pubkey) -> tuple[int, PerpFill]:
        """O que o keeper deu à carteira na última transação da posição (raw
        de USDC) e a saída a esse valor, ao preço do oráculo agora; 0 é uma
        liquidação."""
        returned, oracle = await asyncio.gather(
            self.reader.last_payout(address, self.owner, SHORT_COLLATERAL),
            self.feed.oracle_price(terms.market_mint),
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
        return returned, fill

    async def acknowledge(self, terms: PerpTerms) -> None:
        return None  # o venue já não tem a posição

    # --- o keeper: a espera ao vivo e a resolução (A3, A12, A20) -------------

    async def resolve_send(
        self, sent: SentTx, terms: PerpTerms
    ) -> ExecutionResult | None:
        """Um envio que entrou na rede: o que o keeper fez com ele (None: ainda
        nada). A mesma conferência da espera ao vivo, sem o que ela leu antes."""
        return await self._outcome(sent, terms, None)

    async def _outcome(
        self, sent: SentTx, terms: PerpTerms, before: _Before | None
    ) -> ExecutionResult | None:
        """Uma leitura do pedido e da posição: a posição mudou como o envio
        pediu (`_done`, primeiro): o fill; o pedido ainda lá: None; sumiu sem
        mudar nada: recusado, com a assinatura (a taxa e o rent dele são do
        bucket). Uma ordem no venue (stop, cancelamento) vale quando entrou.
        """
        perp = sent.perp
        if perp is None or perp.request is None or perp.position is None:
            raise ValueError(f"envio {sent.signature} sem o pedido da perp")
        if perp.kind in (PerpSendKind.STOP, PerpSendKind.CANCEL):
            return _order_result(sent.signature, terms, perp.request)
        request, address = map(Pubkey.from_string, (perp.request, perp.position))
        pending, data = await self.reader.accounts([request, address])
        position = open_position(address, data)
        if _done(perp)(position):
            return await self._fill(sent, perp, terms, position, before)
        if pending is not None:
            return None
        rejected = SwapRejectedError(f"o keeper recusou o pedido {perp.request}")
        rejected.failed_signatures = (sent.signature,)
        raise rejected

    async def _fill(
        self,
        sent: SentTx,
        perp: PerpSend,
        terms: PerpTerms,
        position: VenuePosition | None,
        before: _Before | None,
    ) -> ExecutionResult:
        """O fill de um pedido executado; as leituras daqui nunca levantam."""
        if perp.kind == PerpSendKind.CLOSE:
            return await self._close_fill(sent, terms, before)
        assert position is not None  # abrir e pôr mais: a posição existe
        if perp.kind == PerpSendKind.ADD:
            fill = _held_fill(terms, position)
            return _result(
                sent.signature, sent.input_mint, terms, sent.in_amount, fill, size=False
            )
        borrow = await self._after_fill(
            # a custody de onde a posição toma emprestado (A18)
            self.feed.borrow_bps_hour(borrowed_from(terms)),
            Decimal(0),
            "taxa de empréstimo",
        )
        posted = Decimal(sent.in_amount) / USD_SCALE
        fill = _entry_fill(terms, position, posted, borrow)
        return _result(sent.signature, sent.input_mint, terms, sent.in_amount, fill)

    async def _close_fill(
        self, sent: SentTx, terms: PerpTerms, before: _Before | None
    ) -> ExecutionResult:
        """O que voltou de um fechamento. Ao vivo: o USDC da carteira antes e
        depois (sem a leitura, o que a parte valia ao preço do oráculo), e as
        taxas pela diferença. Resolvido depois: o que a última transação na
        posição (a do keeper) deu à carteira, taxas desconhecidas."""
        if before is None:
            assert sent.perp is not None
            returned, fill = await self._payout(terms, position_of(sent.perp))
        else:
            returned = await self._usdc_back(terms, before)
            back = Decimal(returned) / USD_SCALE
            fill = _exit_fill(terms, before.closed, before.price, back)
        # a quantidade que o envio fechou (o venue a gravou nele)
        return ExecutionResult(
            sent.signature,
            terms.market_mint,
            SHORT_COLLATERAL,
            sent.in_amount,
            returned,
            perp=fill,
        )

    async def _usdc_back(self, terms: PerpTerms, before: _Before) -> int:
        worth = int(_worth(terms, before.closed, before.price) * USD_SCALE)
        after = await self._after_fill(
            self.reader.token_amount(token_account(self.owner)),
            before.usdc + worth,
            "saldo de USDC",
        )
        return max(after - before.usdc, 0)

    # --- envio ------------------------------------------------------------------

    async def _send(
        self,
        terms: PerpTerms,
        request: PerpRequest,
        spend: int,
        send: PerpSend,
        in_amount: int | None = None,
    ) -> SentTx:
        """Envia pelo executor: o limite de unidades sai da simulação dele, o
        preço por unidade das taxas recentes na pool e na posição.

        `in_amount`: o que o envio gravado diz que sai (padrão: `spend`; um
        fechamento grava o tamanho, em raw do mercado). Devolve o envio como
        foi gravado (a espera e a resolução o leem).
        """
        recent = await self._recent_fee(request)
        logged = spend if in_amount is None else in_amount
        signed = await self.executor.send_instructions(
            [request.instruction],
            partial(_sent, terms, logged, send),
            announce_send_required,
            SHORT_COLLATERAL,
            spend,
            PERPS,
            ComputeBudget(recent),
        )
        return _sent(terms, logged, send, signed)

    async def _recent_fee(self, request: PerpRequest) -> int | None:
        """O percentil 75 recente na pool e na posição; None se não deu."""
        try:
            return await self.reader.priority_fee([JLP_POOL, request.position])
        except Exception as ex:
            logger.warning(f"Taxas recentes indisponíveis ({ex}); usando o teto")
            return None

    async def _wait(
        self, sent: SentTx, terms: PerpTerms, before: _Before | None = None
    ) -> ExecutionResult:
        """Espera o keeper (`_outcome` a cada `KEEPER_POLL_SECONDS`): o fill, a
        recusa, ou, sem nada em `KEEPER_TIMEOUT_SECONDS`, UNCONFIRMED (a
        resolução pergunta de novo a cada varredura)."""
        for _ in range(KEEPER_TIMEOUT_SECONDS // KEEPER_POLL_SECONDS):
            result = await self._outcome(sent, terms, before)
            if result is not None:
                return result
            await self.sleep(KEEPER_POLL_SECONDS)
        request = sent.perp.request if sent.perp else sent.signature
        raise TransactionSubmittedError(
            f"pedido {request} sem execução do keeper em "
            f"{KEEPER_TIMEOUT_SECONDS} s: confira a posição na Jupiter",
            signature=sent.signature,
        )

    # --- depois da execução -----------------------------------------------------

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts:
        """A taxa de rede do nosso pedido (a do keeper é dele) e o rent da conta
        de posição que ele criou, lidos da transação. Nunca levanta."""
        costs = result.costs or TradeCosts(ONCHAIN)
        read = await self._transaction_costs(result.signature)
        if read is None:
            return costs
        fee, rent = read
        # uma assinatura (a nossa): o que passa da taxa base é prioridade (F4)
        priority = priority_fee_lamports(fee)
        return replace(
            costs, fee_lamports=fee, priority_fee_lamports=priority, rent_lamports=rent
        )

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int:
        """O que pagaram os pedidos que não viraram fill: a taxa e o rent de uma
        conta de posição criada (um pedido recusado pelo keeper, A12). Uma
        leitura que falha conta a taxa base. Nunca levanta."""
        total = 0
        for signature in signatures:
            read = await self._transaction_costs(signature)
            total += BASE_FEE_LAMPORTS if read is None else sum(read)
        return total

    async def _transaction_costs(self, signature: str) -> tuple[int, int] | None:
        """(taxa, rent criado) da transação; logo depois da confirmação o
        índice do RPC pode ainda não tê-la: tenta por `COSTS_READ_SECONDS`."""
        watched = list(self._positions.values())
        for _ in range(COSTS_READ_SECONDS // KEEPER_POLL_SECONDS):
            try:
                read = await self.reader.transaction_costs(signature, watched)
            except Exception as ex:
                logger.warning(f"Custos da transação {signature} não lidos: {ex}")
                return None
            if read is not None:
                return read
            await self.sleep(KEEPER_POLL_SECONDS)
        logger.warning(f"Transação {signature} ainda não indexada: custos sem ela")
        return None

    async def aclose(self) -> None:
        # o executor e o provider são do spot (ele fecha)
        await self.feed.aclose()


def announce_send_required(sent: SentTx) -> None:
    """O envio de uma intenção: gravado no ledger antes, ou nada é enviado."""
    announce_send(sent, required=True)


def _terms(market: str, direction: Direction) -> PerpTerms:
    # a alavancagem não entra no endereço da posição
    return PerpTerms(market, direction, ONE)


def _sent(terms: PerpTerms, spend: int, send: PerpSend, signed) -> SentTx:
    return SentTx(
        signed.signature,
        SHORT_COLLATERAL,
        terms.market_mint,
        spend,
        0,
        signed.last_valid_block_height,
        perp=send,
    )


def _result(
    signature: str,
    collateral_mint: str,
    terms: PerpTerms,
    posted: int,
    fill: PerpFill,
    size: bool = True,
) -> ExecutionResult:
    """Uma perna que posta colateral: abrir (com o tamanho) ou pôr mais (sem)."""
    size_raw = _size_raw(terms, fill.size_usd, fill.price) if size else 0
    return ExecutionResult(
        signature,
        collateral_mint,
        terms.market_mint,
        posted,
        size_raw,
        costs=TradeCosts(ONCHAIN),
        perp=fill,
    )


def _order_result(signature: str, terms: PerpTerms, order: str) -> ExecutionResult:
    """Uma ordem no venue (stop, cancelamento): nada trocado, o endereço dela."""
    return ExecutionResult(
        signature,
        SHORT_COLLATERAL,
        terms.market_mint,
        0,
        0,
        costs=TradeCosts(ONCHAIN),
        venue_order=order,
    )


def position_of(perp: PerpSend) -> Pubkey:
    return Pubkey.from_string(str(perp.position))


def _done(perp: PerpSend) -> Done:
    """A conferência do que o envio pediu, contra o estado de antes dele
    (`PerpSend.before`): a mesma da espera do keeper."""
    before = None if perp.before is None else Decimal(perp.before)
    if perp.kind == PerpSendKind.CLOSE:
        return _smaller_than(before)
    if perp.kind == PerpSendKind.ADD and before is not None:
        return _more_collateral(before)
    return _is_open


def _is_open(position: VenuePosition | None) -> bool:
    return position is not None


def _smaller_than(size_usd: Decimal | None) -> Done:
    """Um fechamento executou: a posição sumiu ou ficou menor que `size_usd`."""
    return lambda now: now is None or (size_usd is not None and now.size_usd < size_usd)


def _more_collateral(collateral_usd: Decimal) -> Done:
    return lambda now: now is not None and now.collateral_usd > collateral_usd


def _scaled(position: VenuePosition, fraction: Decimal) -> VenuePosition:
    return replace(
        position,
        size_usd=position.size_usd * fraction,
        collateral_usd=position.collateral_usd * fraction,
    )


def _size_raw(terms: PerpTerms, size_usd: Decimal, price: Decimal) -> int:
    return SOLANA_MINTS[terms.market_mint].ui_to_raw(size_usd / price)


def _held_fill(terms: PerpTerms, position: VenuePosition) -> PerpFill:
    """A posição como a conta dela mostra, com a liquidação de agora."""
    fill = PerpFill(
        direction=terms.direction,
        leverage=terms.leverage,
        price=position.price,
        size_usd=position.size_usd,
        collateral_usd=position.collateral_usd,
        fees_usd=Decimal(0),
        borrow_bps_hour=Decimal(0),
    )
    return replace(fill, liquidation_price=liquidation_price(fill))


def _entry_fill(
    terms: PerpTerms, position: VenuePosition, collateral: Decimal, borrow: Decimal
) -> PerpFill:
    """A entrada: o que a conta da posição mostra depois do keeper."""
    return replace(
        _held_fill(terms, position),
        fees_usd=collateral - position.collateral_usd,
        borrow_bps_hour=borrow,
    )


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
