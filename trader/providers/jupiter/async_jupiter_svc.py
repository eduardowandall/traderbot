"""Swaps pela Jupiter, independentes de onde são executados.

`AsyncJupiterProvider` faz o que vale para todo local de execução:
- converte quantidades UI -> raw (`buy`/`sell`);
- pede a quote e aplica o teto de impacto de preço;
- re-tenta falhas anteriores ao envio, escalando o slippage até um teto;
- busca e enriquece os custos depois da execução (`fetch_swap_costs`).

Quem executa é o `Executor` (`executor.py`): on-chain (com chave) ou simulado
(`trader.paper`). Crie com `AsyncJupiterProvider.on_chain(keypair, ...)` ou
`trader.paper.paper_provider(wallet, ...)`.
"""

import asyncio
import logging
from dataclasses import replace
from decimal import Decimal

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.models import SOLANA_MINTS, SwapResult
from trader.models.account_data import MintBalance
from trader.models.costs import QUOTE, TradeCosts

# reexportados: os erros moram em models (camada core)
from trader.models.errors import SwapRejectedError as SwapRejectedError
from trader.models.errors import (
    TransactionSubmittedError as TransactionSubmittedError,
)
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.providers.jupiter.executor import Executor, OnChainExecutor
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.providers.jupiter.swap_costs import quote_info

# tempo máximo para buscar os custos de um swap já confirmado
COSTS_TIMEOUT_SECONDS = 20

DEFAULT_MAX_PRICE_IMPACT_PCT = Decimal("1")
DEFAULT_MAX_SLIPPAGE_BPS = 100
SLIPPAGE_RETRY_STEP_BPS = 25


