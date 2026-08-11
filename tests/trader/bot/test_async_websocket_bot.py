from decimal import Decimal
from unittest import mock

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
        mock.call.get_candles(bonk.mint),
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
                inAmount="1000000000",
                outputMint=bonk.mint,
                outAmount="50000000",
                otherAmountThreshold="49500000",
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
                            inAmount="1000000000",
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
        mock.call.get_quote(
            bonk.mint,
            usdc.mint,
            50000000,
            50,
        ),
        mock.call.get_swap_transaction(
            JupiterQuoteResponse(
                inputMint=usdc.mint,
                inAmount="1000000000",
                outputMint=bonk.mint,
                outAmount="50000000",
                otherAmountThreshold="49500000",
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
                            inAmount="1000000000",
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
    ]
    for idx, _call in enumerate(
        [c for c in mock_jupiter_client.mock_calls if c[0] != "__str__"]
    ):
        assert _call == expected_calls[idx], f"call[{idx}] diferente do esperado"


def assert_rpc_client_mock_calls(mock_rpc_client, keypair, usdc, bonk):
    expected_calls = [
        mock.call.get_lamports(keypair.pubkey()),
        mock.call.get_account_balance(keypair.pubkey()),
        mock.call.sign_transaction(mock.ANY, keypair),
        mock.call.simulate_transaction(mock.ANY),
        mock.call.send_transaction(mock.ANY),
        mock.call.check_signature_is_confirmed(mock.ANY),
        mock.call.get_lamports(keypair.pubkey()),
        mock.call.get_account_balance(keypair.pubkey()),
        mock.call.sign_transaction(mock.ANY, keypair),
        mock.call.simulate_transaction(mock.ANY),
        mock.call.send_transaction(mock.ANY),
        mock.call.check_signature_is_confirmed(mock.ANY),
        mock.call.get_lamports(keypair.pubkey()),
        mock.call.get_account_balance(keypair.pubkey()),
    ]
    for idx, _call in enumerate(
        [c for c in mock_rpc_client.mock_calls if c[0] != "__str__"]
    ):
        assert _call == expected_calls[idx], f"call[{idx}] diferente do esperado"
