"""Suíte live: fala com a Jupiter de verdade (quotes, candles, websocket).

Roda só com `uv run pytest -m live`; `pytest .` e o CI a pulam. Nada aqui
pode assinar: a chave, o RPC e o Telegram saem do ambiente, e o
`isolated_workdir` (conftest da raiz) mantém ledger, HALT, carteira paper e
política em `tmp_path`. Só modo paper e comandos de leitura.
"""

from pathlib import Path

import pytest

LIVE_DIR = Path(__file__).parent
SECRETS = (
    "SOLANA_PRIVATE_KEY",
    "SOLANA_PUBLIC_KEY",
    "HELIUS_RPC_URL",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_BOT_TOKEN",
)


@pytest.hookimpl(tryfirst=True)  # antes da seleção por -m
def pytest_collection_modifyitems(items):
    # todo teste desta pasta é live, mesmo se esquecerem o marcador
    for item in items:
        if LIVE_DIR in Path(item.fspath).parents:
            item.add_marker(pytest.mark.live)


@pytest.fixture(autouse=True)
def no_secrets(monkeypatch):
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
