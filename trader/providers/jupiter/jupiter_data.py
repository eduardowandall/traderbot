"""
Dataclasses para dados da API Jupiter (Solana DEX Aggregator).
"""

from dataclasses import dataclass
from typing import Any


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
    def from_dict(cls, data: dict[str, Any]) -> JupiterSwapInfo:
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
    def from_dict(cls, data: dict[str, Any]) -> JupiterRoutePlan:
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
    platformFee: dict[str, Any] | None
    priceImpactPct: str
    routePlan: list[JupiterRoutePlan]
    contextSlot: int | None
    timeTaken: float | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JupiterQuoteResponse:
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
