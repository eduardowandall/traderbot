import asyncio
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from solders.keypair import Keypair

from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.market import JupiterMarketData
from trader.models import SOLANA_MINTS, Interval, OrderSide, OrderSignal, Position
from trader.notification import NullNotificationService
from trader.providers import (
    AsyncJupiterProvider,
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
)
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.trading_service.local import LocalTradeClient
from trader.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    TradeServiceError,
)
from trader.trading_service.service import TradeService
from trader.trading_strategy import TradingStrategy

USDC = SOLANA_MINTS.get_by_symbol("USDC")
BONK = SOLANA_MINTS.get_by_symbol("BONK")


class FakeStrategy(TradingStrategy):
    def __init__(self):
        super().__init__()
        self.count = 0

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        if self.count == 2:
            # encerra o bot após uma compra e uma venda
            raise KeyboardInterrupt()

        self.count += 1
        if current_position:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        else:
            return OrderSignal(OrderSide.BUY, self.calculate_quantity(balance, price))


def _market_client():
    # o preço vem por um cliente próprio (websocket); swaps usam outro (HTTP)
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_candles = AsyncMock(return_value=[])
    client.get_price = AsyncMock(return_value=Decimal("1.0"))
    return client


async def test_async_websocket_bot_complete(
    mock_sleep, mock_jupiter_client, mock_rpc_client
):
    keypair = Keypair()
    strategy = FakeStrategy()
    market_client = _market_client()
    provider = AsyncJupiterProvider.on_chain(
        keypair, rpc_client=mock_rpc_client, jupiter_client=mock_jupiter_client
    )
    trader = LocalTradeClient(
        TradeService(provider, gateway=None),
        "BONK-USDC",
        USDC.mint,
        BONK.mint,
        owns_service=True,
    )
    bot = AsyncWebsocketTradingBot(
        BotConfig(
            name="name-bot-config-teste-complete",
            symbol="BONK-USDC",
            strategy=strategy,
            market=JupiterMarketData(market_client),
            trader=trader,
            notifier=NullNotificationService(),
        )
    )
    bot.stop_when_error = True
    await bot.arun()
    assert strategy.count == 2

    assert_market_mock_calls(market_client)
    assert_jupiter_mock_calls(mock_jupiter_client, keypair)
    assert_rpc_client_mock_calls(mock_rpc_client, keypair)


def _assert_calls(mock_client, expected_calls):
    actual_calls = [
        c for c in mock_client.mock_calls if c[0].isidentifier() and c[0] != "__str__"
    ]
    assert len(actual_calls) == len(expected_calls), actual_calls
    for idx, _call in enumerate(actual_calls):
        assert _call == expected_calls[idx], f"call[{idx}] diferente do esperado"


def assert_market_mock_calls(market_client):
    _assert_calls(
        market_client,
        [
            mock.call.get_candles(BONK.mint, interval=mock.ANY, candle_qty=100),
            mock.call.get_price(BONK.mint),
            mock.call.get_price(BONK.mint),
            mock.call.get_price(BONK.mint),
            mock.call.aclose(),
        ],
    )


def _quote():
    return JupiterQuoteResponse(
        inputMint=USDC.mint,
        inAmount="50000000",
        outputMint=BONK.mint,
        outAmount="5000000",
        otherAmountThreshold="4975000",
        swapMode="ExactIn",
        slippageBps=50,
        platformFee=None,
        priceImpactPct="0.005",
        routePlan=[
            JupiterRoutePlan(
                swapInfo=JupiterSwapInfo(
                    ammKey="FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                    label="HumidiFi",
                    inputMint=BONK.mint,
                    outputMint=USDC.mint,
                    inAmount="50000000",
                    outAmount="7106793162",
                    feeAmount="0",
                    feeMint=BONK.mint,
                ),
                percent=100,
            )
        ],
        contextSlot=123456789,
        timeTaken=0.5,
    )


