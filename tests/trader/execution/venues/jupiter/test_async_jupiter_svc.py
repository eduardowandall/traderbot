from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import simulation
from solana.rpc.async_api import AsyncClient as SolanaClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.rpc.responses import RpcBlockhash
from solders.signature import Signature
from solders.solders import (
    Account,
    GetAccountInfoResp,
    GetLatestBlockhashResp,
    GetTokenAccountsByOwnerResp,
    LiteSVM,
    Message,
    MessageV0,
    RpcKeyedAccount,
    RpcResponseContext,
    SendTransactionResp,
    to_bytes_versioned,
    transfer,
)
from solders.transaction import VersionedTransaction

from trader.execution.models.account_data import MintBalance
from trader.execution.venues import (
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
)
from trader.execution.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.execution.venues.jupiter.async_rpc_client import AsyncRPCClient
from trader.shared.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.costs import DEFAULT_MAX_PRIORITY_FEE_LAMPORTS


@pytest.fixture()
def fake_solana_client():
    client = AsyncMock(spec=SolanaClient)
    lite_svm = LiteSVM()
    client.is_connected = AsyncMock(return_value=True)
    client.get_account_info = AsyncMock(
        return_value=GetAccountInfoResp(
            value=Account(
                lamports=123456789,
                owner=Pubkey.from_string("E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"),
                data=b"",
                executable=False,
            ),
            context=RpcResponseContext(slot=0),
        )
    )
    client.get_token_accounts_by_owner = AsyncMock(
        return_value=GetTokenAccountsByOwnerResp(
            value=[
                RpcKeyedAccount(
                    pubkey=Pubkey.from_string(
                        "E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"
                    ),
                    account=Account(
                        lamports=123456789,
                        owner=Pubkey.from_string(
                            "E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"
                        ),
                        data=b"4YFq9y5f5hi77Bq8kDCE6VgqoAqKGSQN87yW9YeGybpNfqKUG4WxnwhboHGUeXjY7g8262mhL1kCCM9yy8uGvdj7",
                        executable=False,
                    ),
                )
            ],
            context=RpcResponseContext(slot=0),
        )
    )
    client.get_latest_blockhash = AsyncMock(
        return_value=GetLatestBlockhashResp(
            RpcBlockhash(lite_svm.latest_blockhash(), 1),
            context=RpcResponseContext(slot=0),
        )
    )
    # a inspeção (B7) pede a carteira e as contas de token: nada muda
    token = bytes(client.get_token_accounts_by_owner.return_value.value[0].account.data)
    client.simulate_transaction = AsyncMock(
        return_value=simulation(lamports=123456789, tokens=[token])
    )
    client.send_raw_transaction = AsyncMock(
        return_value=SendTransactionResp(value=Signature.new_unique())
    )
    return client


class TestAsyncJupiterProvider:
    def test_init(self):
        keypair = Keypair()
        rpc_client = AsyncMock(spec=AsyncRPCClient)
        jupiter_client = AsyncMock(spec=AsyncJupiterClient)
        api = AsyncJupiterProvider.on_chain(
            keypair,
            rpc_client=rpc_client,
            jupiter_client=jupiter_client,
            max_priority_fee_lamports=100_000,
        )

        assert isinstance(api.executor.keypair, Keypair)
        assert api.executor.keypair == keypair
        assert api.executor.rpc_client is rpc_client
        assert api.jupiter_client is jupiter_client

    async def test_get_account_balance(self, fake_solana_client):
        api = AsyncJupiterProvider.on_chain(
            Keypair(),
            rpc_client=AsyncRPCClient(client=fake_solana_client),
            jupiter_client=AsyncMock(spec=AsyncJupiterClient),
            max_priority_fee_lamports=100_000,
        )

        balance = await api.get_account_balance()
        assert balance == [
            MintBalance(
                available=Decimal("0.123456789"),
                mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
            )
        ]


