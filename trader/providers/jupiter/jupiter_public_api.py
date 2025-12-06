"""
Interface pública da API Jupiter (Solana DEX Aggregator).
Esta interface não requer autenticação e pode ser usada para obter quotes e preços.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from .jupiter_data import (
    JupiterPriceData,
    JupiterQuoteResponse,
    JupiterTokenInfo,
)


class JupiterPublicAPI:
    """
    Interface pública da API Jupiter.
    Não requer autenticação - ideal para obter quotes e informações de tokens.
    """

    def __init__(self, use_pro: bool = False):
        """
        Inicializa a API pública da Jupiter.

        Args:
            use_pro: Se True, usa a API Pro (requer API key). Se False, usa a API Lite.
        """
        # URLs base para diferentes versões da API
        if use_pro:
            self.quote_base_url = "https://quote-api.jup.ag/v6"
            self.price_base_url = "https://api.jup.ag"
        else:
            self.quote_base_url = "https://lite-api.jup.ag/swap/v1"
            self.price_base_url = "https://api.jup.ag"

        self.session = requests.Session()
        self.logger = logging.getLogger(__name__)

        # Headers padrão para requisições públicas
        self.session.headers.update(
            {
                "Content-Type": "application/json",
            }
        )

    def _make_public_request(
        self,
        base_url: str,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Faz requisição pública para a API (sem autenticação)"""
        url = f"{base_url}{endpoint}"

        response = self.session.request(method, url, params=params)

        if response.status_code != 200:
            raise Exception(
                f"Erro na requisição {url}: status={response.status_code} response={response.text}"
            )

        return response.json()

    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
        only_direct_routes: bool = False,
        max_accounts: Optional[int] = None,
    ) -> JupiterQuoteResponse:
        """
        Obtém uma cotação para swap de tokens.

        Args:
            input_mint: Endereço do token de entrada (ex: SOL = 'So11111111111111111111111111111111111111112')
            output_mint: Endereço do token de saída (ex: USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v')
            amount: Quantidade em unidades atômicas (lamports para SOL)
            slippage_bps: Slippage em basis points (50 = 0.5%)
            only_direct_routes: Se True, usa apenas rotas diretas
            max_accounts: Número máximo de contas na transação

        Returns:
            JupiterQuoteResponse: Dados da cotação
        """
        params: Dict[str, Any] = {
            "inputMint": output_mint,
            "outputMint": input_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }

        if only_direct_routes:
            params["onlyDirectRoutes"] = "true"

        if max_accounts is not None:
            params["maxAccounts"] = str(max_accounts)

        response = self._make_public_request(
            self.quote_base_url, "GET", "/quote", params=params
        )

        return JupiterQuoteResponse.from_dict(response)

    def get_tokens(self) -> List[JupiterTokenInfo]:
        """
        Obtém lista de todos os tokens disponíveis na Jupiter.

        Returns:
            List[JupiterTokenInfo]: Lista de tokens
        """
        response = self._make_public_request(
            self.price_base_url, "GET", "/tokens/v1/all"
        )

        return [JupiterTokenInfo.from_dict(token) for token in response]

    def get_token_price(
        self, token_ids: List[str], vs_token: str = "USDC"
    ) -> Dict[str, JupiterPriceData]:
        """
        Obtém preços de tokens.

        Args:
            token_ids: Lista de endereços de tokens
            vs_token: Token de referência para o preço (padrão: USDC)

        Returns:
            Dict[str, JupiterPriceData]: Dicionário com preços (chave = endereço do token)
        """
        params = {
            "ids": ",".join(token_ids),
            "vsToken": vs_token,
        }

        response = self._make_public_request(
            self.price_base_url, "GET", "/price/v2", params=params
        )

        result = {}
        if "data" in response:
            for token_id, price_data in response["data"].items():
                result[token_id] = JupiterPriceData.from_dict(price_data)

        return result

    def get_program_id_to_label(self) -> Dict[str, str]:
        """
        Obtém mapeamento de Program IDs para labels de DEXes.
        Útil para debug e tratamento de erros.

        Returns:
            Dict[str, str]: Dicionário mapeando program_id -> label
        """
        response = self._make_public_request(
            self.quote_base_url, "GET", "/program-id-to-label"
        )

        return response


# Constantes úteis para tokens comuns na Solana
SOLANA_TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "PUMP": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
    "TURBO": "2Dyzu65QA9zdX1UeE7Gx71k7fiwyUK6sZdrvJ7auq5wm",
}

SOLANA_TOKENS_DECIMALS = {
    "SOL": 9,
    "USDC": 6,
    "USDT": 6,
    "BONK": 5,
    "JUP": 6,
    "PUMP": 6,
    "TURBO": 8,
}

SOLANA_TOKENS_BY_MINT = {v: k for k, v in SOLANA_TOKENS.items()}
