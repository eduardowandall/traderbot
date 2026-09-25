import asyncio
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from trader.async_account import AsyncAccount
from trader.execution import (
    DuplicateIntentError,
    KillSwitch,
    PolicyDeniedError,
    TradeGateway,
)
from trader.ledger import Ledger
from trader.models import SOLANA_MINTS, Order, OrderSide, SwapResult
from trader.models.account_data import MintBalance
from trader.models.intent import (
    IntentRecord,
    IntentSide,
    IntentStatus,
    PolicyDecision,
    TradeIntent,
)
from trader.policy import Policy
from trader.providers import AsyncJupiterProvider
from trader.providers.jupiter.async_jupiter_svc import TransactionSubmittedError

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
RESULT = SwapResult("sig", USDC.mint, SOL.mint, 10_000_000, 100_000_000)


_OPEN_LEDGERS: list[Ledger] = []


@pytest.fixture(autouse=True)
def _close_ledgers():
    yield
    while _OPEN_LEDGERS:
        _OPEN_LEDGERS.pop().close()


def _ledger() -> Ledger:
    ledger = Ledger()
    _OPEN_LEDGERS.append(ledger)
    return ledger


def _gateway(tmp_path, policy=None, real_mode=False):
    return TradeGateway(
        _ledger(),
        policy or Policy(),
        KillSwitch(tmp_path / "HALT"),
        real_mode=real_mode,
    )


def _record(gateway: TradeGateway, intent: TradeIntent) -> IntentRecord:
    record = gateway.ledger.get(intent.intent_id)
    assert record is not None
    return record


def _intent(key=None, notional="10"):
    extra = {"idempotency_key": key} if key else {}
    return TradeIntent(
        source="test",
        account="dry:SOL-USDC",
        side=IntentSide.BUY,
        spend_mint=USDC.mint,
        receive_mint=SOL.mint,
        spend_amount=Decimal("10"),
        notional_usd=Decimal(notional),
        **extra,
    )


class TestSubmit:
    async def test_executes_and_records(self, tmp_path):
        gateway = _gateway(tmp_path)
        execute = AsyncMock(return_value=RESULT)
        intent = _intent()

        assert await gateway.submit(intent, execute) == RESULT
        execute.assert_awaited_once()
        assert _record(gateway, intent).status == IntentStatus.EXECUTED

    async def test_same_idempotency_key_executes_once(self, tmp_path):
        gateway = _gateway(tmp_path)
        execute = AsyncMock(return_value=RESULT)
        await gateway.submit(_intent(key="k"), execute)

        with pytest.raises(DuplicateIntentError):
            await gateway.submit(_intent(key="k"), execute)
        execute.assert_awaited_once()

    async def test_denied_intent_never_executes(self, tmp_path):
        gateway = _gateway(tmp_path)
        execute = AsyncMock(return_value=RESULT)
        intent = _intent(notional="1000")

        with pytest.raises(PolicyDeniedError) as ex:
            await gateway.submit(intent, execute)

        execute.assert_not_awaited()
        assert "acima do limite" in ex.value.reasons[0]
        assert _record(gateway, intent).status == IntentStatus.DENIED

    async def test_real_mode_denied_by_default(self, tmp_path):
        gateway = _gateway(tmp_path, real_mode=True)
        with pytest.raises(PolicyDeniedError):
            await gateway.submit(_intent(), AsyncMock(return_value=RESULT))

    async def test_failure_before_send_is_recorded_and_retryable(self, tmp_path):
        gateway = _gateway(tmp_path)
        intent = _intent(key="k")
        with pytest.raises(RuntimeError):
            await gateway.submit(intent, AsyncMock(side_effect=RuntimeError("rota")))
        record = _record(gateway, intent)
        assert record.status == IntentStatus.FAILED
        assert record.error and "rota" in record.error

        # a mesma chave pode tentar de novo: nada foi executado
        assert await gateway.submit(_intent(key="k"), AsyncMock(return_value=RESULT))

    async def test_unconfirmed_blocks_all_trading(self, tmp_path):
        gateway = _gateway(tmp_path)
        intent = _intent()
        with pytest.raises(TransactionSubmittedError):
            await gateway.submit(
                intent, AsyncMock(side_effect=TransactionSubmittedError("timeout"))
            )
        assert _record(gateway, intent).status == IntentStatus.UNCONFIRMED

        with pytest.raises(PolicyDeniedError, match="sem confirmação"):
            await gateway.submit(_intent(), AsyncMock(return_value=RESULT))

        gateway.ledger.resolve(intent.intent_id, IntentStatus.FAILED, "explorer")
        assert await gateway.submit(_intent(), AsyncMock(return_value=RESULT))

    async def test_cancellation_mid_execution_is_unconfirmed(self, tmp_path):
        gateway = _gateway(tmp_path)
        intent = _intent()
        with pytest.raises(asyncio.CancelledError):
            await gateway.submit(
                intent, AsyncMock(side_effect=asyncio.CancelledError())
            )
        assert _record(gateway, intent).status == IntentStatus.UNCONFIRMED

    async def test_circuit_breaker_and_resume(self, tmp_path):
        gateway = _gateway(tmp_path)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await gateway.submit(_intent(), AsyncMock(side_effect=RuntimeError()))

        with pytest.raises(PolicyDeniedError, match="circuit breaker"):
            await gateway.submit(_intent(), AsyncMock(return_value=RESULT))

        gateway.resume("verificado")
        assert await gateway.submit(_intent(), AsyncMock(return_value=RESULT))

    async def test_kill_switch(self, tmp_path):
        gateway = _gateway(tmp_path)
        gateway.halt("teste")
        execute = AsyncMock(return_value=RESULT)
        with pytest.raises(PolicyDeniedError, match="kill switch"):
            await gateway.submit(_intent(), execute)
        execute.assert_not_awaited()

        gateway.resume()
        assert await gateway.submit(_intent(), execute)