class TestPlaceOrder:
    @pytest.fixture(autouse=True)
    def setup_tests(self):
        self.jupiter_client = AsyncMock(spec=AsyncJupiterClient)
        self.api = AsyncJupiterProvider.on_chain(
            Keypair(),
            rpc_client=AsyncMock(spec=AsyncRPCClient),
            jupiter_client=self.jupiter_client,
            max_priority_fee_lamports=100_000,
        )

    async def test_buy_forwards_slippage(self):
        sol = SOLANA_MINTS.get_by_symbol("SOL").pubkey
        usdc = SOLANA_MINTS.get_by_symbol("USDC").pubkey
        swap = AsyncMock(return_value="sig")
        self.api.swap_with_details = swap

        result = await self.api.buy(usdc, sol, Decimal("10"), slippage_bps=100)
        assert result == "sig"
        swap.assert_awaited_once_with(str(usdc), str(sol), 10_000_000, slippage_bps=100)

    async def test_sell_forwards_slippage(self):
        sol = SOLANA_MINTS.get_by_symbol("SOL").pubkey
        usdc = SOLANA_MINTS.get_by_symbol("USDC").pubkey
        swap = AsyncMock(return_value="sig")
        self.api.swap_with_details = swap

        # SOL-USDC: vender 1 SOL (9 decimais) em troca de USDC (6 decimais)
        result = await self.api.sell(usdc, sol, Decimal("1"), slippage_bps=100)
        assert result == "sig"
        swap.assert_awaited_once_with(
            str(sol), str(usdc), 1_000_000_000, slippage_bps=100, fail_closed=False
        )

    async def test_swap_with_details_escalates_slippage(self):
        self.api.max_slippage_bps = 200
        do_swap = AsyncMock(
            side_effect=[Exception("erro 1"), Exception("erro 2"), "sig"]
        )
        self.api._do_swap = do_swap

        result = await self.api.swap_with_details(
            "mint_in", "mint_out", 1000, slippage_bps=100
        )
        assert result == "sig"
        do_swap.assert_has_calls(
            [
                mock.call("mint_in", "mint_out", 1000, 100, fail_closed=True),
                mock.call("mint_in", "mint_out", 1000, 100, fail_closed=True),
                mock.call("mint_in", "mint_out", 1000, 125, fail_closed=True),
            ]
        )

    async def test_swap_with_details_default_slippage(self):
        do_swap = AsyncMock(return_value="sig")
        self.api._do_swap = do_swap

        result = await self.api.swap_with_details("mint_in", "mint_out", 1000)
        assert result == "sig"
        do_swap.assert_awaited_once_with(
            "mint_in", "mint_out", 1000, 50, fail_closed=True
        )

    async def test_get_quote_with_route(self):
        quote_response = JupiterQuoteResponse(
            inputMint="So11111111111111111111111111111111111111112",
            inAmount="1000000000",
            outputMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            outAmount="50000000",
            otherAmountThreshold="49500000",
            swapMode="ExactIn",
            slippageBps=50,
            platformFee=None,
            priceImpactPct="0.005",
            routePlan=[
                JupiterRoutePlan(
                    swapInfo=JupiterSwapInfo(
                        ammKey="key",
                        label="Raydium",
                        inputMint="mint1",
                        outputMint="mint2",
                        inAmount="1000",
                        outAmount="500",
                        feeAmount="10",
                        feeMint="mint1",
                    ),
                    percent=100,
                )
            ],
            contextSlot=123456789,
            timeTaken=0.5,
        )
        get_quote = AsyncMock(return_value=quote_response)
        self.jupiter_client.get_quote = get_quote

        quote = await self.api._get_quote_with_route(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            amount_in=1000000000,
            slippage_bps=50,
        )
        assert quote == quote_response
        get_quote.assert_called_once_with(
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1000000000,
            50,
        )

    async def test_get_quote_with_route_no_route(self):
        quote_response = JupiterQuoteResponse(
            inputMint="So11111111111111111111111111111111111111112",
            inAmount="1000000000",
            outputMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            outAmount="50000000",
            otherAmountThreshold="49500000",
            swapMode="ExactIn",
            slippageBps=50,
            platformFee=None,
            priceImpactPct="0.005",
            routePlan=[],
            contextSlot=123456789,
            timeTaken=0.5,
        )
        self.jupiter_client.get_quote = AsyncMock(return_value=quote_response)

        with pytest.raises(Exception, match="Nenhuma rota encontrada!"):
            await self.api._get_quote_with_route(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount_in=1000000000,
                slippage_bps=50,
            )

    async def test_get_swap_transaction(self):
        quote = JupiterQuoteResponse(
            inputMint="So11111111111111111111111111111111111111112",
            inAmount="1000000000",
            outputMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            outAmount="50000000",
            otherAmountThreshold="49500000",
            swapMode="ExactIn",
            slippageBps=50,
            platformFee=None,
            priceImpactPct="0.005",
            routePlan=[
                JupiterRoutePlan(
                    swapInfo=JupiterSwapInfo(
                        ammKey="FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                        label="HumidiFi",
                        inputMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        outputMint="So11111111111111111111111111111111111111112",
                        inAmount="1000000000",
                        outAmount="7106793162",
                        feeAmount="0",
                        feeMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    ),
                    percent=100,
                )
            ],
            contextSlot=123456789,
            timeTaken=0.5,
        )
        get_swap_transaction = AsyncMock(
            return_value=AsyncMock(spec=VersionedTransaction)
        )
        self.jupiter_client.get_swap_transaction = get_swap_transaction

        tx = await self.api.executor._get_swap_transaction(quote=quote)
        assert isinstance(tx, VersionedTransaction)
        # sem teto dado, o padrão (o de `policy.toml` vem pela `wiring`)
        get_swap_transaction.assert_called_once_with(
            quote, self.api.executor.pubkey, DEFAULT_MAX_PRIORITY_FEE_LAMPORTS
        )

    async def test_get_signed_transaction(self, fake_solana_client):
        keypair = Keypair()
        receiver = Pubkey.new_unique()

        api = AsyncJupiterProvider.on_chain(
            keypair=keypair,
            rpc_client=AsyncRPCClient(client=fake_solana_client),
            jupiter_client=AsyncMock(spec=AsyncJupiterClient),
            max_priority_fee_lamports=100_000,
        )
        ixs = [
            transfer(
                {
                    "from_pubkey": keypair.pubkey(),
                    "to_pubkey": receiver,
                    "lamports": 100_000,
                }
            )
        ]

        lite_svm = LiteSVM()
        blockhash = lite_svm.latest_blockhash()
        msg = Message.new_with_blockhash(ixs, keypair.pubkey(), blockhash)
        message = MessageV0(
            header=msg.header,
            account_keys=msg.account_keys,
            recent_blockhash=blockhash,
            instructions=msg.instructions,
            address_table_lookups=[],
        )
        tx = VersionedTransaction(message, [keypair])
        signed_tx = await api.executor._get_signed_transaction(tx=tx)
        assert isinstance(signed_tx, VersionedTransaction)
        assert signed_tx.signatures[0].verify(
            keypair.pubkey(), to_bytes_versioned(signed_tx.message)
        )

    async def test_send_signed_transaction(self, fake_solana_client):
        keypair = Keypair()
        receiver = Pubkey.new_unique()

        service = AsyncJupiterProvider.on_chain(
            keypair=keypair,
            rpc_client=AsyncRPCClient(client=fake_solana_client),
            jupiter_client=AsyncMock(spec=AsyncJupiterClient),
            max_priority_fee_lamports=100_000,
        )
        ixs = [
            transfer(
                {
                    "from_pubkey": keypair.pubkey(),
                    "to_pubkey": receiver,
                    "lamports": 100_000,
                }
            )
        ]

        lite_svm = LiteSVM()
        blockhash = lite_svm.latest_blockhash()
        msg = Message.new_with_blockhash(ixs, keypair.pubkey(), blockhash)
        tx = VersionedTransaction(msg, [keypair])
        tx.signatures = [keypair.sign_message(to_bytes_versioned(msg))]

        resp = await service.executor._send_signed_transaction(tx)
        assert resp.value is not None
