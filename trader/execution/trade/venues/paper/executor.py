"""Execução simulada: a quote real da Jupiter é aplicada numa carteira em JSON.

Mesmo contrato do `OnChainExecutor` (ver `trader/execution/trade/venues/jupiter/
executor.py`), sem chave e sem RPC. Cobra a taxa base de rede e o rent da
primeira conta de cada token, como on-chain, e recebe um pouco menos do que a
quote (`slippage_bps`), nunca abaixo do mínimo dela (`otherAmountThreshold`):
on-chain, abaixo disso a transação falharia.
"""

import time
import uuid
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.execution.models.account_data import MintBalance
from trader.execution.models.intent import SentTx, TxOutcome, announce_send
from trader.execution.trade.venues.jupiter.executor import DEFAULT_SOL_FEE_RESERVE
from trader.execution.trade.venues.paper.wallet import SimulatedWallet
from trader.shared.models import SOLANA_MINTS, SwapResult
from trader.shared.models.costs import SIMULATED, TradeCosts

# taxa base de uma transação Solana (5000 lamports por assinatura)
DEFAULT_FEE_LAMPORTS = 5000
# rent de uma conta de token SPL (165 bytes); contas Token-2022 custam um
# pouco mais (>= 2_074_080), então em paper é uma aproximação
DEFAULT_ACCOUNT_RENT_LAMPORTS = 2_039_280
# o fill de paper sai este tanto abaixo da quote (o padrão do backtest)
DEFAULT_SLIPPAGE_BPS = 10
BPS = 10_000


class SimulatedExecutor:
    # a carteira simulada muda a cada ordem: dá para reconciliar

    def __init__(
        self,
        wallet: SimulatedWallet,
        fee_lamports: int = DEFAULT_FEE_LAMPORTS,
        account_rent_lamports: int = DEFAULT_ACCOUNT_RENT_LAMPORTS,
        cost_source: str = SIMULATED,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        # cobrada inteira em cada perna, além da base: o teto
        # `max_priority_fee_lamports` da política (`trader/execution/wiring.py`)
        priority_fee_lamports: int = 0,
    ):
        self.wallet = wallet
        self.slippage_bps = slippage_bps
        self.fee_lamports = fee_lamports
        self.priority_fee_lamports = priority_fee_lamports
        self.account_rent_lamports = account_rent_lamports
        self.cost_source = cost_source
        # sem taxas de rede (backtest) não há por que reservar SOL
        charges_sol = (
            fee_lamports > 0 or priority_fee_lamports > 0 or account_rent_lamports > 0
        )
        self.native_fee_reserve = (
            DEFAULT_SOL_FEE_RESERVE if charges_sol else Decimal("0")
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__} with wallet {self.wallet.balances()}"

    async def balances(self) -> list[MintBalance]:
        return [
            MintBalance(available=amount, mint=Pubkey.from_string(mint))
            for mint, amount in self.wallet.balances().items()
            if mint in SOLANA_MINTS
        ]

    async def execute(
        self, input_mint: str, output_mint: str, quote: JupiterQuoteResponse
    ) -> SwapResult:
        in_amount, out_amount = int(quote.inAmount), self._filled_out(quote)
        self.wallet.reload()  # outro processo pode ter aberto a conta do token
        rent = (
            self.account_rent_lamports if self.wallet.needs_account(output_mint) else 0
        )
        # como on-chain: `fee_lamports` é a taxa total (base + priority)
        fee = self.fee_lamports + self.priority_fee_lamports
        signature = f"paper-{uuid.uuid4().hex}"
        announce_send(
            SentTx(
                signature,
                input_mint,
                output_mint,
                in_amount,
                int(quote.outAmount),
                sent_at=time.time(),
            )
        )
        self.wallet.apply_swap(
            input_mint, in_amount, output_mint, out_amount, fee, rent, signature
        )
        return SwapResult(
            signature=signature,
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=in_amount,
            out_amount=out_amount,
            costs=self._costs(in_amount, out_amount, fee, rent),
            quote=quote,
        )

    def _costs(self, in_amount: int, out_amount: int, fee: int, rent: int):
        return TradeCosts(
            source=self.cost_source,
            fee_lamports=fee,
            priority_fee_lamports=self.priority_fee_lamports,
            rent_lamports=rent,
            actual_in_amount=in_amount,
            actual_out_amount=out_amount,
        )

    def _filled_out(self, quote: JupiterQuoteResponse) -> int:
        """A saída do fill: a quote menos o slippage, no mínimo o da quote."""
        quoted = int(quote.outAmount)
        slipped = quoted * (BPS - self.slippage_bps) // BPS
        floor = min(int(quote.otherAmountThreshold or 0), quoted)
        return max(slipped, floor)

    async def fetch_costs(self, result: SwapResult) -> TradeCosts | None:
        # os custos simulados já vêm no resultado da execução; uma intenção
        # resolvida depois (A3) os lê do registro da carteira
        if result.costs is not None:
            return result.costs
        applied = self.wallet.applied(result.signature)
        if applied is None:
            return None
        return self._costs(
            applied["in_amount"], applied["out_amount"], applied["fee"], applied["rent"]
        )

    async def outcome(self, sent: SentTx) -> TxOutcome:
        """Aplicado na carteira, ou nunca: o swap simulado é aplicado na hora.

        Fora do registro, mas enviado antes do último corte dele: pode ter
        sido aplicado e cortado, então fica PENDING (o dono confere).
        """
        if self.wallet.applied(sent.signature) is not None:
            return TxOutcome.LANDED
        trimmed = self.wallet.trimmed_at
        if trimmed is not None and (sent.sent_at is None or sent.sent_at <= trimmed):
            return TxOutcome.PENDING
        return TxOutcome.EXPIRED

    async def fetch_fee(self, signature: str) -> int | None:
        return None  # nada falha "na rede" aqui

    async def aclose(self) -> None:
        return None
