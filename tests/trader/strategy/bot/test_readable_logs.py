"""A32: logs que se leem.

Uma linha por barra no `connect` (preço e posição), a linha do bucket só
quando muda, os custos sem as partes em 0, um fill do `serve` numa linha e o
log do RPC sem quebras.
"""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from factories import make_order
from solders.pubkey import Pubkey

from trader.execution.market.jupiter.logging_utils import logger_wrapper
from trader.execution.trade.accounts.base import fill_line
from trader.shared.models.costs import ONCHAIN, TradeCosts, describe_costs
from trader.shared.models.direction import Direction
from trader.shared.models.perp import PerpFill
from trader.shared.models.position import Position
from trader.shared.trading_service.protocol import BucketSnapshot
from trader.strategy.bot.loop import TradingBot, log_ticker, short_signature

PERP = PerpFill(
    direction=Direction.LONG,
    leverage=Decimal(2),
    price=Decimal(100),
    size_usd=Decimal(20),
    collateral_usd=Decimal("9.982"),
    fees_usd=Decimal("0.018"),
    borrow_bps_hour=Decimal("0.15"),
    liquidation_price=Decimal("50.2"),
)
FEE = TradeCosts(ONCHAIN, fee_lamports=10_000, priority_fee_lamports=5_000)


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "bot"]


def test_one_bar_line_without_a_position(caplog):
    with caplog.at_level(logging.DEBUG, logger="bot"):
        log_ticker("SOL-USDC", Decimal("104.5645"))
    assert _messages(caplog) == ["[blue]SOL-USDC[/blue] 104.56450 | sem posição"]


def test_one_bar_line_carries_the_open_perp(caplog):
    # aberta agora: o empréstimo ainda é 0
    now = datetime.now(UTC)
    order = make_order(quantity="0.2", price="100", timestamp=now)
    entry = replace(order, perp=PERP)
    with caplog.at_level(logging.DEBUG, logger="bot"):
        log_ticker("SOL-USDC", Decimal(101), Position(entry), Decimal(1))
    [line] = _messages(caplog)
    assert line.startswith(
        "[blue]SOL-USDC[/blue] 101.00000 | LONG 2x 0.20000000 @ 100.00000 | preço"
    )
    assert "colateral" in line and line.endswith("| liq 50.2000, empréstimo $0.0000")


def test_the_bucket_line_is_logged_only_when_it_changes(caplog):
    # a cada barra o preço sai; o PnL realizado só no início e depois de um fill
    bot = SimpleNamespace(
        symbol="SOL-USDC",
        wall_clock=iter(range(0, 1000, 15)).__next__,
        _bar_seconds=15,
        _logged_bar=None,
        _logged_bucket=None,
    )
    flat = BucketSnapshot(
        "b", Decimal(0), None, Decimal(0), Decimal(1), pnl_summary="PNL a"
    )
    with caplog.at_level(logging.DEBUG, logger="bot"):
        for summary in ("PNL a", "PNL a", "PNL b", "PNL b"):
            snapshot = replace(flat, pnl_summary=summary)
            TradingBot._log_bar(bot, Decimal(100), snapshot)  # type: ignore[arg-type]
    buckets = [m for m in _messages(caplog) if m.startswith("bucket:")]
    assert buckets == ["bucket: PNL a", "bucket: PNL b"]
    assert sum("sem posição" in m for m in _messages(caplog)) == 4


def test_costs_leave_the_zero_parts_out():
    assert describe_costs(TradeCosts(ONCHAIN, fee_lamports=5_000)) == (
        "custos [onchain]: taxa 0.000005000 SOL"
    )
    assert describe_costs(FEE) == (
        "custos [onchain]: taxa 0.000010000 SOL (priority 0.000005000)"
    )


def test_a_fill_is_one_readable_line():
    signature = "4E6raLWnGbQghCSZeohUf2Xzz53fQpFFybpJFj6r4aEjHNBmjLUF"
    order = replace(
        make_order(quantity="0.2", price="100"),
        order_id=signature,
        costs=FEE,
        perp=PERP,
    )
    line = fill_line(order)
    assert line.startswith("BUY 0.20000000 SOL @ $100.0000; perp long 2x")
    assert "Decimal(" not in line and "\n" not in line
    assert line.endswith(f"; tx {signature}")
    assert short_signature(signature) == "4E6raLWn...BmjLUF"


async def test_the_rpc_log_is_one_line(caplog):
    @logger_wrapper
    async def get_lamports(owner):
        return {"a": Pubkey.default(), "b": Pubkey.default()}

    with caplog.at_level(logging.DEBUG, logger=__name__):
        await get_lamports(Pubkey.default())
    [line] = [r.getMessage() for r in caplog.records]
    assert "\n" not in line and "Pubkey(" in line
