from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import confirms, inspection_passes, signs
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.solders import SendTransactionResp

from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.execution.market.jupiter.quote import JupiterQuoteResponse
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentStatus
from trader.execution.trade.accounts.spot import SpotAccount
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger import Ledger
from trader.execution.trade.policy import Policy
from trader.execution.trade.venues.jupiter.provider import AsyncJupiterProvider
from trader.execution.trade.venues.jupiter.rpc import AsyncRPCClient
from trader.execution.trade.venues.jupiter.swap_costs import (
    SwapLegs,
    parse_swap_costs,
    quote_info,
)
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, Order, OrderSide, Position
from trader.shared.models.costs import (
    ONCHAIN,
    QUOTE,
    PnLResult,
    TradeCosts,
    describe_costs,
    trade_rates,
)

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
WALLET = Keypair().pubkey()
OTHER = Keypair().pubkey()


def _tb(index, mint, amount, owner=WALLET):
    return SimpleNamespace(
        account_index=index,
        mint=Pubkey.from_string(mint.mint),
        owner=owner,
        ui_token_amount=SimpleNamespace(amount=str(amount)),
    )


def _meta(fee, pre, post, pre_tokens, post_tokens, err=None):
    return SimpleNamespace(
        fee=fee,
        err=err,
        pre_balances=pre,
        post_balances=post,
        pre_token_balances=pre_tokens,
        post_token_balances=post_tokens,
    )


KEYS = [WALLET, OTHER, OTHER, OTHER, OTHER]
ATA = 2_039_280


