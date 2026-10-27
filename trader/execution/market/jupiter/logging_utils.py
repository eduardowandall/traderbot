"""`logger_wrapper`: cada chamada da Jupiter e do RPC no log, em DEBUG."""

import functools
import logging

from solana.exceptions import SolanaRpcException


def logger_wrapper(func):
    """Loga os argumentos e o resultado (ou o erro) de uma corrotina."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        logger = logging.getLogger(func.__module__)
        # uma linha só (A32): os reprs do solders quebram linhas (`Pubkey(`)
        call = _one_line(f"{func.__name__} with args={args}, kwargs={kwargs}")
        try:
            result = await func(*args, **kwargs)
        except Exception as e:
            error = e.error_msg if isinstance(e, SolanaRpcException) else e
            logger.debug(f"{call} error={error}", exc_info=True)
            raise
        logger.debug(f"{call} with result={_one_line(str(result))}")
        return result

    return wrapper


def _one_line(text: str) -> str:
    return " ".join(text.split())
