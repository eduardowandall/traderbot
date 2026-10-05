"""Ponto de entrada: `uv run main.py <comando>`. Os comandos estão em `trader/api/cli/`."""

import sys

from trader.api.cli import app
from trader.shared import logging_config

__all__ = ["app", "main"]


def main() -> None:
    logging_config.setup_logging()
    try:
        app()
    except Exception as ex:
        # o Typer troca o `sys.excepthook` pelo dele (que imprime o traceback
        # cru, com a api-key do Helius encadeada em erros do httpx2): o erro
        # que escapa passa pelo `redact()` aqui mesmo
        logging_config.redacted_excepthook(type(ex), ex, ex.__traceback__)
        sys.exit(1)


if __name__ == "__main__":
    main()