class TestParseSwapCosts:
    def test_buy_sol_with_usdc_through_temporary_wsol(self):
        # 10 USDC -> 0.069 SOL (cotado 0.07); taxa 15000 (priority 10000). A
        # conta wSOL temporária é criada e fechada na tx: não aparece nos saldos
        fee = 15_000
        meta = _meta(
            fee,
            pre=[1_000_000_000, ATA, 0, 0, 0],
            post=[1_000_000_000 + 69_000_000 - fee, ATA, 0, 0, 0],
            pre_tokens=[_tb(1, USDC, 100_000_000)],
            post_tokens=[_tb(1, USDC, 90_000_000)],
        )
        legs = SwapLegs(USDC.mint, SOL.mint, 10_000_000, 70_000_000)

        costs = parse_swap_costs(meta, KEYS, 1, WALLET, legs)

        assert costs == TradeCosts(
            source=ONCHAIN,
            fee_lamports=15_000,
            priority_fee_lamports=10_000,
            rent_lamports=0,
            other_lamports=0,
            actual_in_amount=10_000_000,
            actual_out_amount=69_000_000,
            quoted_out_amount=70_000_000,
        )
        assert costs is not None and costs.slippage_raw == 1_000_000

    def test_sell_sol_detects_other_debits(self):
        # 0.07 SOL -> 10.5 USDC; a rota ainda debitou 1000 lamports (ex: gorjeta)
        fee = 5000
        meta = _meta(
            fee,
            pre=[1_000_000_000, ATA, 0, 0, 0],
            post=[1_000_000_000 - 70_000_000 - fee - 1000, ATA, 0, 0, 0],
            pre_tokens=[_tb(1, USDC, 0)],
            post_tokens=[_tb(1, USDC, 10_500_000)],
        )
        legs = SwapLegs(SOL.mint, USDC.mint, 70_000_000, 10_600_000)

        costs = parse_swap_costs(meta, KEYS, 1, WALLET, legs)

        assert costs is not None
        assert costs.actual_in_amount == 70_000_000
        assert costs.actual_out_amount == 10_500_000
        assert costs.other_lamports == 1000
        assert costs.native_cost_lamports == 6000

    def test_opening_a_token_2022_account_measures_real_rent(self):
        rent = 2_074_080  # Token-2022 (ImmutableOwner) custa mais que 2_039_280
        fee = 5000
        meta = _meta(
            fee,
            pre=[1_000_000_000, ATA, 0, 0, 0],
            post=[1_000_000_000 - fee - rent, ATA, 0, rent, 0],
            pre_tokens=[_tb(1, USDC, 100_000_000)],
            post_tokens=[_tb(1, USDC, 90_000_000), _tb(3, JUP, 5_000_000)],
        )
        legs = SwapLegs(USDC.mint, JUP.mint, 10_000_000, 5_100_000)

        costs = parse_swap_costs(meta, KEYS, 1, WALLET, legs)

        assert costs is not None
        assert costs.rent_lamports == rent
        assert costs.other_lamports == 0
        assert (costs.actual_in_amount, costs.actual_out_amount) == (
            10_000_000,
            5_000_000,
        )

    def test_closing_a_persistent_wsol_account_nets_out(self):
        # a carteira tinha uma conta wSOL com 0.05 SOL; a Jupiter a fecha e o
        # SOL volta para o saldo nativo junto com o recebido no swap
        wsol_lamports = ATA + 50_000_000
        fee = 5000
        meta = _meta(
            fee,
            pre=[1_000_000_000, ATA, 0, 0, wsol_lamports],
            post=[1_000_000_000 + 69_000_000 + wsol_lamports - fee, ATA, 0, 0, 0],
            pre_tokens=[_tb(1, USDC, 100_000_000), _tb(4, SOL, 50_000_000)],
            post_tokens=[_tb(1, USDC, 90_000_000)],
        )
        legs = SwapLegs(USDC.mint, SOL.mint, 10_000_000, 70_000_000)

        costs = parse_swap_costs(meta, KEYS, 1, WALLET, legs)

        assert costs is not None
        assert costs.actual_out_amount == 69_000_000
        assert costs.rent_lamports == 0

    def test_ignores_token_accounts_of_other_owners(self):
        fee = 5000
        meta = _meta(
            fee,
            pre=[1_000_000_000, ATA, 0, 0, 0],
            post=[1_000_000_000 + 69_000_000 - fee, ATA, 0, 0, 0],
            pre_tokens=[_tb(1, USDC, 100_000_000), _tb(2, USDC, 7, owner=OTHER)],
            post_tokens=[_tb(1, USDC, 90_000_000), _tb(2, USDC, 9, owner=OTHER)],
        )
        legs = SwapLegs(USDC.mint, SOL.mint, 10_000_000, 70_000_000)
        costs = parse_swap_costs(meta, KEYS, 1, WALLET, legs)
        assert costs is not None and costs.actual_in_amount == 10_000_000

    def test_unreliable_transactions_return_none(self):
        meta = _meta(5000, [0], [0], [], [])
        legs = SwapLegs(USDC.mint, SOL.mint, 1, 1)
        # outra conta pagou a taxa
        assert parse_swap_costs(meta, [OTHER, WALLET], 1, WALLET, legs) is None
        # transação falhou
        failed = _meta(5000, [0], [0], [], [], err="InstructionError")
        assert parse_swap_costs(failed, KEYS, 1, WALLET, legs) is None
        assert parse_swap_costs(None, KEYS, 1, WALLET, legs) is None

    def test_quote_info_collects_lp_fees(self):
        quote = JupiterQuoteResponse.single_route(
            USDC.mint, 10, SOL.mint, 5, price_impact_pct="0.01"
        )
        assert quote_info(quote) == {"lp_fees": {}, "price_impact_pct": "0.01"}
        quote.routePlan[0].swapInfo.feeAmount = "25"
        quote.routePlan[0].swapInfo.feeMint = USDC.mint
        assert quote_info(quote)["lp_fees"] == {USDC.mint: 25}
        assert quote_info(None) == {}


class TestConfirmedTransactionFetch:
    async def test_retries_until_the_rpc_has_the_transaction(self):
        client = AsyncMock()
        tx = object()
        client.get_transaction = AsyncMock(
            side_effect=[
                SimpleNamespace(value=None),
                SimpleNamespace(value=None),
                SimpleNamespace(value=SimpleNamespace(transaction=tx)),
            ]
        )
        rpc = AsyncRPCClient(client=client)
        with mock.patch("asyncio.sleep") as sleep:
            assert (
                await rpc.get_confirmed_transaction(str(Signature.new_unique())) is tx
            )
        assert client.get_transaction.await_count == 3
        assert sleep.await_count == 3
        # "confirmed", não o padrão "finalized" do cliente
        assert (
            str(client.get_transaction.await_args.kwargs["commitment"]) == "confirmed"
        )

    async def test_gives_up(self):
        client = AsyncMock()
        client.get_transaction = AsyncMock(return_value=SimpleNamespace(value=None))
        rpc = AsyncRPCClient(client=client)
        with mock.patch("asyncio.sleep"):
            result = await rpc.get_confirmed_transaction(
                str(Signature.new_unique()), delays=(0, 0)
            )
        assert result is None
        assert client.get_transaction.await_count == 3