def assert_jupiter_mock_calls(mock_jupiter_client, keypair):
    _assert_calls(
        mock_jupiter_client,
        [
            mock.call.get_quote(USDC.mint, BONK.mint, 50000000, 50),
            mock.call.get_swap_transaction(_quote(), keypair.pubkey()),
            # venda de 50 BONK (5 decimais) = 5_000_000 raw
            mock.call.get_quote(BONK.mint, USDC.mint, 5000000, 50),
            mock.call.get_swap_transaction(_quote(), keypair.pubkey()),
            mock.call.aclose(),
        ],
    )


def assert_rpc_client_mock_calls(mock_rpc_client, keypair):
    _assert_calls(
        mock_rpc_client,
        [
            mock.call.get_lamports(keypair.pubkey()),
            mock.call.get_account_balance(keypair.pubkey()),
            mock.call.sign_transaction(mock.ANY, keypair),
            mock.call.simulate_transaction(mock.ANY),
            mock.call.send_transaction(mock.ANY),
            mock.call.check_signature_is_confirmed(mock.ANY),
            # custos lidos da transação confirmada, depois da execução
            mock.call.get_confirmed_transaction(mock.ANY),
            mock.call.get_lamports(keypair.pubkey()),
            mock.call.get_account_balance(keypair.pubkey()),
            mock.call.sign_transaction(mock.ANY, keypair),
            mock.call.simulate_transaction(mock.ANY),
            mock.call.send_transaction(mock.ANY),
            mock.call.check_signature_is_confirmed(mock.ANY),
            # custos lidos da transação confirmada, depois da execução
            mock.call.get_confirmed_transaction(mock.ANY),
            mock.call.get_lamports(keypair.pubkey()),
            mock.call.get_account_balance(keypair.pubkey()),
            mock.call.aclose(),
        ],
    )


class FakeTrader:
    """`TradeClient` falso: bucket fixo e respostas programadas."""

    def __init__(self, replies=(), status=BucketStatus.ACTIVE):
        self.replies = list(replies)
        self.status = status
        self.requests = []
        self.opened = False
        self.closed = 0

    async def open(self):
        self.opened = True

    async def bucket(self):
        return BucketSnapshot("b", Decimal("100"), None, Decimal(0), status=self.status)

    async def submit(self, request):
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else OrderReply.of_denial(("x",))

    async def aclose(self):
        self.closed += 1


def _market(prices=(Decimal("1"),)):
    market = AsyncMock(spec=JupiterMarketData)
    market.get_candles = AsyncMock(return_value=[])
    market.get_price = AsyncMock(side_effect=list(prices))
    return market


def _bot(market, strategy, trader=None, **config):
    return AsyncWebsocketTradingBot(
        BotConfig(
            name="name",
            symbol="BONK-USDC",
            strategy=strategy,
            market=market,
            trader=trader or FakeTrader(),
            notifier=NullNotificationService(),
            **config,
        )
    )


async def test_errors_back_off_exponentially_and_reset():
    market = _market(
        [
            Exception("ws down"),
            Exception("ws down"),
            Exception("ws down"),
            Decimal("1"),
            Exception("ws down"),
            KeyboardInterrupt(),
        ]
    )
    strategy = mock.Mock(spec=TradingStrategy)
    strategy.on_market_refresh.return_value = None
    bot = _bot(market, strategy)

    with mock.patch("asyncio.sleep") as sleep:
        await bot.arun()

    assert [c.args[0] for c in sleep.await_args_list] == [1.0, 2.0, 4.0, 1.0]


async def test_stop_ends_the_loop():
    market = _market()
    market.get_price = AsyncMock(return_value=Decimal("1"))
    strategy = mock.Mock(spec=TradingStrategy)
    bot = _bot(market, strategy)
    strategy.on_market_refresh.side_effect = lambda *a: bot.stop()

    await bot.arun()

    strategy.on_market_refresh.assert_called_once()


