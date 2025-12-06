"""
Interfaces base para APIs de trading.
Define contratos comuns que devem ser implementados por todas as APIs (Mercado Bitcoin, Jupiter, etc).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List

from ..models.account_data import AccountBalanceData, AccountData
from ..models.public_data import Candles, TickerData


class PublicAPIBase(ABC):
    """
    Interface base para APIs públicas (sem autenticação).
    Todas as exchanges/DEXes devem implementar esta interface.
    """

    @abstractmethod
    def get_ticker(self, symbol: str) -> TickerData:
        """
        Obtém ticker de um par específico.

        Args:
            symbol: Símbolo do par (ex: 'BTC-BRL' para Mercado Bitcoin, 'SOL-USDC' para Jupiter)

        Returns:
            TickerData: Dados do ticker
        """
        ...

    @abstractmethod
    def get_candles(
        self, symbol: str, start_date: datetime, end_date: datetime, resolution: str
    ) -> Candles:
        """
        Obtém candles de um par específico.

        Args:
            symbol: Símbolo do par
            start_date: Data de início
            end_date: Data de fim
            resolution: Resolução do candle (ex: '1m', '15m', '1h', '1d')

        Returns:
            Candles: Dados dos candles
        """
        ...


class PrivateAPIBase(ABC):
    """
    Interface base para APIs privadas (com autenticação).
    Todas as exchanges/DEXes devem implementar esta interface.
    """

    @abstractmethod
    def get_accounts(self) -> List[AccountData]:
        """
        Obtém lista de contas do usuário.

        Returns:
            List[AccountData]: Lista de contas
        """
        ...

    @abstractmethod
    def get_account_balance(self, account_id: str) -> List[AccountBalanceData]:
        """
        Obtém saldo de uma conta específica.

        Args:
            account_id: ID da conta

        Returns:
            List[AccountBalanceData]: Lista de saldos por moeda
        """
        ...

    @abstractmethod
    def place_order(
        self,
        account_id: str,
        symbol: str,
        side: str,
        type_order: str,
        quantity: str,
        price: Decimal,
    ) -> str:
        """
        Executa uma ordem de compra ou venda.

        Args:
            account_id: ID da conta
            symbol: Símbolo do par (ex: 'BTC-BRL')
            side: Lado da ordem ('buy' ou 'sell')
            type_order: Tipo da ordem ('market', 'limit', etc)
            quantity: Quantidade a negociar

        Returns:
            str: ID da ordem executada
        """
        ...

    @abstractmethod
    def get_orders(
        self, symbol: str | None = None, status: str | None = None
    ) -> Dict[str, Any]:
        """
        Lista ordens do usuário.

        Args:
            symbol: Filtrar por símbolo (opcional)
            status: Filtrar por status (opcional)

        Returns:
            Dict contendo as ordens
        """
        ...