def _provider():
    return AsyncJupiterProvider.on_chain(
        Keypair(),
        rpc_client=inspection_passes(AsyncMock(spec=AsyncRPCClient)),
        jupiter_client=AsyncMock(spec=AsyncJupiterClient),
        max_priority_fee_lamports=100_000,
    )


def _result(**kwargs):
    quote = JupiterQuoteResponse.single_route(USDC.mint, 10, SOL.mint, 7)
    return ExecutionResult("sig", USDC.mint, SOL.mint, 10, 7, quote=quote, **kwargs)


class TestFetchSwapCosts:
    async def test_never_raises(self):
        provider = _provider()
        provider.executor.rpc_client.get_confirmed_transaction = AsyncMock(
            side_effect=RuntimeError("rpc caiu")
        )
        costs = await provider.fetch_swap_costs(_result())
        assert costs.source == QUOTE
        assert not costs.known
        assert costs.quoted_out_amount == 7

    async def test_missing_transaction_degrades_to_quote(self):
        provider = _provider()
        provider.executor.rpc_client.get_confirmed_transaction = AsyncMock(
            return_value=None
        )
        assert (await provider.fetch_swap_costs(_result())).source == QUOTE

    async def test_costs_already_known_are_kept(self):
        known = TradeCosts(source="simulated", fee_lamports=5000)
        costs = await _provider().fetch_swap_costs(_result(costs=known))
        assert costs.fee_lamports == 5000 and costs.source == "simulated"

    async def test_cost_failure_never_retries_or_fails_an_executed_swap(self):
        # custos quebrados depois de um swap confirmado: a intenção continua
        # EXECUTED, a transação foi enviada uma única vez
        provider = _provider()
        quote = JupiterQuoteResponse.single_route(
            USDC.mint, USDC.ui_to_raw("10"), SOL.mint, SOL.ui_to_raw("0.1")
        )
        provider.jupiter_client.get_quote = AsyncMock(return_value=quote)
        signs(provider.executor.rpc_client)
        provider.executor.rpc_client.send_transaction = AsyncMock(
            return_value=SendTransactionResp(value=Signature.new_unique())
        )
        confirms(provider.executor.rpc_client)
        provider.executor.rpc_client.get_confirmed_transaction = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        provider.executor.rpc_client.get_account_balance = AsyncMock(return_value={})
        provider.executor.rpc_client.get_lamports = AsyncMock(return_value=0)
        provider.get_account_balance = AsyncMock(
            return_value=[
                SimpleNamespace(mint=USDC.pubkey, available=Decimal("1000")),
            ]
        )
        ledger = Ledger()
        gateway = TradeGateway(ledger, Policy(), False)
        account = SpotAccount(
            SpotVenue(provider),
            USDC.pubkey,
            SOL.pubkey,
            gateway=gateway,
            account_id="t",
        )

        order = await account.buy(Decimal("100"), Decimal("0.1"))

        provider.executor.rpc_client.send_transaction.assert_awaited_once()
        record = ledger.list_intents(1)[0]
        assert record.status == IntentStatus.EXECUTED
        assert order.costs is not None and order.costs.source == QUOTE
        ledger.close()


class TestTradeRates:
    def test_sol_usdc(self):
        # compra SOL com USDC: preço do SOL em USD vem do feed
        rates = trade_rates(Decimal("150"), Decimal("150.3"), True, False, True)
        assert rates.quote_usd == Decimal("1")
        assert rates.sol_usd == Decimal("150")
        assert rates.sol_in_quote == Decimal("150.3")

    def test_usdc_sol(self):
        # compra USDC gastando SOL: 0.9998 USD por USDC / 0.0086 SOL por USDC
        rates = trade_rates(Decimal("0.9998"), Decimal("0.0086"), False, True, False)
        assert rates.sol_usd == Decimal("0.9998") / Decimal("0.0086")
        assert rates.quote_usd == rates.sol_usd
        assert rates.sol_in_quote == Decimal("1")

    def test_jup_sol(self):
        rates = trade_rates(Decimal("0.5"), Decimal("0.004"), False, True, False)
        assert rates.sol_usd == Decimal("125")

    def test_pair_without_sol_has_no_sol_rate(self):
        rates = trade_rates(Decimal("0.5"), Decimal("0.5"), True, False, False)
        assert rates.quote_usd == Decimal("1")
        assert rates.sol_usd is None and rates.sol_in_quote is None