async def test_startup_opens_the_bucket_and_shutdown_closes_everything():
    market = _market([KeyboardInterrupt()])
    trader = FakeTrader()
    bot = _bot(market, mock.Mock(spec=TradingStrategy), trader)

    await bot.arun()

    assert trader.opened
    assert trader.closed == 1
    market.aclose.assert_awaited_once()
    assert bot.is_running is False


async def test_cancellation_closes_and_propagates():
    market = _market([asyncio.CancelledError()])
    trader = FakeTrader()
    notifier = mock.Mock(spec=NullNotificationService)
    bot = _bot(market, mock.Mock(spec=TradingStrategy), trader)
    bot.notification_service = notifier

    with pytest.raises(asyncio.CancelledError):
        await bot.arun()

    market.aclose.assert_awaited_once()
    assert trader.closed == 1
    notifier.send_message.assert_called_with("Bot interrompido pelo usuário")


async def test_shutdown_survives_close_errors():
    market = _market([KeyboardInterrupt()])
    market.aclose = AsyncMock(side_effect=RuntimeError("already closed"))
    trader = FakeTrader()
    bot = _bot(market, mock.Mock(spec=TradingStrategy), trader)

    await bot.arun()  # não propaga o erro de fechamento
    assert trader.closed == 1  # o trader fecha mesmo com erro no market


def _buy_strategy():
    strategy = mock.Mock(spec=TradingStrategy)
    strategy.on_market_refresh.return_value = OrderSignal(
        OrderSide.BUY, Decimal("1"), rationale="porque sim"
    )
    return strategy


def _snapshot(status=BucketStatus.ACTIVE):
    return BucketSnapshot("b", Decimal("100"), None, Decimal(0), status=status)


async def test_denied_order_pauses_orders_but_not_the_strategy():
    strategy = _buy_strategy()
    trader = FakeTrader()  # nega todas
    bot = _bot(_market(), strategy, trader)
    clock = [0.0]
    bot.monotonic = lambda: clock[0]

    assert await bot.process_market_data(Decimal("1"), _snapshot()) is None
    # dentro do cooldown: a estratégia roda, mas nenhuma ordem é tentada
    for _ in range(3):
        assert await bot.process_market_data(Decimal("1"), _snapshot()) is None
    assert len(trader.requests) == 1
    assert strategy.on_market_refresh.call_count == 4
    assert trader.requests[0].rationale == "porque sim"

    clock[0] += bot.denial_cooldown
    await bot.process_market_data(Decimal("1"), _snapshot())
    assert len(trader.requests) == 2


async def test_error_reply_goes_through_the_backoff():
    trader = FakeTrader([OrderReply.of_error("RuntimeError: rpc caiu")])
    bot = _bot(_market(), _buy_strategy(), trader)
    with pytest.raises(TradeServiceError, match="rpc caiu"):
        await bot.process_market_data(Decimal("1"), _snapshot())


async def test_retiring_bucket_stops_the_bot():
    strategy = _buy_strategy()
    market = _market([Decimal("1"), Decimal("1")])
    bot = _bot(market, strategy, FakeTrader(status=BucketStatus.RETIRING))

    await bot.arun()

    strategy.on_market_refresh.assert_not_called()
    assert market.get_price.await_count == 1


async def test_warmup_and_tick_callback():
    class Warm(TradingStrategy):
        def warmup(self):
            return Interval.HOUR_1, 24

        def on_market_refresh(self, price, spread, balance, current_position):
            raise KeyboardInterrupt()

    market = _market([Decimal("2")])
    ticks = []
    bot = _bot(market, Warm(), on_tick=lambda ts, price: ticks.append(price))

    await bot.arun()

    market.get_candles.assert_awaited_once_with(BONK.mint, Interval.HOUR_1, 24)
    assert ticks == [Decimal("2")]
