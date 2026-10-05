"""A3: intenções sem desfecho são resolvidas pelo que a rede (ou a carteira
simulada) diz de cada envio gravado."""

import logging
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import events_of, make_intent, memory_gateway, mock_provider

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.models.intent import (
    IntentStatus,
    PolicyDecision,
    SentTx,
    TxOutcome,
)
from trader.execution.trade.gateway.resolve import IntentResolver
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import OrderRequest

USDC = SOLANA_MINTS.get_by_symbol("USDC")
BONK = SOLANA_MINTS.get_by_symbol("BONK")
SOL = SOLANA_MINTS.get_by_symbol("SOL")


# --- em paper, de ponta a ponta ---------------------------------------------


def _paper_service():
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(datetime(2026, 9, 1, 12, 0), Decimal("1"))
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    gateway = memory_gateway()
    service = TradeService(paper_provider(wallet, jupiter_client=quotes), gateway)
    return service, gateway, wallet


def _dies_once(monkeypatch, gateway):
    """O próximo `mark_executed` falha: o swap aconteceu, o registro não."""
    real = gateway.ledger.mark_executed
    calls = []

    def mark(intent_id, result):
        calls.append(intent_id)
        if len(calls) == 1:
            raise RuntimeError("processo morto")
        return real(intent_id, result)

    monkeypatch.setattr(gateway.ledger, "mark_executed", mark)


def _request(side, quantity="10"):
    return OrderRequest(side, Decimal(quantity), Decimal("1"))


async def test_a_buy_that_landed_is_executed_and_the_bucket_holds_it(monkeypatch):
    service, gateway, wallet = _paper_service()
    await service.open_bucket("b", USDC.mint, BONK.mint)
    _dies_once(monkeypatch, gateway)

    reply = await service.submit_order("b", _request(OrderSide.BUY))
    assert not reply.filled  # a ordem "morreu" depois de aplicada
    [record] = gateway.ledger.active_intents()
    assert record.signature and record.signature.startswith("paper-")

    await service.resolve_intents()

    done = gateway.ledger.get(record.intent.intent_id)
    assert done and done.status == IntentStatus.EXECUTED and done.order_json
    position = (await service.get_bucket("b")).position
    assert position is not None
    assert position.entry_order.quantity == wallet.balance(BONK.mint)
    resolved = events_of(gateway.ledger, "intent_resolved")
    assert [(e["from"], e["outcome"]) for e in resolved] == [("executing", "executed")]


async def _round_trip(monkeypatch=None):
    """Compra e venda; com `monkeypatch`, a venda morre e é resolvida."""
    service, gateway, wallet = _paper_service()
    await service.open_bucket("b", USDC.mint, BONK.mint)
    assert (await service.submit_order("b", _request(OrderSide.BUY))).filled
    if monkeypatch is not None:
        _dies_once(monkeypatch, gateway)
    held = wallet.balance(BONK.mint)
    await service.submit_order("b", _request(OrderSide.SELL, str(held)))
    await service.resolve_intents()
    return service, gateway


async def test_a_sell_that_landed_books_the_same_pnl_as_a_normal_one(monkeypatch):
    _, normal = await _round_trip()
    service, resolved = await _round_trip(monkeypatch)

    assert (await service.get_bucket("b")).position is None
    sell, reference = resolved.ledger.list_intents()[0], normal.ledger.list_intents()[0]
    assert sell.status == IntentStatus.EXECUTED
    assert sell.realized_pnl_usd == reference.realized_pnl_usd
    assert resolved.ledger.pnl_totals("b") == normal.ledger.pnl_totals("b")


async def test_an_unapplied_paper_send_expires_and_the_intent_fails():
    service, gateway, _ = _paper_service()
    intent = _executing(gateway)
    gateway.ledger.record_send(intent.intent_id, _sent("paper-nunca-aplicado"))

    await service.resolve_intents()

    assert gateway.ledger.get(intent.intent_id).status == IntentStatus.FAILED  # type: ignore[union-attr]
    assert gateway.ledger.active_intents() == []


async def test_a_resolved_loss_past_the_max_loss_retires_the_bucket(monkeypatch):
    from trader.shared.trading_service.protocol import BucketStatus

    service, gateway, wallet = _paper_service()
    await service.open_bucket("b", USDC.mint, BONK.mint, max_loss_usd=Decimal(1))
    assert (await service.submit_order("b", _request(OrderSide.BUY))).filled
    # o preço cai pela metade: a venda perde ~5 USD, além do limite de 1
    quotes = service.provider.jupiter_client
    assert isinstance(quotes, ReplayQuoteClient)
    quotes.tick = Tick(datetime(2026, 9, 1, 12, 1), Decimal("0.5"))
    _dies_once(monkeypatch, gateway)
    held = wallet.balance(BONK.mint)
    await service.submit_order("b", _request(OrderSide.SELL, str(held)))
    assert (await service.get_bucket("b")).status == BucketStatus.ACTIVE

    await service.resolve_intents()

    assert (await service.get_bucket("b")).status == BucketStatus.RETIRING
    assert events_of(gateway.ledger, "bucket_max_loss")


# --- intenções sem envio ----------------------------------------------------


async def test_an_intent_never_sent_fails():
    gateway = memory_gateway()
    intent = _executing(gateway)
    resolver = IntentResolver(gateway, mock_provider())

    assert await resolver.run() == {intent.account}

    record = gateway.ledger.get(intent.intent_id)
    assert record and record.status == IntentStatus.FAILED
    assert "nunca enviada" in (record.error or "")


