"""
Dataclasses para dados da API Jupiter (Solana DEX Aggregator).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class JupiterSwapInfo:
    """Informações sobre um swap individual em uma rota"""

    amm_key: str
    label: str
    input_mint: str
    output_mint: str
    in_amount: str
    out_amount: str
    fee_amount: str
    fee_mint: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterSwapInfo":
        """Cria uma instância JupiterSwapInfo a partir de um dicionário"""
        return cls(
            amm_key=data["ammKey"],
            label=data["label"],
            input_mint=data["inputMint"],
            output_mint=data["outputMint"],
            in_amount=data["inAmount"],
            out_amount=data["outAmount"],
            fee_amount=data["feeAmount"],
            fee_mint=data["feeMint"],
        )


@dataclass
class JupiterRoutePlan:
    """Plano de rota para um swap"""

    swap_info: JupiterSwapInfo
    percent: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterRoutePlan":
        """Cria uma instância JupiterRoutePlan a partir de um dicionário"""
        return cls(
            swap_info=JupiterSwapInfo.from_dict(data["swapInfo"]),
            percent=data["percent"],
        )


@dataclass
class JupiterQuoteResponse:
    """Resposta da API de quote da Jupiter"""

    input_mint: str
    in_amount: str
    output_mint: str
    out_amount: str
    other_amount_threshold: str
    swap_mode: str
    slippage_bps: int
    platform_fee: Optional[Dict[str, Any]]
    price_impact_pct: str
    route_plan: List[JupiterRoutePlan]
    context_slot: Optional[int]
    time_taken: Optional[float]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterQuoteResponse":
        """Cria uma instância JupiterQuoteResponse a partir de um dicionário"""
        return cls(
            input_mint=data["inputMint"],
            in_amount=data["inAmount"],
            output_mint=data["outputMint"],
            out_amount=data["outAmount"],
            other_amount_threshold=data["otherAmountThreshold"],
            swap_mode=data["swapMode"],
            slippage_bps=data["slippageBps"],
            platform_fee=data.get("platformFee"),
            price_impact_pct=data["priceImpactPct"],
            route_plan=[JupiterRoutePlan.from_dict(rp) for rp in data["routePlan"]],
            context_slot=data.get("contextSlot"),
            time_taken=data.get("timeTaken"),
        )


@dataclass
class JupiterSwapResponse:
    """Resposta da API de swap da Jupiter"""

    swap_transaction: str
    last_valid_block_height: int
    prioritization_fee_lamports: Optional[int]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterSwapResponse":
        """Cria uma instância JupiterSwapResponse a partir de um dicionário"""
        return cls(
            swap_transaction=data["swapTransaction"],
            last_valid_block_height=data["lastValidBlockHeight"],
            prioritization_fee_lamports=data.get("prioritizationFeeLamports"),
        )


@dataclass
class JupiterTokenInfo:
    """Informações sobre um token na Solana"""

    address: str
    chain_id: int
    decimals: int
    name: str
    symbol: str
    logo_uri: Optional[str]
    tags: List[str]
    extensions: Optional[Dict[str, Any]]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterTokenInfo":
        """Cria uma instância JupiterTokenInfo a partir de um dicionário"""
        return cls(
            address=data["address"],
            chain_id=data["chainId"],
            decimals=data["decimals"],
            name=data["name"],
            symbol=data["symbol"],
            logo_uri=data.get("logoURI"),
            tags=data.get("tags", []),
            extensions=data.get("extensions"),
        )


@dataclass
class JupiterPriceData:
    """Dados de preço de um token"""

    id: str
    mint_symbol: str
    vs_token: str
    vs_token_symbol: str
    price: Decimal

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterPriceData":
        """Cria uma instância JupiterPriceData a partir de um dicionário"""
        return cls(
            id=data["id"],
            mint_symbol=data["mintSymbol"],
            vs_token=data["vsToken"],
            vs_token_symbol=data["vsTokenSymbol"],
            price=Decimal(str(data["price"])),
        )
