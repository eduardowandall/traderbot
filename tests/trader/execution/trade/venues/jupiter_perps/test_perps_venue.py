"""`JupiterPerpsVenue` (A11b, A12) com um leitor e um executor de mentira.

Nada sai para a rede: o leitor devolve as contas que cada teste roteiriza
(o pedido e a posição a cada consulta do keeper), e o executor só anota o
que mandaria. Que os pedidos são aceitos pelo programa é do
`tests/live/test_live_perps.py` (simulação na mainnet).
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from factories import USDC
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.execution.market.perps.encode import _write, instruction_discriminator
from trader.execution.market.perps.feed import PerpsFeed, StaleOracleError
from trader.execution.market.perps.idl import _idl, _read, discriminator, instructions
from trader.execution.market.perps.reader import (
    CustodyState,
    JumpRate,
    OraclePrice,
    VenuePosition,
)
from trader.execution.models.errors import SwapRejectedError, TransactionSubmittedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import PerpSendKind, SentTx, send_hook
from trader.execution.models.perp import PerpTerms, stop_level
from trader.execution.trade.venues.jupiter.compute_budget import ComputeBudget
from trader.execution.trade.venues.jupiter_perps.requests import CANCEL, CLOSE, OPEN
from trader.execution.trade.venues.jupiter_perps.venue import PERPS, JupiterPerpsVenue
from trader.shared.models.costs import BASE_FEE_LAMPORTS
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.perp import PerpFill

SHORT = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), stop_pct=Decimal(5))
LONG = PerpTerms(SOL_MINT, Direction.LONG, Decimal(2), stop_pct=Decimal(3))
REQUEST = b"request account"  # qualquer conteúdo: o venue só vê se existe
RENT = 1_747_520  # o rent da conta de uma posição (o run 1 real, A11b)


def position_bytes(
    size: int, price: int = 100_000_000, collateral: int = 9_982_000
) -> bytes:
    fields = {
        "owner": Pubkey.default(),
        "pool": Pubkey.default(),
        "custody": Pubkey.default(),
        "collateralCustody": Pubkey.default(),
        "openTime": 0,
        "updateTime": 0,
        "side": "Short",
        "price": price,
        "sizeUsd": size,
        "collateralUsd": collateral,
        "realisedPnlUsd": 0,
        "cumulativeInterestSnapshot": 0,
        "lockedAmount": 0,
        "bump": 255,
    }
    return discriminator("Position") + _write(_idl()[0]["Position"], fields)


def _position(size="30", collateral="9.982") -> VenuePosition:
    return VenuePosition(
        Pubkey.new_unique(),
        Direction.SHORT,
        Decimal(100),
        Decimal(size),
        Decimal(collateral),
        datetime.now(UTC),
    )


class Reader:
    """O leitor de mentira: `polls` são as respostas (pedido, posição)."""

    def __init__(
        self,
        polls=(),
        position=None,
        balances=(0, 0),
        payout=0,
        costs=(10_000, 0),
        oracle_age=timedelta(0),
    ):
        self.polls = list(polls)
        self.position_value = position
        self.balances = list(balances)
        self.payout = payout
        self.costs = costs  # (taxa, rent) da transação de um pedido
        self.oracle_age = oracle_age
        self.stop_account: bytes | None = None
        self.open_accounts: list[bytes | None] | None = None
        self.closed = False

    async def oracle_price(self, mint):
        return OraclePrice(Decimal(100), datetime.now(UTC) - self.oracle_age)

    async def custody(self, mint):
        return CustodyState(
            mint=mint,
            decimals=9,
            owned=1,
            locked=0,
            open_fee_bps=Decimal(6),
            close_fee_bps=Decimal(6),
            jump_rate=JumpRate(Decimal(1), Decimal(2), Decimal(1), Decimal("0.8")),
            oracle=Pubkey.new_unique(),
            pyth_oracle=Pubkey.new_unique(),
        )

    async def position(self, owner, terms):
        return self.position_value

    async def token_amount(self, account):
        return self.balances.pop(0)

    async def accounts(self, addresses):
        if len(addresses) == 1:  # a ordem de stop
            return [self.stop_account]
        if self.open_accounts is not None:  # as posições da carteira
            return self.open_accounts
        if len(addresses) == 2 and self.polls:  # o keeper: (pedido, posição)
            return list(self.polls.pop(0))
        return [None for _ in addresses]  # as posições da varredura

    async def priority_fee(self, accounts):
        return 50_000

    async def transaction_costs(self, signature, watched):
        return self.costs

    async def last_payout(self, address, owner, mint):
        return self.payout

    async def aclose(self):
        self.closed = True


@dataclass
class Executor:
    pubkey: Pubkey
    sends: list

    async def send_instructions(
        self, instructions, sent, announce, mint, spend, extra, budget
    ):
        signed = SimpleNamespace(
            signature=f"sig-{len(self.sends)}", last_valid_block_height=1
        )
        announce(sent(signed))
        self.sends.append((instructions, mint, spend, extra, budget))
        return signed


def _venue(reader: Reader) -> tuple[JupiterPerpsVenue, Executor]:
    executor = Executor(Keypair().pubkey(), [])
    quotes = SimpleNamespace(
        get_quote=_quote(SimpleNamespace(otherAmountThreshold="95000000"))
    )
    provider = SimpleNamespace(jupiter_client=quotes)

    async def no_wait(seconds):
        return None

    venue = JupiterPerpsVenue(
        executor,  # type: ignore[arg-type]
        PerpsFeed(reader),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        sleep=no_wait,
    )
    return venue, executor


def _quote(value):
    async def get_quote(*args, **kwargs):
        return value

    return get_quote


def _args(instruction, name: str) -> dict:
    """Os argumentos da instrução lidos de volta pela IDL."""
    spec = instructions()[name]["args"][0]["type"]
    return _read(spec, bytes(instruction.data), 8)[0]


@pytest.fixture
def sends():
    logged: list[SentTx] = []
    token = send_hook.set(logged.append)
    yield logged
    send_hook.reset(token)


async def test_an_open_waits_for_the_keeper_and_fills_from_the_position(sends):
    reader = Reader(polls=[(REQUEST, None), (None, position_bytes(30_000_000))])
    venue, executor = _venue(reader)

    result = await venue.open_perp(USDC, SHORT, Decimal(10), "k")

    fill = result.perp
    assert fill is not None
    assert (fill.price, fill.size_usd, fill.collateral_usd) == (
        100,
        30,
        Decimal("9.982"),
    )
    assert fill.fees_usd == Decimal("0.018")
    assert fill.liquidation_price and fill.liquidation_price > 130
    [(instructions, mint, spend, extra, budget)] = executor.sends
    assert (mint, spend, extra) == (USDC, 10_000_000, PERPS)  # só o colateral sai
    # o limite de unidades é do executor (A12): aqui só o pedido e o preço recente
    assert len(instructions) == 1 and budget == ComputeBudget(50_000)
    # o envio no ledger antes dele (A3), com o que ele pediu ao venue (A12)
    [sent] = sends
    assert sent.perp is not None and sent.perp.kind == PerpSendKind.OPEN
    assert (sent.input_mint, sent.output_mint) == (USDC, SOL_MINT)


async def test_a_stale_oracle_refuses_the_open_before_sending(sends):
    reader = Reader(oracle_age=timedelta(minutes=2))
    venue, executor = _venue(reader)
    with pytest.raises(StaleOracleError, match="parado"):
        await venue.open_perp(USDC, SHORT, Decimal(10), "k")
    assert executor.sends == [] and sends == []


async def test_a_long_asks_the_keeper_for_the_swap_minimum(sends):
    reader = Reader(polls=[(None, position_bytes(20_000_000))])
    venue, executor = _venue(reader)
    await venue.open_perp(USDC, LONG, Decimal(10), "k")
    [(instructions, *_)] = executor.sends
    assert _args(instructions[0], OPEN)["jupiterMinimumOut"] == 95_000_000


async def test_a_request_the_keeper_drops_is_rejected_with_its_signature(sends):
    venue, _ = _venue(Reader(polls=[(REQUEST, None), (None, None)]))
    with pytest.raises(SwapRejectedError, match="recusou") as ex:
        await venue.open_perp(USDC, SHORT, Decimal(10), "k")
    # o pedido entrou e pagou taxa e rent: o bucket os registra (A12)
    assert ex.value.failed_signatures == ("sig-0",)


async def test_a_keeper_that_never_answers_leaves_the_intent_unconfirmed(sends):
    venue, _ = _venue(Reader(polls=[(REQUEST, None)] * 40))
    with pytest.raises(TransactionSubmittedError, match="sem execução do keeper") as ex:
        await venue.open_perp(USDC, SHORT, Decimal(10), "k")
    assert ex.value.signature == "sig-0"


async def test_a_close_fills_with_the_usdc_that_came_back(sends):
    reader = Reader(
        polls=[(REQUEST, position_bytes(30_000_000)), (None, None)],
        position=_position(),
        balances=[1_000_000, 10_500_000],
    )
    venue, executor = _venue(reader)

    result = await venue.close_perp(USDC, SHORT, "c")

    assert result.out_amount == 9_500_000 and result.perp is not None
    assert result.perp.collateral_usd == Decimal("9.5")
    [(instructions, _, spend, *_)] = executor.sends
    assert spend == 0  # fechar não gasta USDC
    assert _args(instructions[0], CLOSE)["entirePosition"] is True


async def test_a_partial_close_takes_size_and_collateral_in_proportion(sends):
    # A12: metade da posição; o keeper a deixa com metade do tamanho
    reader = Reader(
        polls=[
            (REQUEST, position_bytes(30_000_000)),
            (None, position_bytes(15_000_000)),
        ],
        position=_position(),
        balances=[1_000_000, 5_900_000],
    )
    venue, executor = _venue(reader)

    result = await venue.close_perp(USDC, SHORT, "c", Decimal("0.5"))

    args = _args(executor.sends[0][0][0], CLOSE)
    assert args["entirePosition"] is False
    assert (args["sizeUsdDelta"], args["collateralUsdDelta"]) == (15_000_000, 4_991_000)
    assert result.out_amount == 4_900_000
    assert result.perp is not None and result.perp.size_usd == 15
    assert sends[0].perp is not None and sends[0].perp.before == "30"


async def test_closing_nothing_is_rejected():
    venue, _ = _venue(Reader())
    with pytest.raises(SwapRejectedError, match="nenhuma posição"):
        await venue.close_perp(USDC, SHORT, "c")


async def test_collateral_is_added_with_a_size_zero_request(sends):
    # A12: o pedido de aumento sem tamanho; o keeper põe o colateral
    grown = position_bytes(30_000_000, collateral=11_982_000)
    reader = Reader(polls=[(REQUEST, None), (None, grown)], position=_position())
    venue, executor = _venue(reader)

    result = await venue.add_collateral(USDC, SHORT, Decimal(2), "a")

    [(instructions, _, spend, *_)] = executor.sends
    args = _args(instructions[0], OPEN)
    assert (args["sizeUsdDelta"], args["collateralTokenDelta"]) == (0, 2_000_000)
    assert spend == 2_000_000
    assert (result.in_amount, result.out_amount) == (2_000_000, 0)
    fill = result.perp
    assert fill is not None and fill.collateral_usd == Decimal("11.982")
    # mais colateral, liquidação mais longe (um vendido: mais acima)
    assert fill.liquidation_price and fill.liquidation_price > 138
    assert sends[0].perp is not None and sends[0].perp.kind == PerpSendKind.ADD


def _entry_fill() -> PerpFill:
    return PerpFill(
        Direction.SHORT,
        Decimal(3),
        Decimal(100),
        Decimal(30),
        Decimal("9.98"),
        Decimal(0),
        Decimal(0),
        liquidation_price=Decimal(133),
    )


async def test_the_venue_stop_is_placed_and_a_leftover_is_cancelled(sends):
    reader = Reader()
    venue, executor = _venue(reader)

    placed = await venue.place_stop(SHORT, _entry_fill(), "k:stop")

    assert placed.venue_order is not None and executor.sends[0][2] == 0
    assert sends[0].perp is not None and sends[0].perp.kind == PerpSendKind.STOP
    reader.stop_account = b"still there"
    assert await venue.stop_left(SHORT, placed.venue_order) is True
    cancelled = await venue.cancel_stop(SHORT, placed.venue_order, "k:cancel")
    assert cancelled.venue_order == placed.venue_order
    [cancel] = executor.sends[1][0]
    # sem argumentos: só o discriminador
    assert bytes(cancel.data) == instruction_discriminator(CANCEL)
    request = Pubkey.from_string(placed.venue_order)
    assert any(m.pubkey == request and m.is_writable for m in cancel.accounts)
    # o keeper é opcional: o programa no lugar, sem assinar
    assert not cancel.accounts[0].is_signer


async def test_a_stop_the_keeper_clears_after_the_close_is_not_left():
    # A11c F3: no run 1 o keeper apagou a ordem 1 s depois de conferida
    class Clearing(Reader):
        async def accounts(self, addresses):
            seen, self.stop_account = self.stop_account, None
            return [seen]

    reader = Clearing()
    venue, _ = _venue(reader)
    reader.stop_account = b"still there for a moment"
    assert await venue.stop_left(SHORT, str(Pubkey.new_unique())) is False


async def test_costs_come_from_the_request_transaction(sends):
    # A11c F1/F4, A12: a taxa (base + prioridade) e o rent da conta da posição
    # que o pedido criou, do `meta` da transação dele
    venue, _ = _venue(Reader(polls=[(None, position_bytes(30_000_000))]))
    venue.reader.costs = (10_000, RENT)  # type: ignore[attr-defined]
    result = await venue.open_perp(USDC, SHORT, Decimal(10), "k")

    costs = await venue.fetch_costs(result)

    assert (costs.fee_lamports, costs.priority_fee_lamports) == (10_000, 5_000)
    assert costs.rent_lamports == RENT


async def test_a_rejected_request_pays_its_fee_and_the_rent_it_created():
    venue, _ = _venue(Reader(costs=(10_000, RENT)))
    assert await venue.fetch_failed_fees(["sig"]) == 10_000 + RENT

    class Unreadable(Reader):
        async def transaction_costs(self, signature, watched):
            raise OSError("RPC fora")

    venue, _ = _venue(Unreadable())
    assert await venue.fetch_failed_fees(["a", "b"]) == 2 * BASE_FEE_LAMPORTS


@pytest.mark.parametrize(("payout", "liquidated"), [(0, True), (9_000_000, False)])
async def test_a_position_gone_from_the_venue_is_an_exit(payout, liquidated):
    venue, _ = _venue(Reader(payout=payout))
    [exit_] = (await venue.sweep([SHORT])).exits
    assert exit_.terms == SHORT
    assert exit_.result.out_amount == payout
    assert exit_.result.perp is not None and exit_.result.perp.liquidated is liquidated


async def test_the_open_markets_are_read_at_once():
    reader = Reader()
    venue, _ = _venue(reader)
    # (SOL comprado, SOL vendido): só o vendido aberto
    reader.open_accounts = [None, position_bytes(30_000_000)]
    assert await venue.open_markets() == [(SOL_MINT, Direction.SHORT)]


def _sent(kind: PerpSendKind) -> SentTx:
    from trader.execution.models.intent import PerpSend

    send = PerpSend(kind, str(Pubkey.new_unique()), str(Pubkey.new_unique()))
    return SentTx("sig", USDC, SOL_MINT, 10_000_000, 0, 1, perp=send)


async def test_a_landed_open_resolves_by_what_the_keeper_did():
    pending = Reader(polls=[(REQUEST, None)])
    assert (
        await _venue(pending)[0].resolve_send(_sent(PerpSendKind.OPEN), SHORT) is None
    )

    filled = Reader(polls=[(None, position_bytes(30_000_000))])
    result = await _venue(filled)[0].resolve_send(_sent(PerpSendKind.OPEN), SHORT)
    assert isinstance(result, ExecutionResult) and result.perp is not None
    assert (result.in_amount, result.perp.size_usd) == (10_000_000, 30)

    dropped = Reader(polls=[(None, None)])
    with pytest.raises(SwapRejectedError):
        await _venue(dropped)[0].resolve_send(_sent(PerpSendKind.OPEN), SHORT)


async def test_a_landed_close_resolves_with_the_keepers_payout():
    reader = Reader(polls=[(None, None)], payout=9_500_000)
    result = await _venue(reader)[0].resolve_send(_sent(PerpSendKind.CLOSE), SHORT)
    assert result is not None and result.out_amount == 9_500_000

    kept = Reader(polls=[(None, position_bytes(30_000_000))])
    with pytest.raises(SwapRejectedError):
        await _venue(kept)[0].resolve_send(_sent(PerpSendKind.CLOSE), SHORT)


async def test_a_landed_stop_is_placed_without_asking_the_keeper():
    sent = _sent(PerpSendKind.STOP)
    result = await _venue(Reader())[0].resolve_send(sent, SHORT)
    assert result is not None and sent.perp is not None
    assert result.venue_order == sent.perp.request


@pytest.mark.parametrize(
    ("terms", "level"),
    [
        (SHORT, Decimal(105)),  # o stop da spec (5%) vem antes da liquidação
        (
            PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), Decimal(20)),
            Decimal("116.5"),
        ),
        (LONG, Decimal(97)),
        # sem o stop da spec (termos de teste): metade até a liquidação
        (PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3)), Decimal("116.5")),
    ],
)
def test_the_venue_stop_is_the_closer_of_the_spec_and_half_to_liquidation(terms, level):
    liquidation = Decimal(133) if terms.direction == Direction.SHORT else Decimal(51)
    fill = PerpFill(
        terms.direction,
        terms.leverage,
        Decimal(100),
        Decimal(30),
        Decimal(10),
        Decimal(0),
        Decimal(0),
        liquidation_price=liquidation,
    )
    assert stop_level(terms, fill) == level


async def test_reads_that_fail_after_the_keeper_filled_never_raise(sends):
    # code review A11c: depois do fill, uma leitura que falha não pode deixar
    # a intenção FAILED com a posição aberta na Jupiter
    class Flaky(Reader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.custody_reads = 0

        async def custody(self, mint):
            raise OSError("429")

    venue, _ = _venue(Flaky(polls=[(None, position_bytes(30_000_000))]))
    opened = await venue.open_perp(USDC, SHORT, Decimal(10), "k")
    assert opened.perp is not None and opened.perp.borrow_bps_hour == 0

    class NoBalance(Reader):
        async def token_amount(self, account):
            if self.balances:
                return self.balances.pop(0)
            raise OSError("RPC fora")

    reader = NoBalance(
        polls=[(REQUEST, position_bytes(30_000_000)), (None, None)],
        position=_position(),
        balances=[1_000_000],
    )
    closed = await _venue(reader)[0].close_perp(USDC, SHORT, "c")
    # o valor ao preço do oráculo (100, o da entrada): o colateral inteiro
    assert closed.out_amount == 9_982_000


# --- revisão da A12 ---------------------------------------------------------------


def _sent_before(kind, before, in_amount=0) -> SentTx:
    from trader.execution.models.intent import PerpSend

    send = PerpSend(
        kind,
        str(Pubkey.new_unique()),
        str(Pubkey.new_unique()),
        before=before,
    )
    return SentTx("sig", USDC, SOL_MINT, in_amount, 0, 1, perp=send)


async def test_a_resolved_close_keeps_the_size_it_closed():
    # o fechamento grava o tamanho que sai; a resolução fecha essa quantidade
    reader = Reader(polls=[(None, None)], payout=9_500_000)
    sent = _sent_before(PerpSendKind.CLOSE, "30", in_amount=300_000_000)
    result = await _venue(reader)[0].resolve_send(sent, SHORT)
    assert result is not None and result.in_amount == 300_000_000


async def test_a_close_logs_the_size_it_takes(sends):
    reader = Reader(
        polls=[(REQUEST, position_bytes(30_000_000)), (None, None)],
        position=_position(),
        balances=[1_000_000, 10_500_000],
    )
    result = await _venue(reader)[0].close_perp(USDC, SHORT, "c")
    assert sends[0].in_amount == result.in_amount == 300_000_000  # 0.3 SOL


@pytest.mark.parametrize(
    ("size", "executed"), [(15_000_000, True), (30_000_000, False)]
)
async def test_a_resolved_partial_close_compares_with_the_size_before(size, executed):
    reader = Reader(polls=[(None, position_bytes(size))], payout=4_900_000)
    sent = _sent_before(PerpSendKind.CLOSE, "30")
    venue = _venue(reader)[0]
    if executed:
        assert await venue.resolve_send(sent, SHORT) is not None
    else:
        with pytest.raises(SwapRejectedError):
            await venue.resolve_send(sent, SHORT)


@pytest.mark.parametrize(
    ("collateral", "executed"), [(11_982_000, True), (9_982_000, False)]
)
async def test_a_resolved_top_up_compares_with_the_collateral_before(
    collateral, executed
):
    grown = position_bytes(30_000_000, collateral=collateral)
    sent = _sent_before(PerpSendKind.ADD, "9.982")
    venue = _venue(Reader(polls=[(None, grown)]))[0]
    if executed:
        result = await venue.resolve_send(sent, SHORT)
        assert result is not None and result.perp is not None
        assert result.perp.collateral_usd == Decimal("11.982")
    else:
        with pytest.raises(SwapRejectedError):
            await venue.resolve_send(sent, SHORT)


async def test_the_sweep_counts_the_borrow_a_held_position_owes():
    # a Jupiter tira o empréstimo do colateral no fechamento: a liquidação
    # de agora já o conta (o que o add_collateral olha)
    class Owing(Reader):
        async def custody(self, mint):
            state = await super().custody(mint)
            # 0.01 USD por USD de tamanho desde a abertura (x 1e9)
            return replace(state, cumulative_interest=10_000_000)

    owing_reader, clean_reader = Owing(), Reader()
    for reader in (owing_reader, clean_reader):
        reader.stop_account = position_bytes(30_000_000)  # a leitura de uma conta
    venue, fresh = _venue(owing_reader)[0], _venue(clean_reader)[0]

    owing = (await venue.sweep([SHORT])).held[SHORT]
    clean = (await fresh.sweep([SHORT])).held[SHORT]

    assert owing.collateral_usd == clean.collateral_usd - Decimal("0.3")
    assert owing.liquidation_price and clean.liquidation_price
    assert owing.liquidation_price < clean.liquidation_price  # vendido: mais perto


async def test_the_entry_records_the_collateral_custodys_borrow_rate(sends):
    # A18: o vendido toma USDC emprestado; a taxa da entrada é a dessa custody
    asked = []

    class PerCustody(Reader):
        async def custody(self, mint):
            asked.append(mint)
            rate = Decimal("0.6") if mint == USDC else Decimal("0.2")
            state = await super().custody(mint)
            # a taxa por hora sai da curva: anual = rate x 8760
            curve = JumpRate(rate * 8760, rate * 8760, rate * 8760, Decimal("0.8"))
            return replace(state, jump_rate=curve)

    reader = PerCustody(polls=[(None, position_bytes(30_000_000))])
    opened = await _venue(reader)[0].open_perp(USDC, SHORT, Decimal(10), "k")

    assert asked == [USDC]
    assert opened.perp is not None and opened.perp.borrow_bps_hour == Decimal("0.6")
