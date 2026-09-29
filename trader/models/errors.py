"""Erros de execução de swap compartilhados entre camadas.

Ficam em models (core) porque quem os trata — gateway, conta, carteira
simulada, serviço de trading — não deve depender do módulo que executa swaps.
"""


class SwapRejectedError(Exception):
    """Swap recusado antes do envio por uma regra de segurança.

    Não é re-tentado: a mesma quote seria recusada novamente.
    """


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
