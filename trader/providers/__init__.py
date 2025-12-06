"""
Módulo de interfaces da API do Mercado Bitcoin e Jupiter.
"""

from .base_api import PrivateAPIBase, PublicAPIBase
from .jupiter.jupiter_adapter import (
    FakeJupiterPrivateAPI,
    JupiterPrivateAPI,
    JupiterPublicAPIAdapter,
)
from .jupiter.jupiter_public_api import SOLANA_TOKENS, JupiterPublicAPI

__all__ = [
    # Base interfaces
    "PublicAPIBase",
    "PrivateAPIBase",
    # Jupiter (APIs nativas)
    "JupiterPublicAPI",
    "JupiterPrivateAPI",
    "SOLANA_TOKENS",
    # Jupiter (Adaptadores compatíveis)
    "JupiterPublicAPIAdapter",
    "FakeJupiterPrivateAPI",
]
