"""Onde o bot guarda o estado (ledger, kill switch, carteira paper) e a política.

Os caminhos não dependem do diretório atual: o bot e os comandos de CLI
(`halt`, `resume`, `ledger resolve`) precisam enxergar os mesmos arquivos de
onde quer que sejam chamados. Um `HALT` gravado em outra pasta seria um kill
switch que o bot rodando nunca vê.

Valores relativos (inclusive nas variáveis de ambiente) são resolvidos a
partir da raiz do projeto, nunca do cwd. Os caminhos são lidos a cada chamada,
então as variáveis de ambiente valem mesmo se mudarem depois do import.
"""

import os
from pathlib import Path

# raiz do checkout (instalação editável, o padrão do `uv sync`); numa
# instalação não editável aponte TRADER_DATA_DIR e TRADER_POLICY_FILE
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def data_dir() -> Path:
    """`TRADER_DATA_DIR`, ou `<raiz do projeto>/.data`."""
    return _resolve(os.getenv("TRADER_DATA_DIR") or ".data")


def policy_file() -> Path:
    """`TRADER_POLICY_FILE`, ou `<raiz do projeto>/policy.toml`."""
    return _resolve(os.getenv("TRADER_POLICY_FILE") or "policy.toml")
