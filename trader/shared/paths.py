"""Onde o bot guarda o estado (ledger, carteira paper, logs) e a política.

Os caminhos não dependem do diretório atual: vários bots (um por spec)
precisam enxergar o mesmo ledger e a mesma carteira paper de onde quer que
sejam iniciados.

Valores relativos (inclusive nas variáveis de ambiente) são resolvidos a
partir da raiz do projeto, nunca do cwd. Os caminhos são lidos a cada chamada,
então as variáveis de ambiente valem mesmo se mudarem depois do import.
"""

import os
from pathlib import Path

# raiz do checkout (instalação editável, o padrão do `uv sync`); numa
# instalação não editável aponte TRADER_DATA_DIR e TRADER_POLICY_FILE
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def data_dir() -> Path:
    """`TRADER_DATA_DIR`, ou `<raiz do projeto>/.data`."""
    return _resolve(os.getenv("TRADER_DATA_DIR") or ".data")


def logs_dir() -> Path:
    """`TRADER_LOG_DIR`, ou `<raiz do projeto>/.logs`."""
    return _resolve(os.getenv("TRADER_LOG_DIR") or ".logs")


def policy_file() -> Path:
    """`TRADER_POLICY_FILE`, ou `<raiz do projeto>/policy.toml`."""
    return _resolve(os.getenv("TRADER_POLICY_FILE") or "policy.toml")
