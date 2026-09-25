import asyncio
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from solders.keypair import Keypair

from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.models import SOLANA_MINTS, OrderSide, OrderSignal, Position
from trader.models.bot_config import BotConfig, RunningMode
from trader.notification import NullNotificationService
from trader.providers import (
    AsyncJupiterProvider,
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
)
from trader.trading_strategy import TradingStrategy


class FakeStrategy(TradingStrategy):
    def __init__(self):
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


async def test_async_websocket_bot_complete(
    mock_sleep, mock_jupiter_client, mock_rpc_client
):
    usdc = SOLANA_MINTS.get_by_symbol("USDC")
    bonk = SOLANA_MINTS.get_by_symbol("BONK")

    keypair = Keypair()
    strategy = FakeStrategy()
    config = BotConfig(
        id="id-bot-config-teste-complete",
        name="name-bot-config-teste-complete",
        input_mint=usdc.mint,
        output_mint=bonk.mint,
        mode=RunningMode.DRY,
        wallet=keypair,
        provider=AsyncJupiterProvider(
            keypair=keypair,
            rpc_client=mock_rpc_client,
            jupiter_client=mock_jupiter_client,
        ),
        strategy=strategy,
        notifier=NullNotificationService(),
    )

    bot = AsyncWebsocketTradingBot(config)
    bot.stop_when_error = True
    await bot._run()
    assert strategy.count == 2

    assert_jupiter_mock_calls(mock_jupiter_client, keypair, usdc, bonk)
    assert_rpc_client_mock_calls(mock_rpc_client, keypair, usdc, bonk)


def assert_jupiter_mock_calls(mock_jupiter_client, keypair, usdc, bonk):
    expected_calls = [
        mock.call.get_candles(bonk.mint, interval=mock.ANY, candle_qty=100),
        mock.call.get_price(bonk.mint),
        mock.call.get_quote(
            usdc.mint,
            bonk.mint,
            50000000,
            50,
        ),
        mock.call.get_swap_transaction(
            JupiterQuoteResponse(
                inputMint=usdc.mint,
                inAmount="50000000",
                outputMint=bonk.mint,
                outAmount="5000000",
                otherAmountThreshold="4975000",
                swapMode="ExactIn",
                slippageBps=50,
                platformFee=None,
                priceImpactPct="0.5",
                routePlan=[
                    JupiterRoutePlan(
                        swapInfo=JupiterSwapInfo(
                            ammKey="FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                            label="HumidiFi",
                            inputMint=bonk.mint,
                            outputMint=usdc.mint,
                            inAmount="50000000",
                            outAmount="7106793162",
                            feeAmount="0",
                            feeMint=bonk.mint,
                        ),
                        percent=100,
                    )
                ],
                contextSlot=123456789,
                timeTaken=0.5,
            ),
            keypair.pubkey(),
        ),
        mock.call.get_price(bonk.mint),
        # venda de 50 BONK (5 decimais) = 5_000_000 raw
        mock.call.get_quote(
            bonk.mint,
            usdc.mint,
            5000000,
            50,
        ),
        mock.call.get_swap_transaction(
            JupiterQuoteResponse(
                inputMint=usdc.mint,
                inAmount="50000000",
                outputMint=bonk.mint,
                outAmount="5000000",
                otherAmountThreshold="4975000",
                swapMode="ExactIn",
                slippageBps=50,
                platformFee=None,
                priceImpactPct="0.5",
                routePlan=[
                    JupiterRoutePlan(
                        swapInfo=JupiterSwapInfo(
                            ammKey="FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                            label="HumidiFi",
                            inputMint=bonk.mint,
                            outputMint=usdc.mint,
                            inAmount="50000000",
                            outAmount="7106793162",
                            feeAmount="0",
                            feeMint=bonk.mint,
                        ),
                        percent=100,
                    )
                ],
                contextSlot=123456789,
                timeTaken=0.5,
            ),
            keypair.pubkey(),
        ),
        mock.call.get_price(bonk.mint),
        mock.call.aclose(),
    ]
    actual_calls = [c for c in mock_jupiter_client.mock_calls if c[0] != "__str__"]
    assert len(actual_calls) == len(expected_calls)
    for idx, _call in enumerate(actual_calls):
        assert _call == expected_calls[idx], f"call[{idx}] diferente do esperado"


def assert_rpc_client_mock_calls(mock_rpc_client, keypair, usdc, bonk):
    expected_calls = [
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
    ]
    actual_calls = [
        c
        for c in mock_rpc_client.mock_calls
        if c[0].isidentifier() and c[0] != "__str__"
    ]
    assert len(actual_calls) == len(expected_calls)
    for idx, _call in enumerate(actual_calls):
        assert _call == expected_calls[idx], f"call[{idx}] diferente do esperado"


