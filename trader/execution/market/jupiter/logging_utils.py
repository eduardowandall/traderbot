"""`logger_wrapper`: cada chamada da Jupiter e do RPC no log, em DEBUG."""

import functools
import logging

from solana.exceptions import SolanaRpcException


def logger_wrapper(func):
    """Loga os argumentos e o resultado (ou o erro) de uma corrotina."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        logger = logging.getLogger(func.__module__)
        call = f"{func.__name__} with args={args}, kwargs={kwargs}"
        try:
            result = await func(*args, **kwargs)
        except Exception as e:
            error = e.error_msg if isinstance(e, SolanaRpcException) else e
            logger.debug(f"{call} error={error}", exc_info=True)
            raise
        logger.debug(f"{call} with result={result}")
        return result

    return wrapper