def _order(side, quantity, quote_amount, quote_usd, sol_usd, sol_in_quote, costs):
    return Order(
        "id",
        USDC.mint,
        SOL.mint,
        Decimal(quantity),
        Decimal("100"),
        side,
        datetime(2026, 9, 1),
        quote_amount=Decimal(quote_amount),
        quote_usd=quote_usd,
        sol_usd=sol_usd,
        sol_in_quote=sol_in_quote,
        costs=costs,
    )


def _costs(lamports, source="onchain"):
    return TradeCosts(source=source, fee_lamports=lamports)


class TestNetPnL:
    def test_sol_usdc_round_trip(self):
        one = Decimal("1")
        entry = _order(
            OrderSide.BUY,
            "1",
            "100",
            one,
            Decimal("100"),
            Decimal("100"),
            _costs(10**6),
        )
        exit_ = _order(
            OrderSide.SELL,
            "1",
            "110",
            one,
            Decimal("110"),
            Decimal("110"),
            _costs(10**6),
        )
        pnl = Position(entry, exit_).realized_pnl_detail()

        assert pnl is not None
        assert pnl == PnLResult(
            quote_symbol="USDC",
            gross_quote=Decimal("10"),
            costs_sol=Decimal("0.002"),
            costs_quote=Decimal("0.21"),  # 0.001 x 100 + 0.001 x 110
            gross_usd=Decimal("10"),
            costs_usd=Decimal("0.21"),
            complete=True,
        )
        assert pnl.net_quote == Decimal("9.79")
        assert "[!]" not in pnl.summary()

    def test_partial_sell_prorates_entry(self):
        one = Decimal("1")
        entry = _order(
            OrderSide.BUY, "2", "200", one, None, Decimal("100"), _costs(10**6)
        )
        exit_ = _order(OrderSide.SELL, "1", "110", one, None, Decimal("110"), None)
        pnl = Position(entry, exit_).realized_pnl_detail()
        assert pnl is not None
        assert pnl.gross_quote == Decimal("10")  # 110 - 200/2
        assert pnl.costs_sol == Decimal("0.0005")
        assert not pnl.complete  # a venda não tem custos reais

    def test_pair_without_sol_rate_is_incomplete(self):
        one = Decimal("1")
        entry = _order(OrderSide.BUY, "1", "10", one, None, None, _costs(5000))
        exit_ = _order(OrderSide.SELL, "1", "11", one, None, None, _costs(5000))
        pnl = Position(entry, exit_).realized_pnl_detail()
        assert pnl is not None
        assert pnl.costs_quote is None and pnl.net_quote is None
        assert pnl.net_usd == Decimal("1")  # custos não convertidos
        assert not pnl.complete
        assert "[!] incompleto" in pnl.summary()

    def test_sol_quoted_pair_is_exact_in_sol(self):
        # USDC-SOL: cotação em SOL, custos já em SOL: líquido exato
        rate = Decimal("116")
        one = Decimal("1")
        entry = _order(OrderSide.BUY, "10", "0.1", rate, rate, one, _costs(10_000))
        exit_ = _order(OrderSide.SELL, "10", "0.102", rate, rate, one, _costs(10_000))
        pnl = Position(entry, exit_).realized_pnl_detail()
        assert pnl is not None
        assert pnl.net_quote == Decimal("0.00198")
        assert pnl.complete

    def test_legacy_orders_fall_back_to_usd_formula(self):
        entry = Order(
            "a",
            "in",
            "out",
            Decimal("1"),
            Decimal("100"),
            OrderSide.BUY,
            datetime(2026, 1, 1),
        )
        exit_ = Order(
            "b",
            "in",
            "out",
            Decimal("1"),
            Decimal("105"),
            OrderSide.SELL,
            datetime(2026, 1, 1),
        )
        position = Position(entry, exit_)
        assert position.realized_pnl_detail() is None
        assert position.realized_pnl == Decimal("5")


def test_describe_costs():
    costs = TradeCosts(
        source="onchain",
        fee_lamports=15_000,
        priority_fee_lamports=10_000,
        rent_lamports=2_039_280,
        actual_out_amount=9,
        quoted_out_amount=10,
    )
    text = describe_costs(costs, Decimal("100"))
    assert "taxa 0.000015000 SOL (priority 0.000010000)" in text
    assert "rent 0.002039280 SOL" in text
    assert "slippage 1 raw" in text
    assert "~$0.2054" in text
    assert "desconhecidos" in describe_costs(None)
    assert "desconhecidos" in describe_costs(TradeCosts(source=QUOTE))


@pytest.fixture(autouse=True)
def _no_sleep():
    with mock.patch("asyncio.sleep"):
        yield
