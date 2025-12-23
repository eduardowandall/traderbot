"""
Dataclasses para dados da API Jupiter (Solana DEX Aggregator).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class JupiterSwapInfo:
    """Informações sobre um swap individual em uma rota"""

    ammKey: str
    label: str
    inputMint: str
    outputMint: str
    inAmount: str
    outAmount: str
    feeAmount: str | None
    feeMint: str | None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterSwapInfo":
        """Cria uma instância JupiterSwapInfo a partir de um dicionário"""
        return cls(
            ammKey=data["ammKey"],
            label=data["label"],
            inputMint=data["inputMint"],
            outputMint=data["outputMint"],
            inAmount=data["inAmount"],
            outAmount=data["outAmount"],
            feeAmount=data.get("feeAmount"),
            feeMint=data.get("feeMint"),
        )


@dataclass
class JupiterRoutePlan:
    """Plano de rota para um swap"""

    swapInfo: JupiterSwapInfo
    percent: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterRoutePlan":
        """Cria uma instância JupiterRoutePlan a partir de um dicionário"""
        return cls(
            swapInfo=JupiterSwapInfo.from_dict(data["swapInfo"]),
            percent=data["percent"],
        )


@dataclass
class JupiterQuoteResponse:
    """Resposta da API de quote da Jupiter"""

    inputMint: str
    inAmount: str
    outputMint: str
    outAmount: str
    otherAmountThreshold: str
    swapMode: str
    slippageBps: int
    platformFee: Optional[Dict[str, Any]]
    priceImpactPct: str
    routePlan: List[JupiterRoutePlan]
    contextSlot: Optional[int]
    timeTaken: Optional[float]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JupiterQuoteResponse":
        """Cria uma instância JupiterQuoteResponse a partir de um dicionário"""
        return cls(
            inputMint=data["inputMint"],
            inAmount=data["inAmount"],
            outputMint=data["outputMint"],
            outAmount=data["outAmount"],
            otherAmountThreshold=data["otherAmountThreshold"],
            swapMode=data["swapMode"],
            slippageBps=data["slippageBps"],
            platformFee=data.get("platformFee"),
            priceImpactPct=data["priceImpactPct"],
            routePlan=[JupiterRoutePlan.from_dict(rp) for rp in data["routePlan"]],
            contextSlot=data.get("contextSlot"),
            timeTaken=data.get("timeTaken"),
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
