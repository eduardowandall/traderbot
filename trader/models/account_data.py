from dataclasses import dataclass
from decimal import Decimal

from solders.pubkey import Pubkey


@dataclass
class MintBalance:
    available: Decimal
    mint: Pubkey
