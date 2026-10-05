from enum import StrEnum, auto


class RunningMode(StrEnum):
    REAL = auto()
    # carteira simulada com quotes reais: não usa chave nem RPC
    PAPER = auto()
