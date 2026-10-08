"""O rent de volta: fechar a conta de token de um bucket encerrado (A15).

A primeira compra de um token abre a conta dele na carteira, e o rent dela
entra nos custos do bucket. Fechar a conta vazia devolve esse rent; o
`serve` fecha quando o bucket encerra sem posição e nada mais usa o token.

No ledger são dois eventos (sem coluna nova): `rent_refund_sent`, gravado
antes do envio (como o `intent_sent` dos swaps), e `rent_refund`, com o
desfecho. O PnL soma o `rent_refund` como o `failed_tx_fee`: fora das
intenções.
"""

from dataclasses import dataclass
from decimal import Decimal

from trader.shared.models.costs import LAMPORTS_PER_SOL


@dataclass(frozen=True)
class RentRefund:
    """O desfecho do fechamento de uma conta de token."""

    signature: str
    mint: str
    refund_lamports: int  # o rent devolvido (0: a transação não fechou a conta)
    # a taxa da transação; o executor não a lê, o provider preenche
    fee_lamports: int = 0

    @property
    def net_sol(self) -> Decimal:
        return Decimal(self.refund_lamports - self.fee_lamports) / LAMPORTS_PER_SOL