def test_kill_switch_fails_closed(tmp_path, monkeypatch):
    switch = KillSwitch(tmp_path / "HALT")
    assert not switch.is_active()

    def broken(self):
        raise OSError("disco")

    monkeypatch.setattr("pathlib.Path.exists", broken)
    assert switch.is_active()


# --- integração com AsyncAccount -------------------------------------------------


def _account(gateway, usdc="1000", sol="1"):
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_account_balance = AsyncMock(
        return_value=[
            MintBalance(mint=USDC.pubkey, available=Decimal(usdc)),
            MintBalance(mint=SOL.pubkey, available=Decimal(sol)),
        ]
    )
    provider.buy = AsyncMock(
        return_value=SwapResult(
            "buy-sig", USDC.mint, SOL.mint, USDC.ui_to_raw("10"), SOL.ui_to_raw("0.1")
        )
    )
    provider.sell = AsyncMock(
        return_value=SwapResult(
            "sell-sig", SOL.mint, USDC.mint, SOL.ui_to_raw("0.1"), USDC.ui_to_raw("11")
        )
    )
    return AsyncAccount(
        provider, USDC.pubkey, SOL.pubkey, gateway=gateway, account_id="dry:SOL-USDC"
    )


class TestAccountWithGateway:
    async def test_buy_and_sell_are_recorded(self, tmp_path):
        gateway = _gateway(tmp_path)
        account = _account(gateway)

        await account.buy(Decimal("100"), Decimal("0.1"))
        await account.sell(Decimal("110"), Decimal("0.1"))

        buy, sell = reversed(gateway.ledger.list_intents())
        assert buy.intent.side == IntentSide.BUY
        assert buy.intent.notional_usd == Decimal("10.0")
        assert buy.order_json
        assert sell.intent.side == IntentSide.SELL
        assert sell.intent.idempotency_key == "dry:SOL-USDC:sell:buy-sig"
        assert sell.realized_pnl_usd == Decimal("1.0")
        assert gateway.ledger.verify_chain() is None

    async def test_denied_buy_opens_no_position(self, tmp_path):
        gateway = _gateway(tmp_path, policy=Policy(max_trade_usd=Decimal("5")))
        account = _account(gateway)

        with pytest.raises(PolicyDeniedError):
            await account.buy(Decimal("100"), Decimal("0.1"))

        assert account.current_position is None
        account.provider.buy.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_restart_restores_open_position_and_pnl(self, tmp_path):
        gateway = _gateway(tmp_path)
        first = _account(gateway)
        await first.buy(Decimal("100"), Decimal("0.1"))
        await first.sell(Decimal("110"), Decimal("0.1"))
        await first.buy(Decimal("100"), Decimal("0.1"))

        # novo processo: mesma conta, estado vazio
        restarted = _account(gateway)
        restarted.restore_from_ledger()

        assert restarted.current_position is not None
        assert first.current_position is not None
        assert (
            restarted.current_position.entry_order == first.current_position.entry_order
        )
        assert restarted.get_total_realized_pnl() == Decimal("1.0")

    async def test_restart_after_close_has_no_position(self, tmp_path):
        gateway = _gateway(tmp_path)
        first = _account(gateway)
        await first.buy(Decimal("100"), Decimal("0.1"))
        await first.sell(Decimal("110"), Decimal("0.1"))

        restarted = _account(gateway)
        restarted.restore_from_ledger()
        assert restarted.current_position is None

    async def test_reconcile_flags_missing_tokens(self, tmp_path):
        gateway = _gateway(tmp_path)
        account = _account(gateway, sol="0.05")
        account.current_position = None
        await _account(gateway).buy(Decimal("100"), Decimal("0.1"))
        account.restore_from_ledger()

        assert await account.reconcile_position() is False
        events = gateway.ledger.conn.execute(
            "SELECT type FROM events WHERE type = 'reconcile_mismatch'"
        ).fetchall()
        assert len(events) == 1

    async def test_reconcile_ok(self, tmp_path):
        gateway = _gateway(tmp_path)
        account = _account(gateway, sol="1")
        await account.buy(Decimal("100"), Decimal("0.1"))
        assert await account.reconcile_position() is True

    async def test_second_sell_of_same_position_is_blocked(self, tmp_path):
        gateway = _gateway(tmp_path)
        account = _account(gateway)
        await account.buy(Decimal("100"), Decimal("0.1"))
        entry = account.current_position
        await account.sell(Decimal("110"), Decimal("0.1"))

        # ex: estado em memória antigo tentando vender a mesma entrada de novo
        account.current_position = entry
        assert entry
        entry.exit_order = None
        with pytest.raises(DuplicateIntentError):
            await account.sell(Decimal("110"), Decimal("0.1"))
        assert account.provider.sell.await_count == 1  # type: ignore[attr-defined]