class AsyncJupiterProvider[E: Executor]:
    """Genérico no executor: `AsyncJupiterProvider[OnChainExecutor]` etc."""

    def __init__(
        self,
        executor: E,
        # AsyncJupiterClient, ou um substituto com a mesma interface (replay)
        jupiter_client=None,
        max_price_impact_pct: Decimal | None = DEFAULT_MAX_PRICE_IMPACT_PCT,
        max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
    ):
        self.executor = executor
        # None desativa a checagem de impacto de preço
        self.max_price_impact_pct = max_price_impact_pct
        # teto para o aumento automático de slippage nas re-tentativas
        self.max_slippage_bps = max_slippage_bps
        self.jupiter_client = jupiter_client or AsyncJupiterClient()
        self.logger = logging.getLogger(self.__module__)

    @classmethod
    def on_chain(
        cls,
        keypair: Keypair,
        rpc_client: AsyncRPCClient | None = None,
        jupiter_client: AsyncJupiterClient | None = None,
        is_dryrun: bool = False,
        **limits,
    ) -> AsyncJupiterProvider[OnChainExecutor]:
        """Provider que assina e envia (ou só simula, em dry run) com a chave."""
        client = jupiter_client or AsyncJupiterClient()
        executor = OnChainExecutor(keypair, rpc_client, client, is_dryrun)
        return AsyncJupiterProvider(executor, client, **limits)

    @property
    def balances_track_fills(self) -> bool:
        return self.executor.balances_track_fills

    @property
    def native_fee_reserve(self) -> Decimal:
        return self.executor.native_fee_reserve

    def __repr__(self):
        return f"{self.__class__.__name__} via {self.executor!r}"

    async def get_account_balance(self) -> list[MintBalance]:
        return await self.executor.balances()

    async def buy(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        type_order: str,
        quantity: Decimal,
        price: Decimal | None = None,
        slippage_bps: int = 50,
    ) -> SwapResult:
        amount_in = quantity * price if price else quantity
        raw_quantity = SOLANA_MINTS[input_mint].ui_to_raw(amount_in)
        return await self.swap_with_details(
            str(input_mint),
            str(output_mint),
            raw_quantity,
            slippage_bps=slippage_bps,
        )

    async def sell(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        type_order: str,
        quantity: Decimal,
        slippage_bps: int = 50,
    ) -> SwapResult:
        # venda gasta o output_mint: a conversão usa os decimais dele
        raw_quantity = SOLANA_MINTS[output_mint].ui_to_raw(quantity)
        # venda inverte os mints
        return await self.swap_with_details(
            str(output_mint),
            str(input_mint),
            raw_quantity,
            slippage_bps=slippage_bps,
        )

    async def swap(
        self,
        input_mint: str,
        output_mint: str,
        raw_quantity: int,
        slippage_bps: int = 50,
    ) -> str:
        result = await self.swap_with_details(
            input_mint, output_mint, raw_quantity, slippage_bps=slippage_bps
        )
        return result.signature

    async def swap_with_details(
        self,
        input_mint: str,
        output_mint: str,
        raw_quantity: int,
        slippage_bps: int = 50,
    ) -> SwapResult:
        return await self._do_swap_with_retry(
            input_mint,
            output_mint,
            raw_quantity,
            slippage_bps=slippage_bps,
        )

    def _retry_slippages(self, slippage_bps: int) -> list[int]:
        # a escalada nunca passa do teto (nem reduz um slippage pedido acima dele)
        ceiling = max(slippage_bps, self.max_slippage_bps)
        escalated = min(slippage_bps + SLIPPAGE_RETRY_STEP_BPS, ceiling)
        return [slippage_bps, slippage_bps, escalated]

    async def _do_swap_with_retry(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
    ) -> SwapResult:
        last_error: Exception | None = None
        slippages = self._retry_slippages(slippage_bps)
        for i in range(3):
            try:
                return await self._do_swap(
                    input_mint, output_mint, amount_in, slippages[i]
                )
            except TransactionSubmittedError, SwapRejectedError:
                raise
            except Exception as e:
                last_error = e
                if i == 2:
                    break
                self.logger.warning(
                    f"Erro ao executar swap: {e}. Tentando novamente..."
                )
        raise RuntimeError(
            f"Erro ao executar swap após múltiplas tentativas: {last_error}"
        ) from last_error

    async def _get_quote_with_route(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
    ) -> JupiterQuoteResponse:
        quote = await self.jupiter_client.get_quote(
            input_mint, output_mint, amount_in, slippage_bps
        )

        if not quote.routePlan:
            raise Exception("Nenhuma rota encontrada!")

        self._check_price_impact(quote)
        return quote

    def _check_price_impact(self, quote: JupiterQuoteResponse) -> None:
        if self.max_price_impact_pct is None:
            return
        # `priceImpactPct` da Jupiter é uma fração (0.01 = 1%), confirmado
        # comparando com o campo `priceImpact` (percentual) da API v2 num
        # mesmo quote: os dois batem multiplicados por 100. `max_price_impact_pct`
        # é expresso em percentual (API pública/CLI), daí a conversão abaixo.
        impact_pct = abs(Decimal(quote.priceImpactPct or "0")) * 100
        if impact_pct > self.max_price_impact_pct:
            raise SwapRejectedError(
                f"Impacto de preço {impact_pct}% acima do limite "
                f"{self.max_price_impact_pct}%"
            )

    async def _do_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
    ) -> SwapResult:
        quote = await self._get_quote_with_route(
            input_mint, output_mint, amount_in, slippage_bps
        )
        return await self.executor.execute(input_mint, output_mint, quote)

    async def fetch_swap_costs(self, result: SwapResult) -> TradeCosts:
        """Custos reais do swap. **Nunca** levanta exceção.

        O swap já foi executado: qualquer falha aqui só pode degradar para
        custos desconhecidos (source="quote"), nunca re-tentar ou marcar a
        intenção como falha.
        """
        costs = result.costs
        if costs is None:
            try:
                async with asyncio.timeout(COSTS_TIMEOUT_SECONDS):
                    costs = await self.executor.fetch_costs(result)
            except Exception as ex:
                self.logger.warning(f"Custos de {result.signature} indisponíveis: {ex}")
        return _with_quote_info(costs or TradeCosts(source=QUOTE), result)

    async def aclose(self) -> None:
        """Fecha as conexões HTTP/WebSocket/RPC abertas."""
        for client in (self.jupiter_client, self.executor):
            try:
                await client.aclose()
            except Exception as ex:
                self.logger.warning(f"Erro ao fechar {client!r}: {ex}")


def _with_quote_info(costs: TradeCosts, result: SwapResult) -> TradeCosts:
    """Acrescenta os informativos da quote (LP fees, impacto, saída cotada)."""
    quoted_out = int(result.quote.outAmount) if result.quote else result.out_amount
    return replace(costs, quoted_out_amount=quoted_out, **quote_info(result.quote))
