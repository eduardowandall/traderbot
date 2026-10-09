import itertools
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import (
    bonk_quote,
    close_open_ledgers,
    confirms,
    inspection_passes,
    open_ledger,
    signs,
)
from solders.signature import Signature
from solders.solders import SendTransactionResp, VersionedTransaction

from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.execution.trade.venues.jupiter.rpc import AsyncRPCClient
from trader.shared.models import SOLANA_MINTS


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
    confirms(_mock)
    inspection_passes(_mock)
    signs(_mock)
    _mock.send_transaction = AsyncMock(
        return_value=SendTransactionResp(value=Signature.new_unique())
    )
    return _mock


@pytest.fixture
def mock_sleep():
    with mock.patch("asyncio.sleep"):
        yield


@pytest.fixture(autouse=True)
def _send_log():
    """Executores chamados direto (sem o gateway) gravam envios em lugar nenhum.

    O on-chain recusa enviar sem quem grave (A3); dentro do gateway, o
    `send_hook` dele vale por cima deste.
    """
    from trader.execution.models.intent import send_hook

    token = send_hook.set(lambda sent: None)
    yield
    send_hook.reset(token)


@pytest.fixture(autouse=True)
def _close_open_ledgers():
    yield
    close_open_ledgers()


@pytest.fixture
def ledger():
    """Ledger em memória (use `factories.open_ledger()` fora de fixtures)."""
    return open_ledger()


_tmp_counter = itertools.count()


@pytest.fixture(scope="session")
def _tmp_root(tmp_path_factory):
    return tmp_path_factory.mktemp("t", numbered=False)


@pytest.fixture
def tmp_path(_tmp_root):
    """Troca o `tmp_path` do pytest: o dele custa ~18ms por teste no Windows.

    O `mktemp` numerado lista o diretório base a cada teste; aqui um contador
    dá o nome direto (o diretório base continua o do pytest).
    """
    path = _tmp_root / str(next(_tmp_counter))
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    # o estado (.data/: ledger, HALT, carteira paper) e o policy.toml apontam
    # para o diretório do teste, nunca para os arquivos reais do projeto; o
    # chdir só contém escritas relativas incidentais (logs, CSVs de ticks)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / ".data"))
    monkeypatch.setenv("TRADER_POLICY_FILE", str(tmp_path / "policy.toml"))
    monkeypatch.setenv("TRADER_LOG_DIR", str(tmp_path / ".logs"))
    monkeypatch.chdir(tmp_path)
