"""
Adaptadores para a API Jupiter que implementam as interfaces base.
Permite usar Jupiter com a mesma interface do Mercado Bitcoin.
"""

import base64
import logging
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List

import requests
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.solders import LiteSVM, SendTransactionResp
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token.instructions import (
    TOKEN_2022_PROGRAM_ID,  # type: ignore
    TOKEN_PROGRAM_ID,  # type: ignore
)

from ...models.account_data import AccountBalanceData, AccountData
from ...models.public_data import Candles, TickerData
from ..base_api import PrivateAPIBase, PublicAPIBase
from .jupiter_public_api import (
    SOLANA_TOKENS,
    SOLANA_TOKENS_BY_MINT,
    SOLANA_TOKENS_DECIMALS,
    JupiterPublicAPI,
)


class JupiterPublicAPIAdapter(PublicAPIBase):
    """
    Adaptador da API pública Jupiter para a interface base.
    Converte chamadas da interface padrão para chamadas específicas da Jupiter.
    """

    def __init__(self, use_pro: bool = False):
        self.jupiter_api = JupiterPublicAPI(use_pro=use_pro)
        self.logger = logging.getLogger(__name__)

        # Cache de tokens para conversão de símbolos
        self._token_cache: dict[str, tuple[str, str]] = {}
        self._load_token_cache()

    def _load_token_cache(self):
        """Carrega cache de tokens comuns"""
        # Tokens pré-definidos
        self._token_cache = {
            "SOL-USDC": (SOLANA_TOKENS["SOL"], SOLANA_TOKENS["USDC"]),
            "SOL-USDT": (SOLANA_TOKENS["SOL"], SOLANA_TOKENS["USDT"]),
            "BONK-USDC": (SOLANA_TOKENS["BONK"], SOLANA_TOKENS["USDC"]),
            "JUP-USDC": (SOLANA_TOKENS["JUP"], SOLANA_TOKENS["USDC"]),
            "PUMP-USDC": (SOLANA_TOKENS["PUMP"], SOLANA_TOKENS["USDC"]),
            "TURBO-USDC": (SOLANA_TOKENS["TURBO"], SOLANA_TOKENS["USDC"]),
        }

    def _parse_symbol(self, symbol: str) -> tuple[str, str]:
        """
        Converte símbolo (ex: 'SOL-USDC') para mints (input_mint, output_mint).

        Args:
            symbol: Símbolo no formato 'TOKEN1-TOKEN2'

        Returns:
            Tuple com (input_mint, output_mint)
        """
        if symbol in self._token_cache:
            return self._token_cache[symbol]

        # Tenta parsear o símbolo
        parts = symbol.split("-")
        if len(parts) != 2:
            raise ValueError(f"Símbolo inválido: {symbol}. Use formato 'TOKEN1-TOKEN2'")

        token1, token2 = parts

        # Busca nos tokens conhecidos
        input_mint = SOLANA_TOKENS.get(token1)
        output_mint = SOLANA_TOKENS.get(token2)

        if not input_mint or not output_mint:
            raise ValueError(
                f"Token não encontrado: {token1 if not input_mint else token2}. "
                f"Tokens disponíveis: {list(SOLANA_TOKENS.keys())}"
            )

        self._token_cache[symbol] = (input_mint, output_mint)
        return input_mint, output_mint

    def get_ticker(self, symbol: str) -> TickerData:
        """
        Obtém ticker de um par específico usando Jupiter.

        Args:
            symbol: Símbolo do par (ex: 'SOL-USDC')

        Returns:
            TickerData: Dados do ticker
        """
        input_mint, output_mint = self._parse_symbol(symbol)
        token1, _ = symbol.split("-")

        # Obtém quote para 1 unidade do token de entrada
        # Para SOL: 1 SOL = 1_000_000 lamports
        amount = 10 ** SOLANA_TOKENS_DECIMALS[token1]

        try:
            buy_quote = self.jupiter_api.get_quote(
                input_mint=input_mint,
                output_mint=output_mint,
                amount=amount,
                slippage_bps=50,
            )

            # Calcula o preço: output_amount / input_amount
            buy_price = (
                Decimal(buy_quote.in_amount) / Decimal(buy_quote.out_amount) / 10
            )

            quote_sell = self.jupiter_api.get_quote(
                input_mint=output_mint,
                output_mint=input_mint,
                amount=amount,
                slippage_bps=50,
            )
            sell_price = (
                Decimal(quote_sell.out_amount) / Decimal(quote_sell.in_amount) / 10
            )

            spread = (sell_price - buy_price) / buy_price * 100

            # Cria TickerData com os dados disponíveis
            # Nota: Jupiter não fornece OHLC, então usamos o preço atual para todos
            return TickerData(
                buy=buy_price,
                timestamp=datetime.now(),
                high=buy_price,  # Não disponível, usa preço atual
                last=buy_price,
                low=buy_price,  # Não disponível, usa preço atual
                open=buy_price,  # Não disponível, usa preço atual
                pair=symbol,
                sell=sell_price,
                vol=Decimal("0"),  # Não disponível via quote API
                spread=spread,
            )

        except Exception as e:
            self.logger.error(f"Erro ao obter ticker para {symbol}: {e}")
            raise

    def get_candles(
        self, symbol: str, start_date: datetime, end_date: datetime, resolution: str
    ) -> Candles:
        """
        Obtém candles de um par específico.

        NOTA: Jupiter não fornece dados históricos de candles diretamente.
        Esta implementação retorna candles vazios.
        Para dados históricos, use um provedor de dados como Birdeye ou DexScreener.

        Args:
            symbol: Símbolo do par
            start_date: Data de início
            end_date: Data de fim
            resolution: Resolução do candle

        Returns:
            Candles: Candles vazios (Jupiter não fornece histórico)
        """
        self.logger.warning(
            "Jupiter não fornece dados históricos de candles. "
            "Retornando candles vazios. "
            "Use Birdeye API ou DexScreener para dados históricos."
        )

        return Candles(
            close=[],
            high=[],
            low=[],
            open=[],
            timestamp=[],
            volume=[],
        )


