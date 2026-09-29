from datetime import datetime
from decimal import Decimal

from factories import open_ledger

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution import KillSwitch, TradeGateway
from trader.execution.fills import Fill
from trader.models import SOLANA_MINTS, OrderSide
from trader.models.costs import TradeCosts
from trader.models.intent import IntentSide, IntentStatus
from trader.models.order import SwapResult
from trader.paper import SimulatedWallet, paper_provider
from trader.policy import Policy
from trader.trading_service.manual import swap_order
from trader.trading_service.protocol import ReplyStatus, SwapRequest
from trader.trading_service.service import TradeService

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 9, 1, 12, 0)


def _fill(spend, receive, spent, received, costs=None):
    result = SwapResult(
        "sig",
        spend.mint,
        receive.mint,
        spend.ui_to_raw(spent),
        receive.ui_to_raw(received),
    )
    return Fill(result, costs)


class TestSwapOrder:
    def test_spending_a_stable_prices_the_token_in_usd(self):
        order = swap_order(_fill(USDC, SOL, "150", "1"), USDC, SOL, T0)

        assert (order.input_mint, order.output_mint) == (USDC.mint, SOL.mint)
        assert order.side == OrderSide.BUY
        assert order.quantity == Decimal("1")
        assert order.quote_amount == Decimal("150")
        assert order.price == order.fill_price == Decimal("150")
        assert (order.quote_usd, order.sol_usd) == (Decimal("1"), Decimal("150"))
        assert order.timestamp == T0

    def test_receiving_a_stable_gives_the_usd_value_of_what_was_spent(self):
        order = swap_order(_fill(SOL, USDC, "2", "300"), SOL, USDC, T0)

        assert order.price == Decimal("1")  # USD por USDC recebido
        assert order.quote_usd == Decimal("150")  # USD por SOL gasto
        assert order.sol_usd == Decimal("150")

    def test_without_a_stable_the_usd_rates_are_unknown(self):
        order = swap_order(_fill(SOL, JUP, "1", "200"), SOL, JUP, T0)

        assert order.price == order.fill_price == Decimal("0.005")
        assert (order.quote_usd, order.sol_usd, order.sol_in_quote) == (None,) * 3

    def test_uses_the_real_amounts_when_known(self):
        costs = TradeCosts(
            "onchain",
            fee_lamports=5000,
            actual_in_amount=USDC.ui_to_raw("150"),
            actual_out_amount=SOL.ui_to_raw("0.99"),
        )
        order = swap_order(_fill(USDC, SOL, "150", "1", costs), USDC, SOL, T0)

        assert order.quantity == Decimal("0.99")
        assert order.costs is costs


def _service(tmp_path, ledger, policy=None):
    client = ReplayQuoteClient(USDC, Decimal(0))
    client.tick = Tick(T0, Decimal("100"))
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    gateway = TradeGateway(
        ledger, policy or Policy(), KillSwitch(tmp_path / "HALT"), False
    )
    provider = paper_provider(wallet, jupiter_client=client)
    return TradeService(provider, gateway, mode="paper", clock=lambda: T0), wallet


def _swap(amount="10", key=None):
    return SwapRequest(USDC.mint, JUP.mint, Decimal(amount), idempotency_key=key)


class TestTradeServiceSwap:
    async def test_records_the_order_and_costs_in_the_manual_bucket(self, tmp_path):
        ledger = open_ledger()
        service, wallet = _service(tmp_path, ledger)

        reply = await service.swap(_swap("10"))

        assert reply.status == ReplyStatus.FILLED
        assert reply.order is not None
        assert reply.order.quantity == Decimal("0.1")
        assert wallet.balance(JUP.mint) == Decimal("0.1")
        (record,) = ledger.list_intents()
        assert record.intent.account == "paper:manual"
        assert record.intent.side == IntentSide.SWAP
        assert record.intent.source == "cli"
        assert record.status == IntentStatus.EXECUTED
        assert record.order_json
        report = ledger.pnl_report("paper:manual")["paper:manual"]
        assert report.trades == 1
        assert report.fee_lamports > 0  # a taxa de rede simulada foi contada

    async def test_a_denial_is_a_reply_not_an_exception(self, tmp_path):
        ledger = open_ledger()
        service, _ = _service(tmp_path, ledger, Policy(max_trade_usd=Decimal("5")))

        reply = await service.swap(_swap("10"))

        assert reply.status == ReplyStatus.DENIED
        assert "acima do limite" in reply.reasons[0]

    async def test_same_token_on_both_sides_is_rejected(self, tmp_path):
        service, _ = _service(tmp_path, open_ledger())

        reply = await service.swap(SwapRequest(USDC.mint, USDC.mint, Decimal("1")))

        assert reply.status == ReplyStatus.REJECTED

    async def test_a_repeated_key_runs_once(self, tmp_path):
        service, wallet = _service(tmp_path, open_ledger())

        first = await service.swap(_swap("10", key="k"))
        second = await service.swap(_swap("10", key="k"))

        assert first.status == ReplyStatus.FILLED
        assert second.status == ReplyStatus.DENIED
        assert wallet.balance(JUP.mint) == Decimal("0.1")
