"""Erros de execução de swap compartilhados entre camadas.

Ficam em models (core) porque quem os trata — gateway, conta, carteira
simulada, serviço de trading — não deve depender do módulo que executa swaps.

Os erros que encerram as tentativas de um swap levam `failed_signatures`: as
transações que a rede confirmou como falhas antes disso. A taxa delas foi
paga, e `execute_trade` a registra como custo do bucket.
"""


class SwapAttemptsError(Exception):
    """Um erro que encerra as tentativas de um swap.

    `failed_signatures`: as tentativas que a rede confirmou como falhas antes
    dele (o provider preenche ao encerrar).
    """

    failed_signatures: tuple[str, ...] = ()


def failed_signatures_of(ex: BaseException) -> tuple[str, ...]:
    """As transações falhas que um erro de swap carrega (nenhuma em outros)."""
    return ex.failed_signatures if isinstance(ex, SwapAttemptsError) else ()


class SwapRejectedError(SwapAttemptsError):
    """Swap recusado antes do envio por uma regra de segurança.

    Não é re-tentado: a mesma quote seria recusada novamente.
    """


class SwapFailedError(SwapAttemptsError, RuntimeError):
    """Todas as tentativas falharam sem mover fundos (quote, RPC, rede)."""


class TransactionFailedOnChainError(Exception):
    """A rede processou a transação e ela falhou (ex: slippage estourado).

    Não é incerteza: nada foi trocado (só a taxa foi cobrada), então pode ser
    re-tentada, e a intenção termina FAILED, não UNCONFIRMED.
    """

    def __init__(self, message: str, signature: str | None = None):
        super().__init__(message)
        self.signature = signature


class TransactionSubmittedError(SwapAttemptsError):
    """Falha depois que a transação já foi enviada à rede.

    Não deve ser re-tentada automaticamente: a transação pode ter sido
    executada, e um novo envio poderia duplicar o swap.
    """

    def __init__(self, message: str, signature: str | None = None):
        super().__init__(message)
        # guardada no ledger: permite conferir a transação e cobrar a taxa
        # mesmo se ela falhou
        self.signature = signature
