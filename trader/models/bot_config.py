import os
import uuid
from dataclasses import InitVar, dataclass
from enum import StrEnum, auto

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.models import SOLANA_MINTS
from trader.notification.notification_service import (
    NotificationService,
)
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.trading_strategy import TradingStrategy


class RunningMode(StrEnum):
    REAL = auto()
    DRY = auto()


@dataclass
class BotConfig:
    id: str
    name: str  # unique friendly name

    input_mint: str  # a moeda que eu tenho
    output_mint: str  # a moeda que eu vou comprar
    mode: RunningMode
    wallet: InitVar[Keypair]
    provider: AsyncJupiterProvider
    strategy: TradingStrategy
    notifier: NotificationService

    @property
    def currency(self):
        _in = SOLANA_MINTS[self.input_mint].symbol
        _out = SOLANA_MINTS[self.output_mint].symbol
        return f"{_out}-{_in}"


def create_bot_config(mode: str, name: str, symbol: str, provider, strategy, notifier):
    keypair = get_keypair_from_env()
    _out, _in = symbol.split("-")
    SOLANA_MINTS.get_by_symbol(_in)

    return BotConfig(
        id=uuid.uuid4().hex,
        name=name,
        input_mint=SOLANA_MINTS.get_by_symbol(_in).mint,
        output_mint=SOLANA_MINTS.get_by_symbol(_out).mint,
        mode=RunningMode[mode.upper()],
        wallet=keypair,
        provider=provider,
        strategy=strategy,
        notifier=notifier,
    )


def get_keypair_from_env():
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    assert private_key, "Chave privada não definida nas variaveis de ambiente"
    keypair = Keypair.from_base58_string(private_key)

    # Valida que a chave publica que veio a partir da privada é a mesma da carteira
    pub_key_str = os.getenv("SOLANA_PUBLIC_KEY")
    if pub_key_str:
        public_key = Pubkey.from_string(pub_key_str)
        assert keypair.pubkey() == public_key

    return keypair