async def test_an_intent_from_an_older_build_keeps_blocking(caplog):
    gateway = memory_gateway()
    intent = _executing(gateway)
    _drop_send_log(gateway, intent.intent_id)
    gateway.ledger.mark_unconfirmed(intent.intent_id, "interrompida")
    resolver = IntentResolver(gateway, mock_provider())

    with caplog.at_level(logging.WARNING):
        assert await resolver.run() == set()
        assert await resolver.run() == set()

    assert gateway.ledger.get(intent.intent_id).status == IntentStatus.UNCONFIRMED  # type: ignore[union-attr]
    # o aviso sai uma vez por processo
    assert caplog.text.count("versão sem registro de envios") == 1


# --- desfechos de cada envio ------------------------------------------------


async def test_a_pending_send_keeps_blocking_until_it_lands():
    gateway = memory_gateway()
    intent = _executing(gateway)
    gateway.ledger.record_send(intent.intent_id, _sent("sig-1"))
    provider = mock_provider()
    provider.send_outcome = AsyncMock(return_value=TxOutcome.PENDING)
    resolver = IntentResolver(gateway, provider)

    assert await resolver.run() == set()
    assert gateway.ledger.active_intents()

    provider.send_outcome = AsyncMock(return_value=TxOutcome.LANDED)
    assert await resolver.run() == {intent.account}
    record = gateway.ledger.get(intent.intent_id)
    assert record and record.status == IntentStatus.EXECUTED
    assert record.signature == "sig-1" and record.order_json


async def test_failed_sends_pay_their_fee_once():
    gateway = memory_gateway()
    intent = _executing(gateway)
    for signature in ("sig-a", "sig-b"):
        gateway.ledger.record_send(intent.intent_id, _sent(signature))
    # a taxa de sig-a já tinha sido registrada antes de o processo morrer
    gateway.ledger.add_event(
        "failed_tx_fee",
        {"account": intent.account, "signatures": ["sig-a"], "fee_lamports": 5000},
        intent_id=intent.intent_id,
    )
    provider = mock_provider()
    provider.send_outcome = AsyncMock(side_effect=[TxOutcome.FAILED, TxOutcome.EXPIRED])
    provider.fetch_failed_fees = AsyncMock(return_value=5000)

    await IntentResolver(gateway, provider).run()

    assert gateway.ledger.get(intent.intent_id).status == IntentStatus.FAILED  # type: ignore[union-attr]
    provider.fetch_failed_fees.assert_not_awaited()  # sig-a já paga; sig-b expirou


async def test_a_landed_send_after_failed_ones_books_the_new_fees():
    gateway = memory_gateway()
    intent = _executing(gateway)
    for signature in ("sig-a", "sig-b"):
        gateway.ledger.record_send(intent.intent_id, _sent(signature))
    provider = mock_provider()
    provider.send_outcome = AsyncMock(side_effect=[TxOutcome.FAILED, TxOutcome.LANDED])
    provider.fetch_failed_fees = AsyncMock(return_value=5000)

    await IntentResolver(gateway, provider).run()

    record = gateway.ledger.get(intent.intent_id)
    assert record and record.status == IntentStatus.EXECUTED
    assert record.signature == "sig-b"
    provider.fetch_failed_fees.assert_awaited_once_with(("sig-a",))
    assert gateway.ledger.booked_fee_signatures(intent.intent_id) == {"sig-a"}


async def test_an_error_in_one_intent_does_not_stop_the_others():
    gateway = memory_gateway()
    first, second = _executing(gateway), _executing(gateway)
    gateway.ledger.record_send(first.intent_id, _sent("sig-1"))
    provider = mock_provider()
    provider.send_outcome = AsyncMock(side_effect=RuntimeError("bug"))

    # a segunda (nunca enviada) se resolve mesmo com a primeira quebrando
    assert await IntentResolver(gateway, provider).run() == {second.account}


# --- o registro de envios no ledger -----------------------------------------


def test_a_send_is_recorded_only_on_an_active_intent():
    gateway = memory_gateway()
    intent = _executing(gateway)
    gateway.ledger.record_send(intent.intent_id, _sent("sig-1"))
    assert gateway.ledger.get(intent.intent_id).signature == "sig-1"  # type: ignore[union-attr]
    assert gateway.ledger.sends_of(intent.intent_id) == [_sent("sig-1")]

    gateway.ledger.mark_failed(intent.intent_id, "x")
    with pytest.raises(ValueError, match="sem envio"):
        gateway.ledger.record_send(intent.intent_id, _sent("sig-2"))


async def test_the_gateway_records_each_send_before_it_happens():
    from trader.execution.models.intent import announce_send
    from trader.shared.models import SwapResult

    gateway = memory_gateway()
    intent = make_intent(account="paper:b")
    seen = []

    async def execute():
        announce_send(_sent("sig-1"))
        # já está no ledger quando o envio acontece
        seen.append(gateway.ledger.get(intent.intent_id).signature)  # type: ignore[union-attr]
        return SwapResult("sig-1", USDC.mint, SOL.mint, 1, 2)

    await gateway.submit(intent, execute)

    assert seen == ["sig-1"]


# --- ajudantes ----------------------------------------------------------------


def _executing(gateway):
    intent = make_intent(account="paper:b")
    gateway.ledger.record_intent(intent, PolicyDecision(True))
    return intent


def _sent(signature: str) -> SentTx:
    return SentTx(signature, USDC.mint, SOL.mint, 10_000_000, 100_000_000, 1_000)


def _drop_send_log(gateway, intent_id: str) -> None:
    """Como uma intenção gravada por uma versão de antes do A3."""
    with gateway.ledger.conn:
        gateway.ledger.conn.execute(
            "UPDATE events SET payload = json_remove(payload, '$.send_log') "
            "WHERE intent_id = ?",
            (intent_id,),
        )
