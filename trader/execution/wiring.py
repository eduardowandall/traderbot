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

from trader.execution.market.perps.reader import JupiterPerpsReader
from trader.execution.market.prices import JupiterPriceOracle, PriceOracle
from trader.execution.models.mode import RunningMode
from trader.execution.models.venue import PerpVenue
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import load_policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.jupiter.provider import (
    DEFAULT_MAX_QUOTE_DEVIATION_PCT,
    AsyncJupiterProvider,
)
from trader.execution.trade.venues.jupiter_perps.venue import JupiterPerpsVenue
from trader.execution.trade.venues.paper import (
    DEFAULT_PAPER_BALANCES,
    SimulatedWallet,
    paper_provider,
)
from trader.execution.trade.venues.paper.perps import SimulatedPerpsVenue
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.notification.notification_service import Notifier
from trader.shared.paths import data_dir


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
    max_priority_fee_lamports: int,  # da política do modo
    on_wallet_created: Callable[[str], None] = lambda message: None,
    wallet: SimulatedWallet | None = None,  # paper: a carteira já aberta
    **limits,
) -> AsyncJupiterProvider:
    """Provider real (com chave) ou paper (carteira simulada, sem chave).

    Os dois conferem cada quote contra a Price API (`max_quote_deviation_pct`).
    O teto da priority fee vai para a Jupiter no real e é cobrado inteiro em
    paper, por perna.
    """
    limits.setdefault("max_quote_deviation_pct", DEFAULT_MAX_QUOTE_DEVIATION_PCT)
    if mode == RunningMode.PAPER:
        return paper_provider(
            wallet or open_paper_wallet(on_wallet_created),
            priority_fee_lamports=max_priority_fee_lamports,
            **limits,
        )
    return AsyncJupiterProvider.on_chain(
        keypair=keypair_from_env(),
        max_priority_fee_lamports=max_priority_fee_lamports,
        **limits,
    )


def _perps_venue(
    provider: AsyncJupiterProvider,
    wallet: SimulatedWallet | None,
    prices: PriceOracle,
    enabled: bool,
    fee_cap: int,
) -> PerpVenue | None:
    """O venue de perps do modo: o paper na carteira simulada (A8, com a taxa
    de empréstimo ao vivo, A10); o real só com `perps_enabled` (A11b)."""
    if wallet is not None:
        return SimulatedPerpsVenue(
            wallet,
            prices,
            priority_fee_lamports=fee_cap,
            borrow_rates=JupiterPerpsReader(),
        )
    executor = provider.executor
    if not enabled or not isinstance(executor, OnChainExecutor):
        return None
    # a chave fica no executor do spot: um caminho de envio só
    return JupiterPerpsVenue(executor, JupiterPerpsReader(), provider, fee_cap)


def build_trade_service(
    mode: RunningMode,
    on_wallet_created: Callable[[str], None] = lambda message: None,
    prices: PriceOracle | None = None,
    notifier: Notifier | None = None,
    **limits,
) -> TradeService:
    """O serviço que executa as ordens dos buckets deste modo.

    `prices`: o oráculo USD do processo (o `PriceHub` do `serve`), para
    o serviço e a conferência das quotes; sem ele, a Price API no cliente
    Jupiter das quotes. `notifier`: avisos ao dono (vendas que a carteira
    não cobre, A5). Quem cria fecha: `service.aclose()` (provider) e o
    ledger do gateway.
    """
    policy = load_policy(mode=str(mode))
    fee_cap = policy.max_priority_fee_lamports
    wallet = open_paper_wallet(on_wallet_created) if mode == RunningMode.PAPER else None
    provider = build_provider(mode, fee_cap, on_wallet_created, wallet, **limits)
    prices = prices or JupiterPriceOracle(provider.jupiter_client)
    provider.usd_prices = prices.usd_prices
    gateway = TradeGateway.for_mode(mode, policy)  # política + ledger do modo
    perps = _perps_venue(provider, wallet, prices, policy.perps_enabled, fee_cap)
    return TradeService(
        SpotVenue(provider),
        gateway,
        mode=str(mode),
        prices=prices,
        notifier=notifier,
        perps=perps,
    )
