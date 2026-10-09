"""`PerpsFeed` (A12): a custody e o oráculo da Jupiter Perps em cache.

Um por `serve`: as ordens e a varredura leem dele, e uma abertura recusa um
oráculo parado antes de enviar.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.execution.market.perps.feed import (
    MAX_ORACLE_AGE_SECONDS,
    PerpsFeed,
    StaleOracleError,
)
from trader.execution.market.perps.reader import OraclePrice
from trader.execution.models.errors import SwapRejectedError
from trader.shared.models.mints import SOL_MINT

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


class Reader:
    def __init__(self, age=timedelta(0)):
        self.age = age
        self.reads = {"custody": 0, "oracle": 0}

    async def custody(self, mint):
        self.reads["custody"] += 1
        return type("Custody", (), {"borrow_bps_hour": Decimal("0.2")})()

    async def oracle_price(self, mint):
        self.reads["oracle"] += 1
        return OraclePrice(Decimal(100), NOW - self.age)

    async def aclose(self):
        return None


def _feed(reader: Reader, clock: list[float]) -> PerpsFeed:
    return PerpsFeed(reader, monotonic=lambda: clock[0], now=lambda: NOW)  # type: ignore[arg-type]


async def test_the_custody_and_the_oracle_are_read_once_per_ttl():
    reader, clock = Reader(), [0.0]
    feed = _feed(reader, clock)

    for _ in range(3):
        assert await feed.borrow_bps_hour(SOL_MINT) == Decimal("0.2")
        await feed.oracle_price(SOL_MINT)
    assert reader.reads == {"custody": 1, "oracle": 1}

    clock[0] += 2  # o oráculo vence em 1 s, a custody em 60
    await feed.oracle_price(SOL_MINT)
    await feed.custody(SOL_MINT)
    assert reader.reads == {"custody": 1, "oracle": 2}


async def test_a_stale_oracle_is_a_rejection_not_a_failure():
    stale = Reader(age=timedelta(seconds=MAX_ORACLE_AGE_SECONDS + 1))
    with pytest.raises(StaleOracleError, match="parado") as ex:
        await _feed(stale, [0.0]).fresh_price(SOL_MINT)
    # uma recusa (REJECTED): fora do circuit breaker
    assert isinstance(ex.value, SwapRejectedError)
    fresh = await _feed(Reader(), [0.0]).fresh_price(SOL_MINT)
    assert fresh.price == 100