class JupiterPrivateAPI(PrivateAPIBase):
    """
    Adaptador da API privada Jupiter para a interface base.

    NOTA: Jupiter funciona de forma diferente do Mercado Bitcoin:
    - Não há conceito de "contas" centralizadas
    - Usa wallets Solana descentralizadas
    - Não há histórico de ordens centralizado

    Este adaptador simula a interface para compatibilidade,
    mas muitas funcionalidades são limitadas ou não aplicáveis.
    """

    def __init__(
        self,
        wallet_public_key: str,
    ):
        """
        Inicializa o adaptador Jupiter privado.

        Args:
            wallet_public_key: Chave pública da wallet Solana
            rpc_url: URL do RPC endpoint da Solana
        """
        self.wallet_public_key = wallet_public_key
        self.wallet = Pubkey.from_string(wallet_public_key)
        self.rpc_url = os.getenv("HELIUS_RPC_URL")
        assert self.rpc_url, "RPC URL não definida"
        self.client = Client(self.rpc_url)
        self.logger = logging.getLogger(__name__)
        private_key = os.getenv("SOLANA_PRIVATE_KEY")
        assert private_key, "Chave privada não definida"
        self.keypair = Keypair.from_base58_string(private_key)

        # Simula uma conta única (wallets Solana não têm múltiplas contas)
        self._account_id = "solana_wallet"

    def get_accounts(self) -> List[AccountData]:
        """
        Retorna uma "conta" simulada representando a wallet Solana.

        Returns:
            List[AccountData]: Lista com uma conta simulada
        """
        resp = self.client.get_account_info(self.wallet)
        if resp.value:
            lamports = resp.value.lamports
            sol = lamports / 1_000_000

            if sol > 0:
                return [
                    AccountData(
                        id=self.wallet_public_key,
                        currency="USDC",
                        currencySign="◎",
                        name="Solana Wallet",
                        type="wallet",
                    )
                ]
        return []

    def get_account_balance(self, account_id: str) -> List[AccountBalanceData]:
        """
        Obtém saldo da wallet Solana.

        NOTA: Requer implementação de RPC calls para Solana.
        Por enquanto, retorna lista vazia.

        Args:
            account_id: ID da conta (ignorado, usa wallet_public_key)

        Returns:
            List[AccountBalanceData]: Lista de saldos
        """
        balances = []

        # ============================
        # 1 - Saldo de SOL (lamports)
        # ============================
        resp = self.client.get_account_info(self.wallet)
        if resp.value:
            lamports = resp.value.lamports
            sol = lamports / 1_000_000

            if sol > 0:
                balances.append(
                    AccountBalanceData(
                        available=Decimal(str(sol)),
                        on_hold=Decimal("0"),
                        symbol="SOL",
                        total=Decimal(str(sol)),
                    )
                )

        # ====================================================
        # 2 - Listar todas as contas SPL pertencentes à wallet
        # ====================================================
        for token in [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]:
            token_accounts = self.client.get_token_accounts_by_owner(
                self.wallet, TokenAccountOpts(program_id=token)
            )

            for token_acc in token_accounts.value:
                info = token_acc.account.data  # base64 data
                decoded = bytes(info)

                # Estrutura da SPL Token Account (primeiros 8 bytes = quantidade)
                amount = int.from_bytes(decoded[64:72], "little")

                # Mint (posição fixa)
                mint = Pubkey(decoded[0:32])
                symbol = SOLANA_TOKENS_BY_MINT.get(str(mint))
                if not symbol:
                    continue

                # Para converter o token corretamente, buscamos dados do mint
                mint_info = self.client.get_account_info(mint)

                if not mint_info.value:
                    continue

                mint_raw = bytes(mint_info.value.data)
                decimals = mint_raw[44]
                real_amount = amount / (10**decimals)
                if real_amount > 0:
                    balances.append(
                        AccountBalanceData(
                            available=real_amount,
                            on_hold=Decimal("0"),
                            symbol=symbol,
                            total=real_amount,
                        )
                    )

        return balances

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
        Executa um swap na Jupiter.

        1. Obter quote
        2. Obter transação serializada
        3. Assinar com private key
        4. Enviar para blockchain

        Args:
            account_id: ID da conta (ignorado)
            symbol: Símbolo do par (ex: 'SOL-USDC')
            side: 'buy' ou 'sell'
            type_order: Tipo da ordem (apenas 'market' suportado)
            quantity: Quantidade a negociar

        Returns:
            str: Transaction signature (simulado)
        """
        mint_in = SOLANA_TOKENS[symbol.split("-")[0]]
        mint_out = SOLANA_TOKENS[symbol.split("-")[1]]

        if side == "buy":
            mint_in, mint_out = mint_out, mint_in
            decimals = SOLANA_TOKENS_DECIMALS[symbol.split("-")[1]]
            amount_in = int(Decimal(quantity) * price * (10**decimals))
        else:
            decimals = SOLANA_TOKENS_DECIMALS[symbol.split("-")[0]]
            amount_in = int(Decimal(quantity) * (10**decimals))
        return self._do_swap_with_retry(mint_in, mint_out, amount_in)

    def _do_swap_with_retry(self, mint_in: str, mint_out: str, amount_in: int):
        for i in range(3):
            try:
                return self._do_swap(mint_in, mint_out, amount_in, [50, 50, 75][i])
            except Exception as e:
                if i == 2:
                    raise e
                self.logger.warning(
                    f"Erro ao executar swap: {e}. Tentando novamente..."
                )
        raise Exception("Erro ao executar swap após múltiplas tentativas")

    def _wait_for_confirmation(self, signature, timeout=30):
        print("→ Aguardando confirmação...")
        start = time.time()

        while True:
            result = self.client.get_signature_statuses([signature])
            status = result.value[0]

            if status is not None:
                # Se a transação foi processada
                if status.confirmation_status in [
                    TransactionConfirmationStatus.Confirmed,
                    TransactionConfirmationStatus.Finalized,
                ]:
                    return True
                if status.err is not None:
                    raise Exception(f"Transação falhou: {status.err}")

            # Timeout
            if time.time() - start > timeout:
                raise TimeoutError("Transação não foi confirmada a tempo.")

            time.sleep(1)

    def _get_quote_with_route(
        self,
        mint_in: str,
        mint_out: str,
        amount_in: int,
        slippage_bps: int = 50,
    ):
        print("→ Criando rota na Jupiter...")
        quote = requests.get(
            "https://lite-api.jup.ag/swap/v1/quote",
            params={
                "inputMint": mint_in,
                "outputMint": mint_out,
                "amount": amount_in,
                "slippageBps": slippage_bps,
            },
        ).json()

        if not quote.get("routePlan"):
            raise Exception("Nenhuma rota encontrada!")

        print("✓ Rota encontrada.")
        return quote

    def _get_swap_transaction(self, quote: Dict[str, Any]) -> VersionedTransaction:
        print("→ Gerando transação de swap...")
        swap_tx = requests.post(
            "https://lite-api.jup.ag/swap/v1/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": self.wallet_public_key,
            },
        ).json()
        raw_tx = base64.b64decode(swap_tx["swapTransaction"])

        # ---------- desserializar ----------
        tx = VersionedTransaction.from_bytes(raw_tx)
        return tx

    def _get_signed_transaction(self, tx: VersionedTransaction) -> VersionedTransaction:
        # ---------- assinar ----------
        print("→ Assinando transação...")
        latest = self.client.get_latest_blockhash()
        blockhash = latest.value.blockhash
        message = tx.message
        message = MessageV0(
            header=tx.message.header,
            account_keys=tx.message.account_keys,
            recent_blockhash=blockhash,
            instructions=tx.message.instructions,
            address_table_lookups=tx.message.address_table_lookups,  # type: ignore
        )

        new_tx = VersionedTransaction(
            message=message,
            keypairs=[self.keypair],
        )

        signature = self.keypair.sign_message(to_bytes_versioned(message))

        new_tx.signatures = [signature]
        return new_tx

    def _send_signed_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        # ---------- enviar ----------
        print("→ Enviando via Helius RPC...")
        simulation = self.client.simulate_transaction(new_tx)
        if simulation.value.err:
            raise Exception(f"Erro ao simular transação: {simulation.value}")
        resp = self.client.send_raw_transaction(bytes(new_tx))
        signature = resp.value

        print(f"✓ Transação enviada: {signature}")
        return resp

    def _do_swap(
        self,
        mint_in: str,
        mint_out: str,
        amount_in: int,
        slippage_bps: int = 50,
    ):
        quote = self._get_quote_with_route(mint_in, mint_out, amount_in, slippage_bps)
        tx = self._get_swap_transaction(quote)
        new_tx = self._get_signed_transaction(tx)
        resp = self._send_signed_transaction(new_tx)

        signature = resp.value
        self._wait_for_confirmation(signature)
        return resp.to_json()
        # return "FAKE_SIGNATURE"

    def get_orders(
        self, symbol: str | None = None, status: str | None = None
    ) -> Dict[str, Any]:
        """
        Lista ordens.

        NOTA: Jupiter/Solana não mantém histórico centralizado de ordens.
        Para histórico, consulte a blockchain diretamente ou use um indexador.

        Args:
            symbol: Filtrar por símbolo (ignorado)
            status: Filtrar por status (ignorado)

        Returns:
            Dict vazio
        """
        self.logger.warning(
            "get_orders não aplicável para Jupiter. "
            "Consulte a blockchain Solana diretamente para histórico de transações."
        )

        return {"orders": []}


class FakeJupiterPrivateAPI(JupiterPrivateAPI):
    """
    Versão fake da API privada Jupiter para testes e backtesting.
    Simula operações sem executar transações reais.
    """

    def __init__(
        self,
    ):
        self.logger = logging.getLogger(__name__)
        self.keypair = Keypair()
        self.wallet = self.keypair.pubkey()
        self.wallet_public_key = str(self.wallet)
        self.client = LiteSVM()

        self._account_id = "solana_wallet"
        self._balances: Dict[str, Decimal] = {
            "SOL": Decimal("10.0"),
            "USDC": Decimal("100.0"),
            "BONK": Decimal("1000"),
        }

    def get_accounts(self) -> List[AccountData]:
        return [
            AccountData(
                id=self.wallet_public_key,
                currency="USDC",
                currencySign="◎",
                name="Solana Wallet",
                type="wallet",
            )
        ]

    def get_account_balance(self, account_id: str) -> List[AccountBalanceData]:
        """Retorna saldos simulados"""
        balances = []
        for symbol, amount in self._balances.items():
            balances.append(
                AccountBalanceData(
                    symbol=symbol,
                    available=amount,
                    on_hold=Decimal("0"),
                    total=amount,
                )
            )
        return balances

    def place_order(
        self,
        account_id: str,
        symbol: str,
        side: str,
        type_order: str,
        quantity: str,
        price: Decimal,
    ) -> str:
        """Simula execução de ordem"""
        import uuid

        order_id = str(uuid.uuid4())
        self.logger.info(
            f"[FAKE] Order placed: {side} {quantity} {symbol} - ID: {order_id}"
        )

        # TODO: utilizar LiteSVM para simular mudanças reais

        return order_id

    def get_orders(
        self, symbol: str | None = None, status: str | None = None
    ) -> Dict[str, Any]:
        return {"orders": []}