def test_order_timestamp_is_preserved_by_restore(tmp_path):
    ledger = _ledger()
    order = Order(
        "id",
        USDC.mint,
        SOL.mint,
        Decimal("1"),
        Decimal("2"),
        OrderSide.BUY,
        datetime(2026, 1, 1, 10, 30),
    )
    intent = _intent()
    ledger.record_intent(intent, PolicyDecision(True))
    ledger.mark_executed(intent.intent_id, RESULT)
    ledger.attach_order(intent.intent_id, order)
    account = AsyncAccount(
        AsyncMock(spec=AsyncJupiterProvider),
        USDC.pubkey,
        SOL.pubkey,
        gateway=TradeGateway(ledger, Policy(), KillSwitch(tmp_path / "H"), False),
        account_id="dry:SOL-USDC",
    )
    account.restore_from_ledger()
    assert account.current_position
    assert account.current_position.entry_order.timestamp == datetime(
        2026, 1, 1, 10, 30
    )


async def test_non_stablecoin_input_has_unknown_notional(tmp_path):
    # USDC-SOL: gasta SOL; quantity*price misturaria SOL com USD e subestimaria
    # o valor pelo preço do SOL, furando max_trade_usd
    gateway = _gateway(tmp_path)
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_account_balance = AsyncMock(
        return_value=[MintBalance(mint=SOL.pubkey, available=Decimal("1"))]
    )
    account = AsyncAccount(
        provider, SOL.pubkey, USDC.pubkey, gateway=gateway, account_id="dry:USDC-SOL"
    )

    with pytest.raises(PolicyDeniedError, match="desconhecido"):
        await account.buy(Decimal("1"), Decimal("0.5"))
    provider.buy.assert_not_awaited()

    allowed = TradeGateway(
        gateway.ledger,
        Policy(allow_unknown_notional=True),
        gateway.kill_switch,
        real_mode=False,
    )
    account.gateway = allowed
    provider.buy = AsyncMock(
        return_value=SwapResult("s", SOL.mint, USDC.mint, SOL.ui_to_raw("0.5"), 1)
    )
    await account.buy(Decimal("1"), Decimal("0.5"))
    record = gateway.ledger.list_intents(1)[0]
    assert record.intent.notional_usd is None
