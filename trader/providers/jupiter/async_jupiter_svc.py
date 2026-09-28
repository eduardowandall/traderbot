import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.solders import SendTransactionResp
from solders.transaction import VersionedTransaction

from trader.models import SOLANA_MINTS, SwapResult, TickerData
from trader.models.account_data import MintBalance
from trader.models.costs import BASE_FEE_LAMPORTS, ESTIMATED, QUOTE, TradeCosts
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient, Interval
from trader.providers.jupiter.async_rpc_client import (
    AsyncRPCClient,
    TransactionFailedError,
)
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.providers.jupiter.swap_costs import SwapLegs, parse_swap_costs, quote_info


class TransactionSubmittedError(Exception):
    """Falha depois que a transação já foi enviada à rede.

    Não deve ser re-tentada automaticamente: a transação pode ter sido
    executada, e um novo envio poderia duplicar o swap.
    """

    def __init__(self, message: str, signature: str | None = None):
        super().__init__(message)
        # guardada no ledger: permite conferir a transação e cobrar a taxa
        # mesmo se ela falhou
        self.signature = signature


# tempo máximo para buscar os custos de um swap já confirmado
COSTS_TIMEOUT_SECONDS = 20


class SwapRejectedError(Exception):
    """Swap recusado antes do envio por uma regra de segurança.

    Não é re-tentado: a mesma quote seria recusada novamente.
    """


DEFAULT_MAX_PRICE_IMPACT_PCT = Decimal("1")
DEFAULT_MAX_SLIPPAGE_BPS = 100
SLIPPAGE_RETRY_STEP_BPS = 25


