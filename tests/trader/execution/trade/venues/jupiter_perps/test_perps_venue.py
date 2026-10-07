"""`JupiterPerpsVenue` (A11b) com um leitor e um executor de mentira.

Nada sai para a rede: o leitor devolve as contas que cada teste roteiriza
(o pedido e a posição a cada consulta do keeper), e o executor só anota o
que mandaria. Que os pedidos são aceitos pelo programa é do
`tests/live/test_live_perps.py` (simulação na mainnet).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from factories import USDC
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.execution.market.perps.encode import _write
from trader.execution.market.perps.idl import _idl, discriminator
from trader.execution.market.perps.reader import (
    CustodyState,
    JumpRate,
    OraclePrice,
    VenuePosition,
)
from trader.execution.models.errors import SwapRejectedError, TransactionSubmittedError
from trader.execution.models.intent import send_hook
from trader.execution.models.perp import PerpTerms
from trader.execution.trade.venues.jupiter_perps.venue import (
    PERPS,
    JupiterPerpsVenue,
    stop_level,
)
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
from trader.shared.models.perp import PerpFill

SHORT = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), stop_pct=Decimal(5))
LONG = PerpTerms(SOL_MINT, Direction.LONG, Decimal(2), stop_pct=Decimal(3))
REQUEST = b"request account"  # qualquer conteúdo: o venue só vê se existe


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


class Reader:
    """O leitor de mentira: `polls` são as respostas (pedido, posição)."""

    def __init__(self, polls=(), position=None, balances=(0, 0), payout=0):
        self.polls = list(polls)
        self.position_value = position
        self.balances = list(balances)
        self.payout = payout
        self.stop_account: bytes | None = None
        self.closed = False

    async def oracle_price(self, mint):
        return OraclePrice(Decimal(100), datetime.now(UTC))

    async def borrow_bps_hour(self, mint):
        return Decimal("0.17")

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
        if len(addresses) == 2 and self.polls:  # o keeper: (pedido, posição)
            return list(self.polls.pop(0))
        return [None for _ in addresses]  # as posições da varredura

    async def simulate(self, tx):
        return 90_000, []

    async def priority_fee(self, accounts):
        return 50_000

    async def last_payout(self, address, owner, mint):
        return self.payout

    async def aclose(self):
        self.closed = True


@dataclass
class Executor:
    pubkey: Pubkey
    sends: list

    async def send_instructions(self, instructions, sent, announce, mint, spend, extra):
        signed = SimpleNamespace(
            signature=f"sig-{len(self.sends)}", last_valid_block_height=1
        )
        announce(sent(signed))
        self.sends.append((instructions, mint, spend, extra))
        return signed


def _venue(reader: Reader) -> tuple[JupiterPerpsVenue, Executor]:
    executor = Executor(Keypair().pubkey(), [])
    quotes = SimpleNamespace(
        get_quote=_quote(SimpleNamespace(otherAmountThreshold="95000000"))
    )
    provider = SimpleNamespace(jupiter_client=quotes)

    async def no_wait(seconds):
        return None

    venue = JupiterPerpsVenue(executor, reader, provider, 20_000, sleep=no_wait)  # type: ignore[arg-type]
    return venue, executor


def _quote(value):
    async def get_quote(*args, **kwargs):
        return value

    return get_quote


@pytest.fixture
def sends():
    logged: list = []
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
    assert fill.fees_usd == Decimal("0.018") and fill.borrow_bps_hour == Decimal("0.17")
    assert fill.liquidation_price and fill.liquidation_price > 130
    [(instructions, mint, spend, extra)] = executor.sends
    assert (mint, spend, extra) == (USDC, 10_000_000, PERPS)  # só o colateral sai
    assert len(sends) == 1  # o envio no ledger antes dele (A3)


async def test_the_unit_price_stays_between_the_floor_and_the_cap(sends):
    reader = Reader(polls=[(None, position_bytes(30_000_000))])
    venue, executor = _venue(reader)
    await venue.open_perp(USDC, SHORT, Decimal(10), "k")
    [(instructions, *_)] = executor.sends
    limit = int.from_bytes(bytes(instructions[0].data)[1:5], "little")
    price = int.from_bytes(bytes(instructions[1].data)[1:9], "little")
    assert limit == 117_000  # 90 mil simuladas x 1.3
    assert price * limit <= 20_000 * 10**6  # nunca passa do teto


async def test_a_long_asks_the_keeper_for_the_swap_minimum(sends):
    reader = Reader(polls=[(None, position_bytes(20_000_000))])
    venue, executor = _venue(reader)
    await venue.open_perp(USDC, LONG, Decimal(10), "k")
    assert executor.sends  # o pedido saiu com o mínimo da quote


async def test_a_request_the_keeper_drops_is_rejected(sends):
    venue, _ = _venue(Reader(polls=[(REQUEST, None), (None, None)]))
    with pytest.raises(SwapRejectedError, match="recusou"):
        await venue.open_perp(USDC, SHORT, Decimal(10), "k")


async def test_a_keeper_that_never_answers_leaves_the_intent_unconfirmed(sends):
    venue, _ = _venue(Reader(polls=[(REQUEST, None)] * 40))
    with pytest.raises(TransactionSubmittedError, match="sem execução do keeper") as ex:
        await venue.open_perp(USDC, SHORT, Decimal(10), "k")
    assert ex.value.signature == "sig-0"


async def test_a_close_fills_with_the_usdc_that_came_back(sends):
    position = VenuePosition(
        Pubkey.new_unique(),
        Direction.SHORT,
        Decimal(100),
        Decimal(30),
        Decimal("9.982"),
        datetime.now(UTC),
    )
    reader = Reader(
        polls=[(REQUEST, position_bytes(30_000_000)), (None, None)],
        position=position,
        balances=[1_000_000, 10_500_000],
    )
    venue, executor = _venue(reader)

    result = await venue.close_perp(USDC, SHORT, "c")

    assert result.out_amount == 9_500_000 and result.perp is not None
    assert result.perp.collateral_usd == Decimal("9.5")
    assert executor.sends[0][2] == 0  # fechar não gasta USDC


async def test_closing_nothing_is_rejected():
    venue, _ = _venue(Reader())
    with pytest.raises(SwapRejectedError, match="nenhuma posição"):
        await venue.close_perp(USDC, SHORT, "c")


async def test_the_venue_stop_is_placed_and_checked_after_the_close():
    reader = Reader()
    venue, executor = _venue(reader)
    fill = PerpFill(
        Direction.SHORT,
        Decimal(3),
        Decimal(100),
        Decimal(30),
        Decimal("9.98"),
        Decimal(0),
        Decimal(0),
        liquidation_price=Decimal(133),
    )
    announced = []
    request = await venue.place_stop(SHORT, fill, "k:stop", announced.append)
    assert request is not None and announced and executor.sends[0][2] == 0
    reader.stop_account = b"still there"
    assert await venue.stop_left(SHORT) == request
    assert await venue.stop_left(SHORT) is None  # conferido uma vez


@pytest.mark.parametrize(("payout", "liquidated"), [(0, True), (9_000_000, False)])
async def test_a_position_gone_from_the_venue_is_an_exit(payout, liquidated):
    venue, _ = _venue(Reader(payout=payout))
    [exit_] = await venue.liquidations([SHORT])
    assert exit_.terms == SHORT
    assert exit_.result.out_amount == payout
    assert exit_.result.perp is not None and exit_.result.perp.liquidated is liquidated


@pytest.mark.parametrize(
    ("terms", "level"),
    [
        (SHORT, Decimal(105)),  # o stop da spec (5%) vem antes da liquidação
        (
            PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3), Decimal(20)),
            Decimal("116.5"),
        ),
        (LONG, Decimal(97)),
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
