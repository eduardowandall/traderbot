"""Swaps pela Jupiter, independentes de onde são executados.

`AsyncJupiterProvider` faz o que vale para todo local de execução:
- converte quantidades UI -> raw (`buy`/`sell`);
- pede a quote e aplica o teto de impacto de preço;
- re-tenta falhas anteriores ao envio, escalando o slippage até um teto;
- busca e enriquece os custos depois da execução (`fetch_swap_costs`).

Quem executa é o `Executor` (`executor.py`): on-chain (com chave) ou simulado
(`trader.execution.trade.venues.paper`). Crie com `AsyncJupiterProvider.on_chain(keypair, ...)` ou
`trader.execution.trade.venues.paper.paper_provider(wallet, ...)`.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from decimal import Decimal

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.execution.models.account_data import MintBalance
from trader.execution.models.errors import (
    SwapAttemptsError,
    SwapFailedError,
    TransactionFailedOnChainError,
)

# reexportados: os erros moram em models (camada core)
from trader.execution.models.errors import SwapRejectedError as SwapRejectedError
from trader.execution.models.errors import (
    TransactionSubmittedError as TransactionSubmittedError,
)
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.trade.venues.jupiter.async_rpc_client import AsyncRPCClient
from trader.execution.trade.venues.jupiter.executor import Executor, OnChainExecutor
from trader.execution.trade.venues.jupiter.swap_costs import quote_info
from trader.shared.models import SOLANA_MINTS, SwapResult
from trader.shared.models.costs import (
    BASE_FEE_LAMPORTS,
    QUOTE,
    TradeCosts,
)

# tempo máximo para buscar os custos de um swap já confirmado
COSTS_TIMEOUT_SECONDS = 20

DEFAULT_MAX_PRICE_IMPACT_PCT = Decimal("1")
DEFAULT_MAX_SLIPPAGE_BPS = 100
# real e paper (wiring): uma quote mais de 2% abaixo da Price API é recusada
DEFAULT_MAX_QUOTE_DEVIATION_PCT = Decimal("2")
SLIPPAGE_RETRY_STEP_BPS = 25
RETRY_DELAYS_SECONDS = (0.5, 1.0)
# depois disso, nenhuma tentativa nova: o sinal e a checagem da política
# ficaram velhos
SWAP_DEADLINE_SECONDS = 60.0


async def _pause_before(attempt: int) -> None:
    """Antes do envio: dá tempo à quote e ao limite de taxa da API."""
    if attempt:
        await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt - 1])


# mints -> USD por unidade (os sem preço ficam de fora)
UsdPrices = Callable[[list[str]], Awaitable[dict[str, Decimal]]]


class AsyncJupiterProvider[E: Executor]:
    """Genérico no executor: `AsyncJupiterProvider[OnChainExecutor]` etc."""

    def __init__(
        self,
        executor: E,
        # AsyncJupiterClient, ou um substituto com a mesma interface (replay)
        jupiter_client=None,
        max_price_impact_pct: Decimal | None = DEFAULT_MAX_PRICE_IMPACT_PCT,
        max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
        # quote no máximo isto (%) abaixo do valor pela Price API; None
        # desliga (replays, testes). `trader/execution/wiring.py` liga para real e paper
        max_quote_deviation_pct: Decimal | None = None,
    ):
        self.executor = executor
        self.max_quote_deviation_pct = max_quote_deviation_pct
        # None desativa a checagem de impacto de preço
        self.max_price_impact_pct = max_price_impact_pct
        # teto para o aumento automático de slippage nas re-tentativas
        self.max_slippage_bps = max_slippage_bps
        self.jupiter_client = jupiter_client or AsyncJupiterClient()
        # preços USD independentes da quote: o oráculo do processo
        # (`wiring.build_trade_service` passa o hub); None, a Price API
        self.usd_prices: UsdPrices | None = None
        self.logger = logging.getLogger(self.__module__)

    @classmethod
    def on_chain(
        cls,
        keypair: Keypair,
        rpc_client: AsyncRPCClient | None = None,
        jupiter_client: AsyncJupiterClient | None = None,
        *,
        max_priority_fee_lamports: int,  # o teto da política
        **limits,
    ) -> AsyncJupiterProvider[OnChainExecutor]:
        """Provider que assina e envia com a chave."""
        client = jupiter_client or AsyncJupiterClient()
        executor = OnChainExecutor(
            keypair, rpc_client, client, max_priority_fee_lamports
        )
        return AsyncJupiterProvider(executor, client, **limits)

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
        spend_amount: Decimal,
        slippage_bps: int = 50,
    ) -> SwapResult:
        """Gasta `spend_amount` (unidades de UI do input_mint) comprando o output."""
        raw_quantity = SOLANA_MINTS[input_mint].ui_to_raw(spend_amount)
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
        quantity: Decimal,
        slippage_bps: int = 50,
    ) -> SwapResult:
        # venda gasta o output_mint: a conversão usa os decimais dele
        raw_quantity = SOLANA_MINTS[output_mint].ui_to_raw(quantity)
        # venda inverte os mints; sem preço independente, a saída segue
        return await self.swap_with_details(
            str(output_mint),
            str(input_mint),
            raw_quantity,
            slippage_bps=slippage_bps,
            fail_closed=False,
        )

    def _retry_slippages(self, slippage_bps: int) -> list[int]:
        # a escalada nunca passa do teto (nem reduz um slippage pedido acima dele)
        ceiling = max(slippage_bps, self.max_slippage_bps)
        escalated = min(slippage_bps + SLIPPAGE_RETRY_STEP_BPS, ceiling)
        return [slippage_bps, slippage_bps, escalated]

    async def swap_with_details(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
        *,
        fail_closed: bool = True,
    ) -> SwapResult:
        """As tentativas, com as transações que falharam na rede anotadas.

        Elas vão no resultado (`failed_signatures`) ou no erro que encerra as
        tentativas: a taxa foi paga e é registrada por quem executa.
        `fail_closed`: sem preço independente para conferir a quote, recusa.
        """
        failed: list[str] = []
        try:
            result = await self._attempts(
                input_mint,
                output_mint,
                amount_in,
                slippage_bps,
                failed,
                fail_closed=fail_closed,
            )
        except SwapAttemptsError as ex:
            ex.failed_signatures = tuple(failed)
            raise
        return replace(result, failed_signatures=tuple(failed)) if failed else result

    async def _attempts(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int,
        failed: list[str],
        *,
        fail_closed: bool,
    ) -> SwapResult:
        last_error: Exception | None = None
        deadline = time.monotonic() + SWAP_DEADLINE_SECONDS
        for attempt, slippage in enumerate(self._retry_slippages(slippage_bps)):
            if attempt and time.monotonic() > deadline:
                # nunca no meio de uma tentativa (um envio interrompido não é
                # seguro): só não começa outra depois do prazo
                break
            await _pause_before(attempt)
            try:
                return await self._do_swap(
                    input_mint,
                    output_mint,
                    amount_in,
                    slippage,
                    fail_closed=fail_closed,
                )
            except TransactionSubmittedError, SwapRejectedError:
                raise
            except Exception as e:
                last_error = e
                self._note_failure(e, attempt, failed)
        raise SwapFailedError(
            f"Erro ao executar swap após múltiplas tentativas: {last_error}"
        ) from last_error

    def _note_failure(self, error: Exception, attempt: int, failed: list[str]) -> None:
        self.logger.warning(f"Erro ao executar swap (tentativa {attempt + 1}): {error}")
        if isinstance(error, TransactionFailedOnChainError) and error.signature:
            failed.append(error.signature)

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
        slippage_bps: int,
        *,
        fail_closed: bool,
    ) -> SwapResult:
        quote = await self._get_quote_with_route(
            input_mint, output_mint, amount_in, slippage_bps
        )
        await self._check_quote_value(quote, fail_closed)
        return await self.executor.execute(input_mint, output_mint, quote)

    async def _check_quote_value(
        self, quote: JupiterQuoteResponse, fail_closed: bool
    ) -> None:
        """A saída vale ao menos (100 - `max_quote_deviation_pct`)% da entrada.

        Os valores vêm da Price API (independente da rota). Sem preço: recusa
        (`fail_closed`, compras) ou segue com um aviso (vendas: uma saída
        nunca fica presa na Price API).
        """
        if self.max_quote_deviation_pct is None:
            return
        loss_pct = await self._quote_loss_pct(quote)
        if loss_pct is None:
            if fail_closed:
                raise SwapRejectedError("sem preço independente para conferir a quote")
            self.logger.warning("Quote não conferida: Price API sem preço")
            return
        if loss_pct > self.max_quote_deviation_pct:
            raise SwapRejectedError(
                f"Quote {loss_pct:.2f}% abaixo do valor pela Price API "
                f"(limite {self.max_quote_deviation_pct}%)"
            )

    async def _quote_loss_pct(self, quote: JupiterQuoteResponse) -> Decimal | None:
        """Quanto (%) a saída vale a menos que a entrada; None sem preços."""
        legs = ((quote.inputMint, quote.inAmount), (quote.outputMint, quote.outAmount))
        try:
            usd_prices = self.usd_prices or self.jupiter_client.get_usd_prices
            prices = await usd_prices([m for m, _ in legs])
            in_usd, out_usd = (
                SOLANA_MINTS.raw_to_ui(mint, int(raw)) * prices[mint]
                for mint, raw in legs
            )
        except Exception as ex:
            self.logger.warning(f"Sem preço independente para a quote: {ex}")
            return None
        return (1 - out_usd / in_usd) * 100 if in_usd > 0 else None

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

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int:
        """Lamports pagos pelas transações que falharam. **Nunca** levanta.

        Uma taxa que não pôde ser lida conta como a taxa base: ao menos ela
        foi paga.
        """
        total = 0
        for signature in signatures:
            fee = await self._failed_fee(signature)
            total += BASE_FEE_LAMPORTS if fee is None else fee
        return total

    async def _failed_fee(self, signature: str) -> int | None:
        try:
            async with asyncio.timeout(COSTS_TIMEOUT_SECONDS):
                return await self.executor.fetch_fee(signature)
        except Exception as ex:
            self.logger.warning(f"Taxa da transação falha {signature}: {ex}")
            return None

    async def send_outcome(self, sent: SentTx) -> TxOutcome:
        """O que aconteceu com um envio gravado. **Nunca** levanta.

        Uma consulta que falha é PENDING: a intenção continua bloqueando e a
        próxima varredura tenta de novo.
        """
        try:
            async with asyncio.timeout(COSTS_TIMEOUT_SECONDS):
                return await self.executor.outcome(sent)
        except Exception as ex:
            self.logger.warning(f"Status da transação {sent.signature}: {ex}")
            return TxOutcome.PENDING

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