def _bot(provider, strategy):
    usdc = SOLANA_MINTS.get_by_symbol("USDC")
    bonk = SOLANA_MINTS.get_by_symbol("BONK")
    keypair = Keypair()
    return AsyncWebsocketTradingBot(
        BotConfig(
            id="id",
            name="name",
            input_mint=usdc.mint,
            output_mint=bonk.mint,
            mode=RunningMode.DRY,
            wallet=keypair,
            provider=provider,
            strategy=strategy,
            notifier=NullNotificationService(),
        )
    )


async def test_errors_back_off_exponentially_and_reset():
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_candles = AsyncMock(return_value=[])
    provider.get_account_balance = AsyncMock(return_value=[])
    provider.get_price_ticker_data = AsyncMock(
        side_effect=[
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
    bot = _bot(provider, strategy)

    with mock.patch("asyncio.sleep") as sleep:
        await bot._run()

    assert [c.args[0] for c in sleep.await_args_list] == [1.0, 2.0, 4.0, 1.0]


async def test_stop_ends_the_loop():
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_candles = AsyncMock(return_value=[])
    provider.get_account_balance = AsyncMock(return_value=[])
    provider.get_price_ticker_data = AsyncMock(return_value=Decimal("1"))
    strategy = mock.Mock(spec=TradingStrategy)
    bot = _bot(provider, strategy)
    strategy.on_market_refresh.side_effect = lambda *a: bot.stop()

    await bot._run()

    strategy.on_market_refresh.assert_called_once()


async def test_shutdown_closes_provider_on_normal_exit():
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_candles = AsyncMock(return_value=[])
    provider.get_account_balance = AsyncMock(return_value=[])
    provider.get_price_ticker_data = AsyncMock(side_effect=KeyboardInterrupt())
    bot = _bot(provider, mock.Mock(spec=TradingStrategy))

    await bot._run()

    provider.aclose.assert_awaited_once()
    assert bot.is_running is False


async def test_cancellation_closes_provider_and_propagates():
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_candles = AsyncMock(return_value=[])
    provider.get_price_ticker_data = AsyncMock(side_effect=asyncio.CancelledError())
    notifier = mock.Mock(spec=NullNotificationService)
    bot = _bot(provider, mock.Mock(spec=TradingStrategy))
    bot.notification_service = notifier

    with pytest.raises(asyncio.CancelledError):
        await bot._run()

    provider.aclose.assert_awaited_once()
    notifier.send_message.assert_called_with("Bot interrompido pelo usuário")


async def test_shutdown_survives_close_errors():
    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_candles = AsyncMock(return_value=[])
    provider.get_price_ticker_data = AsyncMock(side_effect=KeyboardInterrupt())
    provider.aclose = AsyncMock(side_effect=RuntimeError("already closed"))
    bot = _bot(provider, mock.Mock(spec=TradingStrategy))

    await bot._run()  # não propaga o erro de fechamento


async def test_policy_denial_pauses_orders_but_not_the_strategy():
    from trader.execution import PolicyDeniedError
    from trader.models.intent import IntentSide, TradeIntent

    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.get_candles = AsyncMock(return_value=[])
    provider.get_account_balance = AsyncMock(return_value=[])
    provider.get_price_ticker_data = AsyncMock(return_value=Decimal("1"))
    strategy = mock.Mock(spec=TradingStrategy)
    strategy.on_market_refresh.return_value = OrderSignal(OrderSide.BUY, Decimal("1"))
    bot = _bot(provider, strategy)

    clock = [0.0]
    bot.monotonic = lambda: clock[0]
    intent = TradeIntent("t", "a", IntentSide.BUY, "x", "y", Decimal("1"))
    denied = PolicyDeniedError(intent, ("limite",))
    place_order = AsyncMock(side_effect=denied)
    bot.account.place_order = place_order

    with pytest.raises(PolicyDeniedError):
        await bot.process_market_data(Decimal("1"))
    await bot._on_error(denied, 1.0)

    # dentro do cooldown: a estratégia roda, mas nenhuma ordem é tentada
    for _ in range(3):
        assert await bot.process_market_data(Decimal("1")) is None
    assert place_order.await_count == 1
    assert strategy.on_market_refresh.call_count == 4

    clock[0] += bot.denial_cooldown
    with pytest.raises(PolicyDeniedError), mock.patch("asyncio.sleep"):
        await bot.process_market_data(Decimal("1"))
    assert place_order.await_count == 2
