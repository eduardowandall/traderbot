from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from solders.keypair import Keypair
from solders.signature import Signature
from solders.solders import (
    SendTransactionResp,
    VersionedTransaction,
)

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
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.trading_strategy import TradingStrategy

# class FakeSolanaClient:
#     keypair = Keypair()

#     def __init__(self):
#         self.client = LiteSVM()
#         self.setup()

#     def setup(self):
#         owner = FakeSolanaClient.keypair.pubkey()
#         usdc_mint = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
#         ata = get_associated_token_address(owner, usdc_mint)
#         usdc_to_own = 1_000_000_000_000
#         token_acc = TokenAccount(
#             mint=usdc_mint,
#             owner=owner,
#             amount=usdc_to_own,
#             delegate=None,
#             state=TokenAccountState.Initialized,
#             is_native=None,
#             delegated_amount=0,
#             close_authority=None,
#         )
#         client = self.client
#         client.set_account(
#             ata,
#             Account(
#                 lamports=1_000_000_000,
#                 data=bytes(token_acc),
#                 owner=TOKEN_PROGRAM_ID,
#                 executable=False,
#             ),
#         )
#         client.set_account(
#             owner,
#             Account(
#                 lamports=1_000_000_000,
#                 data=bytes(token_acc),
#                 owner=TOKEN_PROGRAM_ID,
#                 executable=False,
#             ),
#         )
#         raw_account = client.get_account(ata)
#         assert raw_account is not None
#         raw_account_data = raw_account.data
#         assert TokenAccount.from_bytes(raw_account_data).amount == usdc_to_own

#     async def is_connected(self):
#         return True

#     async def send_raw_transaction(self, tx: bytes):
#         result = self.client.send_transaction(Transaction.from_bytes(tx))
#         if isinstance(result, FailedTransactionMetadata):
#             raise Exception(f"Invalid Result FailedTransactionMetadata {result}")
#         signature = result.signature()
#         return SendTransactionResp(value=signature)

#     async def simulate_transaction(self, tx: VersionedTransaction):
#         return SimulateTransactionResp(
#             RpcSimulateTransactionResult(),
#             RpcResponseContext(slot=123),  # type: ignore
#         )

#     async def get_account_info(self, pubkey: Pubkey):
#         account = self.client.get_account(pubkey)
#         return GetAccountInfoResp(value=account, context=RpcResponseContext(slot=123))

#     async def get_token_accounts_by_owner(self, owner: Pubkey, opts: TokenAccountOpts):
#         return GetTokenAccountsByOwnerResp(
#             value=[
#                 RpcKeyedAccount(
#                     pubkey=owner,
#                     account=Account(
#                         lamports=123456789,
#                         owner=Pubkey.from_string(
#                             "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
#                         ),
#                         data=base64.b64decode(
#                             "xvp6877brTo9ZfNqq8l0MbG75MLS9uDkfKYCA0UvXWGWLa9jbECytyNphzh7LQSyyvt90dqHEa5Ir7Dj07qtEhXNWwcAAAAA"
#                         ),
#                         executable=False,
#                     ),
#                 )
#             ],
#             context=RpcResponseContext(slot=0),
#         )

#     async def get_latest_blockhash(self):
#         self.client.set_blockhash_check(False)
#         return GetLatestBlockhashResp(
#             RpcBlockhash(self.client.latest_blockhash(), 1),
#             context=RpcResponseContext(slot=0),
#         )


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
            # gambiarra pra forcar o bot parar depois de comprar e vender uma vez
            raise KeyboardInterrupt()

        self.count += 1
        if current_position:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        else:
            return OrderSignal(OrderSide.BUY, self.calculate_quantity(balance, price))


@pytest.fixture
def mock_jupiter_client():
    usdc = SOLANA_MINTS.get_by_symbol("USDC")
    bonk = SOLANA_MINTS.get_by_symbol("BONK")
    _mock = AsyncMock(spec=AsyncJupiterClient)
    _mock.get_candles = AsyncMock(return_value=[])
    _mock.get_price = AsyncMock(return_value=Decimal("1.0"))
    _mock.get_quote = AsyncMock(
        return_value=JupiterQuoteResponse(
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
        )
    )
    _mock.get_swap_transaction = AsyncMock(return_value=AsyncMock(VersionedTransaction))
    return _mock


@pytest.fixture
def mock_rpc_client():
    usdc = SOLANA_MINTS.get_by_symbol("USDC")
    bonk = SOLANA_MINTS.get_by_symbol("BONK")
    sol = SOLANA_MINTS.get_by_symbol("SOL")

    _mock = AsyncMock(spec=AsyncRPCClient)
    _mock.get_lamports = AsyncMock(return_value=sol.ui_to_raw(Decimal("99.0")))
    _mock.get_account_balance = AsyncMock(
        side_effect=[
            {
                bonk.pubkey: bonk.ui_to_raw(Decimal("50.0")),
                usdc.pubkey: usdc.ui_to_raw(Decimal("100.0")),
            },
            # segunda chamada desconta quantidade comprada
            {
                bonk.pubkey: bonk.ui_to_raw(Decimal("62.5")),
                usdc.pubkey: usdc.ui_to_raw(Decimal("50.0")),
            },
            # terceira chamada volta ao saldo inicial depois da venda
            {
                bonk.pubkey: bonk.ui_to_raw(Decimal("50.0")),
                usdc.pubkey: usdc.ui_to_raw(Decimal("100.0")),
            },
        ]
    )
    _mock.check_signature_is_confirmed = AsyncMock(return_value=True)
    _mock.sign_transaction = AsyncMock(side_effect=lambda tx, keypair: tx)
    _mock.send_transaction = AsyncMock(
        return_value=SendTransactionResp(value=Signature.new_unique())
    )
    return _mock


@pytest.fixture
def mock_sleep():
    with mock.patch("asyncio.sleep"):
        yield


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