class AsyncJupiterProvider:
    def __init__(
        self,
        keypair: Keypair | None,
        rpc_client=None,
        jupiter_client=None,
        is_dryrun=False,
        max_price_impact_pct: Decimal | None = DEFAULT_MAX_PRICE_IMPACT_PCT,
        max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
    ):
        self.keypair = keypair
        # None desativa a checagem de impacto de preço
        self.max_price_impact_pct = max_price_impact_pct
        # teto para o aumento automático de slippage nas re-tentativas
        self.max_slippage_bps = max_slippage_bps

        self.is_dryrun = is_dryrun
        self.rpc_client = rpc_client or AsyncRPCClient(is_dryrun=is_dryrun)
        self.jupiter_client = jupiter_client or AsyncJupiterClient()
        self.logger = logging.getLogger(self.__module__)
        self.logger.info(f"Starting bot on {is_dryrun=}")

    @property
    def pubkey(self) -> Pubkey:
        if self.keypair is None:
            raise RuntimeError("Provider sem chave privada (paper trading?)")
        return self.keypair.pubkey()

    def __repr__(self):
        return f"{self.__class__.__name__}.{self.pubkey} with rpc {str(self.rpc_client)} and client {str(self.jupiter_client)}"

    async def get_candles(
        self,
        mint: Pubkey,
        interval: Interval = Interval.SECOND_15,
        candle_qty: int = 100,
    ) -> list[TickerData]:
        candles_json = await self.jupiter_client.get_candles(
            str(mint), interval=interval, candle_qty=candle_qty
        )
        tickers: list[TickerData] = []
        for candle in candles_json:
            tickers.append(
                TickerData(
                    pair="ignored",
                    timestamp=datetime.fromtimestamp(candle["time"]),
                    high=Decimal(candle["high"]),
                    low=Decimal(candle["low"]),
                    open=Decimal(candle["open"]),
                    last=Decimal(candle["close"]),
                    buy=Decimal(candle["open"]),
                    sell=Decimal(candle["open"]),
                    vol=Decimal(candle["volume"]),
                )
            )
        return tickers

    async def get_price_ticker_data(self, mint: Pubkey) -> Decimal:
        price = await self.jupiter_client.get_price(str(mint))
        return price

    async def get_account_balance(self) -> list[MintBalance]:
        balances = []

        # Saldo de SOL (lamports)
        amount = await self.rpc_client.get_lamports(self.pubkey)
        solana_mint = SOLANA_MINTS.get_by_symbol("SOL")
        balances.append(
            MintBalance(
                available=solana_mint.raw_to_ui(amount),
                mint=solana_mint.pubkey,
            )
        )

        mint_balances = await self.rpc_client.get_account_balance(self.pubkey)
        for mint, amount in mint_balances.items():
            mint_info = SOLANA_MINTS.get(mint)
            if not mint_info:
                continue
            balances.append(
                MintBalance(available=mint_info.raw_to_ui(amount), mint=mint)
            )
        return balances

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

    async def _get_swap_transaction(
        self, quote: JupiterQuoteResponse
    ) -> VersionedTransaction:
        return await self.jupiter_client.get_swap_transaction(quote, self.pubkey)

    async def _get_signed_transaction(
        self, tx: VersionedTransaction
    ) -> VersionedTransaction:
        if self.keypair is None:
            raise RuntimeError("Provider sem chave privada não pode assinar")
        return await self.rpc_client.sign_transaction(tx, self.keypair)

    async def _send_signed_transaction(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        await self.rpc_client.simulate_transaction(new_tx)

        resp = await self.rpc_client.send_transaction(new_tx)

        return resp

    async def _wait_for_confirmation(self, signature, timeout=30):
        start = time.time()

        while True:
            if await self._poll_confirmation(signature):
                return True
            if time.time() - start > timeout:
                raise TimeoutError("Transação não foi confirmada a tempo.")
            await asyncio.sleep(1.0)

    async def _poll_confirmation(self, signature) -> bool:
        """Uma consulta; erros transitórios de RPC contam como "ainda não"."""
        try:
            return await self.rpc_client.check_signature_is_confirmed(signature)
        except TransactionFailedError:
            raise
        except Exception as ex:
            self.logger.warning(f"Erro ao consultar confirmação: {ex}")
            return False

    async def _send_transaction_and_wait_for_confirmation(
        self, new_tx: VersionedTransaction
    ) -> SendTransactionResp:
        resp = await self._send_signed_transaction(new_tx)
        signature = resp.value
        try:
            await self._wait_for_confirmation(signature)
        except Exception as ex:
            raise TransactionSubmittedError(
                f"Transação {signature} enviada mas não confirmada: {ex}",
                signature=str(signature),
            ) from ex
        return resp

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
        tx = await self._get_swap_transaction(quote)
        new_tx = await self._get_signed_transaction(tx)
        resp = await self._send_transaction_and_wait_for_confirmation(new_tx)

        # valores da quote; os efetivos (e os custos) vêm de `fetch_swap_costs`,
        # chamado só depois que o ledger marcou a intenção como executada
        return SwapResult(
            signature=json.loads(resp.to_json())["result"],
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(quote.inAmount),
            out_amount=int(quote.outAmount),
            quote=quote,
            message=new_tx.message,
        )

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
                    costs = await self._read_costs(result)
            except Exception as ex:
                self.logger.warning(f"Custos de {result.signature} indisponíveis: {ex}")
        return _with_quote_info(costs or TradeCosts(source=QUOTE), result)

    async def _read_costs(self, result: SwapResult) -> TradeCosts | None:
        if self.is_dryrun:
            return await self._estimated_costs(result)
        tx = await self.rpc_client.get_confirmed_transaction(result.signature)
        if tx is None:
            return None
        ui_tx = tx.transaction
        return parse_swap_costs(
            tx.meta,
            list(getattr(getattr(ui_tx, "message", None), "account_keys", [])),
            len(getattr(ui_tx, "signatures", [])),
            self.pubkey,
            SwapLegs(
                result.input_mint,
                result.output_mint,
                result.in_amount,
                result.out_amount,
            ),
        )

    async def _estimated_costs(self, result: SwapResult) -> TradeCosts | None:
        """Dry run: a transação não é enviada; a taxa é a que a rede cobraria."""
        if result.message is None:
            return None
        fee = await self.rpc_client.get_fee_for_message(result.message)
        if fee is None:
            return None
        signatures = result.message.header.num_required_signatures
        return TradeCosts(
            source=ESTIMATED,
            fee_lamports=fee,
            priority_fee_lamports=max(fee - BASE_FEE_LAMPORTS * signatures, 0),
            actual_in_amount=result.in_amount,
            actual_out_amount=result.out_amount,
        )

    async def aclose(self) -> None:
        """Fecha as conexões HTTP/WebSocket/RPC abertas."""
        for client in (self.jupiter_client, self.rpc_client):
            try:
                await client.aclose()
            except Exception as ex:
                self.logger.warning(f"Erro ao fechar {client!r}: {ex}")


def _with_quote_info(costs: TradeCosts, result: SwapResult) -> TradeCosts:
    """Acrescenta os informativos da quote (LP fees, impacto, saída cotada)."""
    quoted_out = int(result.quote.outAmount) if result.quote else result.out_amount
    return replace(costs, quoted_out_amount=quoted_out, **quote_info(result.quote))
