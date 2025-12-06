import os
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from solana.rpc.api import Client as SolanaClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.solders import (
    Account,
    GetAccountInfoResp,
    GetTokenAccountsByOwnerResp,
    LiteSVM,
    Message,
    MessageV0,
    RpcKeyedAccount,
    RpcResponseContext,
    to_bytes_versioned,
    transfer,
)
from solders.transaction import VersionedTransaction

from trader.models.account_data import AccountBalanceData
from trader.providers.jupiter.jupiter_adapter import JupiterPrivateAPI


@pytest.fixture()
def setenvvar(monkeypatch):
    with mock.patch.dict(os.environ, clear=True):
        envvars = {
            "HELIUS_RPC_URL": "https://mock.com",
            "SOLANA_PRIVATE_KEY": "4YFq9y5f5hi77Bq8kDCE6VgqoAqKGSQN87yW9YeGybpNfqKUG4WxnwhboHGUeXjY7g8262mhL1kCCM9yy8uGvdj7",
        }
        for k, v in envvars.items():
            monkeypatch.setenv(k, v)
        yield  # This is the magical bit which restore the environment after


@pytest.fixture()
def mock_get_account_info():
    with mock.patch.object(
        SolanaClient,
        "get_account_info",
        return_value=GetAccountInfoResp(
            value=Account(
                lamports=123456789,
                owner=Pubkey.from_string("E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"),
                data=b"",
                executable=False,
            ),
            context=RpcResponseContext(slot=0),
        ),
    ):
        yield


@pytest.fixture()
def mock_get_token_accounts_by_owner():
    with mock.patch.object(
        SolanaClient,
        "get_token_accounts_by_owner",
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
        ),
    ):
        yield


class TestJupiterPrivateAPI:
    def test_init(self, setenvvar):
        api = JupiterPrivateAPI(
            wallet_public_key="E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"
        )
        assert api.wallet_public_key == "E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"
        assert api.rpc_url == "https://mock.com"
        assert api._account_id == "solana_wallet"

        assert isinstance(api.wallet, Pubkey)
        assert isinstance(api.client, SolanaClient)
        assert isinstance(api.keypair, Keypair)

    def test_get_account_balance(
        self, setenvvar, mock_get_account_info, mock_get_token_accounts_by_owner
    ):
        api = JupiterPrivateAPI(
            wallet_public_key="E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"
        )

        balance = api.get_account_balance(api.wallet_public_key)
        assert balance == [
            AccountBalanceData(
                available=Decimal("123.456789"),
                on_hold=Decimal("0"),
                symbol="SOL",
                total=Decimal("123.456789"),
            )
        ]


class TestPlaceOrder:
    @pytest.fixture(autouse=True)
    def setup_tests(self, setenvvar):
        self.api = JupiterPrivateAPI(
            wallet_public_key="E6W4RLUxZLQN5mjVfTAv7hTrdLR5Y6nrNvFiW8p1Q1m"
        )

    @mock.patch("requests.get")
    def test_get_quote_with_route(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"routePlan": "test_route"}
        quote = self.api._get_quote_with_route(
            mint_in="So11111111111111111111111111111111111111112",
            mint_out="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            amount_in=1000000000,
            slippage_bps=50,
        )
        assert quote == {"routePlan": "test_route"}

    @mock.patch("requests.get")
    def test_get_quote_with_route_no_route(self, mock_get):
        mock_get.return_value.json.return_value = {}
        with pytest.raises(Exception, match="Nenhuma rota encontrada!"):
            self.api._get_quote_with_route(
                mint_in="So11111111111111111111111111111111111111112",
                mint_out="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount_in=1000000000,
                slippage_bps=50,
            )

    @mock.patch("requests.post")
    def test_get_swap_transaction(self, mock_post):
        mock_post.return_value.json.return_value = {
            "swapTransaction": "AbuRLtc5C9bZtAUT4F4Y2H5SRRUK1HwOFZOK3V4qm/78MDJt+M2de/RCCaI3iTyodDepmrkUWbss0XRHS0lk5AOAAQABAzfDSQC/GjcggrLsDpYz7jAlT+Gca846HqtFb8UQMM9cCWPIi4AX32PV8HrY7/1WgoRc3IATttceZsUMeQ1qx7UAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD2dTRgcJmzcoGH1R3c2WqtHah2H19KvbC1p6BxLDqfoAQICAAEMAgAAAADKmjsAAAAAAA=="
        }
        tx = self.api._get_swap_transaction(quote={"routePlan": "test_route"})
        assert isinstance(tx, VersionedTransaction)

    def test_get_signed_transaction(self):
        keypair = Keypair()
        receiver = Pubkey.new_unique()

        api = JupiterPrivateAPI(wallet_public_key=str(keypair.pubkey()))
        api.keypair = keypair
        ixs = [
            transfer(
                {
                    "from_pubkey": keypair.pubkey(),
                    "to_pubkey": receiver,
                    "lamports": 100_000,
                }
            )
        ]

        client = LiteSVM()
        client.airdrop(keypair.pubkey(), 1_000_000_000)
        blockhash = client.latest_blockhash()
        api.client.get_latest_blockhash = lambda: SimpleNamespace(
            value=SimpleNamespace(blockhash=blockhash)
        )  # type: ignore
        msg = Message.new_with_blockhash(ixs, keypair.pubkey(), blockhash)
        message = MessageV0(
            header=msg.header,
            account_keys=msg.account_keys,
            recent_blockhash=blockhash,
            instructions=msg.instructions,
            address_table_lookups=[],
        )
        tx = VersionedTransaction(message, [keypair])
        signed_tx = api._get_signed_transaction(tx=tx)
        assert isinstance(signed_tx, VersionedTransaction)
        assert signed_tx.signatures[0].verify(
            keypair.pubkey(), to_bytes_versioned(signed_tx.message)
        )

    def test_send_signed_transaction(self):
        keypair = Keypair()
        receiver = Pubkey.new_unique()

        api = JupiterPrivateAPI(wallet_public_key=str(keypair.pubkey()))
        api.keypair = keypair
        ixs = [
            transfer(
                {
                    "from_pubkey": keypair.pubkey(),
                    "to_pubkey": receiver,
                    "lamports": 100_000,
                }
            )
        ]

        client = LiteSVM()
        client.airdrop(keypair.pubkey(), 1_000_000_000)
        blockhash = client.latest_blockhash()
        msg = Message.new_with_blockhash(ixs, keypair.pubkey(), blockhash)
        tx = VersionedTransaction(msg, [keypair])
        tx.signatures = [keypair.sign_message(to_bytes_versioned(msg))]

        def _simulate_transaction(x):
            # signature = client.simulate_transaction(x).meta().signature()

            return SimpleNamespace(value=SimpleNamespace(err=None))

        def _send_raw_transaction(x):
            signature = client.simulate_transaction(tx).meta().signature()

            return SimpleNamespace(value=signature)

        api.client.simulate_transaction = _simulate_transaction  # type: ignore
        api.client.send_raw_transaction = _send_raw_transaction  # type: ignore
        resp = api._send_signed_transaction(tx)
        assert resp.value is not None
