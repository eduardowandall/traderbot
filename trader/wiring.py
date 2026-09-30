"""Montagem dos componentes por modo (camada app): chave, provider, gateway.

É o único lugar que decide, a partir do modo, *onde* os swaps executam
(on-chain com a chave, ou a carteira simulada do paper) e qual
ledger/política valem. Estratégias e o bot não conhecem o modo: recebem os
componentes já montados.
"""

import os
from collections.abc import Callable

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.execution import TradeGateway
from trader.market.prices import JupiterPriceOracle
from trader.models.mode import RunningMode
from trader.paper import DEFAULT_PAPER_BALANCES, SimulatedWallet, paper_provider
from trader.paths import data_dir
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.trading_service.service import TradeService


def keypair_from_env() -> Keypair:
    """Chave de `SOLANA_PRIVATE_KEY`, conferida contra `SOLANA_PUBLIC_KEY`."""
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    if not private_key:
        raise ValueError("Chave privada não definida (SOLANA_PRIVATE_KEY)")
    keypair = Keypair.from_base58_string(private_key)
    public_key = os.getenv("SOLANA_PUBLIC_KEY")
    if public_key and keypair.pubkey() != Pubkey.from_string(public_key):
        raise ValueError("SOLANA_PUBLIC_KEY não corresponde à chave privada")
    return keypair


def paper_wallet_path():
    return data_dir() / "paper-wallet.json"


def open_paper_wallet(
    on_created: Callable[[str], None] = lambda message: None,
) -> SimulatedWallet:
    """Carteira do modo paper; cria com os saldos padrão se não existir."""
    wallet = SimulatedWallet(paper_wallet_path())
    if wallet.is_empty:
        wallet.reset(DEFAULT_PAPER_BALANCES)
        on_created(
            f"Carteira paper criada com {DEFAULT_PAPER_BALANCES} em "
            f"{wallet.path} (apague o arquivo para recomeçar)"
        )
    return wallet


def build_provider(
    mode: RunningMode,
    on_wallet_created: Callable[[str], None] = lambda message: None,
    **limits,
) -> AsyncJupiterProvider:
    """Provider real (com chave) ou paper (carteira simulada, sem chave)."""
    if mode == RunningMode.PAPER:
        return paper_provider(open_paper_wallet(on_wallet_created), **limits)
    return AsyncJupiterProvider.on_chain(keypair=keypair_from_env(), **limits)


def build_trade_service(
    mode: RunningMode,
    on_wallet_created: Callable[[str], None] = lambda message: None,
    **limits,
) -> TradeService:
    """O serviço que executa as ordens dos buckets deste modo.

    Quem cria fecha: `service.aclose()` (provider) e o ledger do gateway.
    """
    provider = build_provider(mode, on_wallet_created, **limits)
    # preços USD pela Price API, no mesmo cliente Jupiter das quotes
    prices = JupiterPriceOracle(provider.jupiter_client)
    gateway = TradeGateway.for_mode(mode)  # política + ledger do modo
    return TradeService(provider, gateway, mode=str(mode), prices=prices)
