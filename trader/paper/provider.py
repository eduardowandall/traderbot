"""Provider de paper trading: quotes reais da Jupiter, execução simulada.

Reaproveita todo o fluxo do `AsyncJupiterProvider` (conversão de unidades,
teto de impacto de preço, re-tentativas) e troca só a execução: em vez de
montar, assinar e enviar uma transação, aplica a quote na `SimulatedWallet`.
Não precisa de chave privada nem de RPC.
"""

import uuid
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.models import SOLANA_MINTS, SwapResult
from trader.models.account_data import MintBalance
from trader.models.costs import SIMULATED, TradeCosts
from trader.paper.wallet import SimulatedWallet
from trader.providers.jupiter.async_jupiter_svc import (
    DEFAULT_MAX_PRICE_IMPACT_PCT,
    DEFAULT_MAX_SLIPPAGE_BPS,
    AsyncJupiterProvider,
)

# taxa base de uma transação Solana (5000 lamports por assinatura)
DEFAULT_FEE_LAMPORTS = 5000
# rent de uma conta de token SPL (165 bytes); contas Token-2022 custam um
# pouco mais (>= 2_074_080), então em paper é uma aproximação
DEFAULT_ACCOUNT_RENT_LAMPORTS = 2_039_280


class _NoRPC:
    """Paper trading não fala com a blockchain: qualquer uso é um bug."""

    async def aclose(self) -> None:
        return None

    def __getattr__(self, name):
        raise RuntimeError(f"RPC indisponível em paper trading ({name})")


class PaperJupiterProvider(AsyncJupiterProvider):
    def __init__(
        self,
        wallet: SimulatedWallet,
        jupiter_client=None,
        max_price_impact_pct: Decimal | None = DEFAULT_MAX_PRICE_IMPACT_PCT,
        max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
        fee_lamports: int = DEFAULT_FEE_LAMPORTS,
        account_rent_lamports: int = DEFAULT_ACCOUNT_RENT_LAMPORTS,
        cost_source: str = SIMULATED,
    ):
        super().__init__(
            keypair=None,
            rpc_client=_NoRPC(),
            jupiter_client=jupiter_client,
            is_dryrun=True,
            max_price_impact_pct=max_price_impact_pct,
            max_slippage_bps=max_slippage_bps,
        )
        self.wallet = wallet
        self.fee_lamports = fee_lamports
        self.account_rent_lamports = account_rent_lamports
        self.cost_source = cost_source

    def __repr__(self):
        return f"{self.__class__.__name__} with wallet {self.wallet.balances()}"

    async def get_account_balance(self) -> list[MintBalance]:
        return [
            MintBalance(available=amount, mint=Pubkey.from_string(mint))
            for mint, amount in self.wallet.balances().items()
            if mint in SOLANA_MINTS
        ]

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
        in_amount, out_amount = int(quote.inAmount), int(quote.outAmount)
        rent = (
            self.account_rent_lamports if self.wallet.needs_account(output_mint) else 0
        )
        self.wallet.apply_swap(
            input_mint, in_amount, output_mint, out_amount, self.fee_lamports, rent
        )
        return SwapResult(
            signature=f"paper-{uuid.uuid4().hex}",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=in_amount,
            out_amount=out_amount,
            costs=TradeCosts(
                source=self.cost_source,
                fee_lamports=self.fee_lamports,
                rent_lamports=rent,
                actual_in_amount=in_amount,
                actual_out_amount=out_amount,
            ),
            quote=quote,
        )
