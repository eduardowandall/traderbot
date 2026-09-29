"""Ponto de entrada: `uv run main.py <comando>`. Os comandos estão em `trader/cli/`."""

from trader import logging_config
from trader.cli import app

__all__ = ["app"]

if __name__ == "__main__":
    logging_config.setup_logging()
    app()
