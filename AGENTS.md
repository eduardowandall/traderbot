# AGENTS.md

Solana trading bot (Jupiter DEX). Python 3.14, `uv`-managed.

## Commands
- Setup: `uv sync --extra dev`
- Test: `uv run pytest .` · single test: `uv run pytest tests/trader/bot/test_async_websocket_bot.py::test_name`
- Lint: `uv run ruff check .` / auto-fix: `uv run ruff check --fix .`
- Format: `uv run ruff format .` · Types: `uv run pyright .`
- The `Makefile` is deprecated (expect removal); prefer raw `uv` commands. The README also documents `make format-check`/`make ruff`, which don't exist.

## Running the bot
```bash
uv run --env-file .env main.py run <mode> <SYMBOL> <strategy> '<key=value ...>'
# e.g. run dry SOL-USDC random 'sell_chance=20 buy_chance=40'
```
- Symbol is `OUTPUT-INPUT`: `SOL-USDC` buys SOL with USDC. New symbols must be added to `SOLANA_MINTS` in `trader/models/mints.py`.
- `dry` mode still requires `SOLANA_PRIVATE_KEY` (keypair always loaded); optional `SOLANA_PUBLIC_KEY` is validated against the derived key.
- CLI strategies resolve via `STRATEGIES` in `trader/__init__.py` (random, target_value, composer) — register new top-level strategies there. `WeightedMovingAverageStrategy`/`TrailingStopLossStrategy`/`TargetPercentStrategy` are only usable inside `StrategyComposer`.
- `botconfigs.example.yaml` is a WIP not wired into the code (no YAML loader exists); config comes from CLI args only.

## Stale code (do not use as reference)
- `desk.py`, `manual_swap.py`, and the `main.py` `run` docstring use old CLI flags (`--wallet-key`, `--api=`, `--websocket`, `--notification-*`); `manual_swap.py` imports the removed `trader/providers/jupiter/jupiter_public_api`.

## Architecture
- `main.py` — Typer CLI (`run`, `start`).
- `trader/bot/async_websocket_bot.py` — loop: price → `strategy.on_market_refresh` → place order; logs under the `bot` logger.
- `trader/providers/jupiter/` — `async_jupiter_svc.py` (facade: swap/buy/sell), `async_jupiter_client.py` (Jupiter HTTP), `async_rpc_client.py` (Solana/Helius RPC). Tests inject `AsyncMock(spec=...)` clients.
- `trader/trading_strategy.py` — strategy base + implementations + `StrategyComposer`.
- `logging_config.py` — console filter shows only `bot`/`trader.trading_strategy` at DEBUG (others WARNING); file logs → `.logs/`.

## Known issues / quirks
- `uv.lock` is gitignored (mistake; to be fixed later — don't address it now).
- Ruff: line-length 88, double quotes, ignores E501/B008. Pyright: `include=["trader/*"]`, basic mode.
- pytest: `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- `tests/trader/bot/test_async_websocket_bot.py` asserts exact mock call sequences — reordering provider calls breaks it.
