import functools
import logging

from solana.exceptions import SolanaRpcException


def logger_wrapper(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        logger = logging.getLogger(func.__module__)
        try:
            result = await func(*args, **kwargs)
            logger.debug(
                f"{func.__name__} with args={args}, kwargs={kwargs} with result={result}"
            )
            return result
        except SolanaRpcException as e:
            logger.debug(
                f"{func.__name__}  with args={args}, kwargs={kwargs} error={str(e.error_msg)}",
                exc_info=True,
            )
            raise
        except Exception as e:
            logger.debug(
                f"{func.__name__}  with args={args}, kwargs={kwargs} error={str(e)}",
                exc_info=True,
            )
            raise

    return wrapper
