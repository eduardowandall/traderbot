"""Relatório diário (B5): somas do dia, texto, uma vez por dia, dia vazio."""

from datetime import UTC, datetime
from decimal import Decimal

from factories import SOL, Inbox, executed_leg, memory_gateway

from trader.execution.notification.daily_report import DailyReporter

NOW = datetime(2026, 10, 3, 0, 5, tzinfo=UTC)  # relata 2026-10-02


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


async def _sol_at_110(mint: str) -> Decimal | None:
    return Decimal("110") if mint == SOL else None


def _reporter(gateway, inbox=None) -> DailyReporter:
    return DailyReporter(
        gateway, "paper", inbox or Inbox(), _sol_at_110, clock=lambda: NOW
    )


def _ledger_with_three_buckets():
    gateway = memory_gateway()
    ledger = gateway.ledger
    # a: ida e volta ontem
    executed_leg(ledger, "paper:strategy:a", "buy", _at(2, 10))
    executed_leg(ledger, "paper:strategy:a", "sell", _at(2, 12), "1.5", price="115")
    # b: comprou anteontem, segue aberto
    executed_leg(ledger, "paper:strategy:b", "buy", _at(1, 9))
    # c: nada ontem, nada aberto
    executed_leg(ledger, "paper:strategy:c", "buy", _at(1, 9))
    executed_leg(ledger, "paper:strategy:c", "sell", _at(1, 11), "-1", price="90")
    return gateway


async def test_reports_yesterday_per_active_bucket():
    gateway = _ledger_with_three_buckets()
    inbox = Inbox()

    text = await _reporter(gateway, inbox).send_due()

    assert inbox.messages == [text]
    assert text is not None
    assert text.startswith("Relatório paper de 2026-10-02 (UTC)")
    assert "strategy:a\n  dia: 2 fill(s), PnL realizado ~$+1.5000" in text
    # b: 0.1 SOL a 100 (custo 10) vale 11 a 110
    assert "strategy:b\n  dia: 0 fill(s)" in text
    assert "aberto: 0.100000 SOL @ USD 100.000000; marcação ~$+1.0000" in text
    assert "strategy:c" not in text


async def test_a_day_is_reported_once_even_across_restarts():
    gateway = _ledger_with_three_buckets()
    inbox = Inbox()
    assert await _reporter(gateway, inbox).send_due() is not None

    assert await _reporter(gateway, inbox).send_due() is None
    assert len(inbox.messages) == 1


async def test_an_empty_day_sends_nothing_but_is_marked():
    gateway = memory_gateway()
    inbox = Inbox()

    assert await _reporter(gateway, inbox).send_due() is None
    assert inbox.messages == []
    assert gateway.ledger.daily_report_sent("2026-10-02")


async def test_a_missing_price_still_reports_the_position():
    gateway = memory_gateway()
    executed_leg(gateway.ledger, "paper:strategy:b", "buy", _at(2, 9))

    async def no_price(mint):
        raise RuntimeError("Price API fora")

    reporter = DailyReporter(gateway, "paper", Inbox(), no_price, clock=lambda: NOW)
    text = await reporter.send_due()
    assert text is not None and "marcação: sem preço" in text


async def test_a_bucket_with_round_trips_shows_their_cost():
    gateway = memory_gateway()
    # gastou 10 a 100, saiu a 115: sem custo +1.5; recebeu +1.4 -> custou 0.1
    executed_leg(gateway.ledger, "paper:strategy:a", "buy", _at(2, 10))
    executed_leg(
        gateway.ledger, "paper:strategy:a", "sell", _at(2, 12), "1.4", price="115"
    )
    # `record_executed` grava valores raw de enfeite (1 e 2): a venda precisa
    # vender o que a compra recebeu, senão conta como venda parcial
    with gateway.ledger.conn:
        gateway.ledger.conn.execute("UPDATE intents SET in_amount = 2")

    text = await _reporter(gateway).send_due()

    assert text is not None
    assert "  custo por ida e volta ~$0.1000 (100.0 bps) em 1\n  total:" in text
