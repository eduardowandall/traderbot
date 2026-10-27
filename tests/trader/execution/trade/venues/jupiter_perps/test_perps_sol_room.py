"""A26 no venue real: o SOL de uma abertura, com o rent da conta da posição
só quando a conta ainda não existe (a PDA lida pelo leitor)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from trader.execution.market.perps.feed import PerpsFeed
from trader.execution.models.perp import PerpTerms
from trader.execution.trade.venues.jupiter_perps.venue import JupiterPerpsVenue
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT

SHORT = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
LOCKS = 2 * 3_723_640
SENDS = 3 * (5000 + 100_000)


class Reader:
    def __init__(self, account: bytes | None):
        self.account = account
        self.read: list = []

    async def accounts(self, addresses):
        self.read.extend(addresses)
        return [self.account for _ in addresses]

    async def aclose(self):
        return None


@pytest.mark.parametrize(
    ("account", "rent"), [(None, 1_747_520), (b"the position account", 0)]
)
async def test_the_rent_counts_only_for_a_new_position_account(account, rent):
    reader = Reader(account)
    executor = SimpleNamespace(
        pubkey=Keypair().pubkey(), max_priority_fee_lamports=100_000
    )
    venue = JupiterPerpsVenue(
        executor,  # type: ignore[arg-type]
        PerpsFeed(reader),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert await venue.open_sol_lamports(SHORT) == LOCKS + rent + SENDS
    assert reader.read == [venue._positions[(SOL_MINT, Direction.SHORT)]]
