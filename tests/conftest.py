from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import bonk_quote, close_open_ledgers, open_ledger
from solders.signature import Signature
from solders.solders import SendTransactionResp, VersionedTransaction

from trader.models import SOLANA_MINTS
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient


@pytest.fixture
def mock_jupiter_client():
    _mock = AsyncMock(spec=AsyncJupiterClient)
    _mock.get_candles = AsyncMock(return_value=[])
    _mock.get_price = AsyncMock(return_value=Decimal("1.0"))
    _mock.get_quote = AsyncMock(return_value=bonk_quote())
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


@pytest.fixture(autouse=True)
def _close_open_ledgers():
    yield
    close_open_ledgers()


@pytest.fixture
def ledger():
    """Ledger em memória (use `factories.open_ledger()` fora de fixtures)."""
    return open_ledger()


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    # o estado (.data/: ledger, HALT, carteira paper) e o policy.toml apontam
    # para o diretório do teste, nunca para os arquivos reais do projeto; o
    # chdir só contém escritas relativas incidentais (logs, CSVs de ticks)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / ".data"))
    monkeypatch.setenv("TRADER_POLICY_FILE", str(tmp_path / "policy.toml"))
    monkeypatch.chdir(tmp_path)
