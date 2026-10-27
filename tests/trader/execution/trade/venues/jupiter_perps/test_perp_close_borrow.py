"""A27: um fechamento separa o empréstimo das taxas.

A Jupiter tira o empréstimo do colateral no fechamento; o venue o lê da
posição antes do envio (`position.borrow_usd(custody)`, como a abertura) e
o põe em `borrow_usd`; `fees_usd` é o que sobra de `esperado - devolvido`.
O que voltou (e o PnL, devolvido - postado) não muda.
"""

from dataclasses import replace
from decimal import Decimal

import pytest
from factories import USDC
from test_perps_venue import REQUEST, SHORT, Reader, _position, _venue, position_bytes

from trader.execution.models.intent import send_hook

# todo fechamento anota o envio antes (A3)
pytestmark = pytest.mark.usefixtures("logged_sends")

# 0.01 USD por USD de tamanho desde a abertura (x 1e9): 0.3 numa posição de 30
INTEREST = 10_000_000


@pytest.fixture
def logged_sends():
    token = send_hook.set(lambda sent: None)
    yield
    send_hook.reset(token)


class Owing(Reader):
    async def custody(self, mint):
        state = await super().custody(mint)
        return replace(state, cumulative_interest=INTEREST)


class NoCustody(Reader):
    async def custody(self, mint):
        raise OSError("429")


def _closing(reader_type, after: int = 9_500_000, size: str = "30") -> Reader:
    # vale 9.982 ao preço do oráculo (o da entrada); voltam 9.5
    return reader_type(
        polls=[(REQUEST, position_bytes(30_000_000)), (None, None)],
        position=_position(size),
        balances=[0, after],
    )


async def test_a_close_with_borrow_owed_splits_it_from_the_fees():
    result = await _venue(_closing(Owing))[0].close_perp(USDC, SHORT, "c")

    assert result.out_amount == 9_500_000 and result.perp is not None
    assert result.perp.collateral_usd == Decimal("9.5")
    assert result.perp.borrow_usd == Decimal("0.3")
    assert result.perp.fees_usd == Decimal("0.182")  # 9.982 - 9.5 - 0.3


async def test_without_borrow_the_whole_gap_is_fees():
    result = await _venue(_closing(Reader))[0].close_perp(USDC, SHORT, "c")

    assert result.perp is not None and result.perp.borrow_usd == 0
    assert result.perp.fees_usd == Decimal("0.482")


async def test_fees_never_go_below_zero():
    # voltou mais do que valia menos o empréstimo: tudo é empréstimo
    reader = _closing(Owing, after=9_900_000)
    result = await _venue(reader)[0].close_perp(USDC, SHORT, "c")

    assert result.perp is not None and result.perp.fees_usd == 0
    assert result.perp.borrow_usd == Decimal("0.3")


async def test_a_partial_close_takes_its_share_of_the_borrow():
    reader = Owing(
        polls=[
            (REQUEST, position_bytes(30_000_000)),
            (None, position_bytes(15_000_000)),
        ],
        position=_position(),
        balances=[0, 4_800_000],
    )
    result = await _venue(reader)[0].close_perp(USDC, SHORT, "c", Decimal("0.5"))

    assert result.perp is not None and result.perp.borrow_usd == Decimal("0.15")
    assert result.perp.fees_usd == Decimal("0.041")  # 4.991 - 4.8 - 0.15


async def test_an_unread_custody_never_blocks_the_close():
    result = await _venue(_closing(NoCustody))[0].close_perp(USDC, SHORT, "c")

    # o empréstimo fica nas taxas, como antes da A27
    assert result.perp is not None and result.perp.borrow_usd == 0
    assert result.perp.fees_usd == Decimal("0.482")
