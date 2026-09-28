# AGENTS.md

Solana trading bot (Jupiter DEX). Python 3.14, `uv`-managed.

## Commands
- Setup: `uv sync --system-certs` (always pass `--system-certs`; this machine uses a corporate TLS cert store that `uv`'s bundled CA bundle doesn't trust). Dev tools live in the `dev` dependency group, installed by default.
- Test: `uv run pytest .` · single test: `uv run pytest tests/trader/bot/test_async_websocket_bot.py::test_name`
- Lint: `uv run ruff check .` / auto-fix: `uv run ruff check --fix .`
- Format: `uv run ruff format .` · Types: `uv run pyright .`
- CI: `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, `pyright`, `pytest` on push/PR.

## Running the bot
```bash
uv run --env-file .env main.py run <mode> <SYMBOL> <strategy> '<key=value ...>'
# e.g. run dry SOL-USDC random 'sell_chance=20 buy_chance=40'
```
- Symbol is `OUTPUT-INPUT`: `SOL-USDC` buys SOL with USDC. New symbols must be added to `SOLANA_MINTS` in `trader/models/mints.py`.
- Modes: `dry` still requires `SOLANA_PRIVATE_KEY` + `HELIUS_RPC_URL` (real wallet, simulated send); `paper` needs neither (simulated wallet `.data/paper-wallet.json`, real Jupiter quotes); `real` trades. Optional `SOLANA_PUBLIC_KEY` is validated against the derived key.
- Backtest: `main.py backtest SYMBOL STRATEGY ARGS --ticks FILE|--candles N` (input must be USDC/USDT). Record ticks with `run ... --record-ticks FILE`.
- CLI strategies resolve via `STRATEGIES` in `trader/__init__.py` (random, target_value, composer) — register new top-level strategies there. `WeightedMovingAverageStrategy`/`TrailingStopLossStrategy`/`TargetPercentStrategy` are only usable inside `StrategyComposer`.
- `botconfigs.example.yaml` is a WIP not wired into the code (no YAML loader exists); config comes from CLI args only.

## One-shot swap
```bash
uv run --env-file .env main.py swap <mode> <SYMBOL_IN> <SYMBOL_OUT> <quantity> [--slippage-bps N]
# e.g. swap dry JUP USDC 1000  |  swap real SOL USDC 0.5 --slippage-bps 100
```
- `quantity` is in UI units of `SYMBOL_IN` (the token being spent), parsed as `Decimal`; converted via `ui_to_raw`.
- `--max-price-impact` (default 1%) rejects quotes above it (`SwapRejectedError`, not retried).
- `slippage_bps` (default 50, max 1000) is forwarded to `AsyncJupiterProvider.swap`; retries escalate to `slippage_bps + 25`, capped at `max_slippage_bps` (100).
- Replaces the deleted `manual_swap.py` capability; built on `trader/providers/jupiter/async_jupiter_svc.py`.

## Architecture
- `main.py` — Typer CLI (`run`, `start`, `swap`).
- `trader/bot/async_websocket_bot.py` — loop: price → `strategy.on_market_refresh` → place order; logs under the `bot` logger.
- `trader/providers/jupiter/` — `async_jupiter_svc.py` (facade: swap/buy/sell), `async_jupiter_client.py` (Jupiter HTTP), `async_rpc_client.py` (Solana/Helius RPC). Tests inject `AsyncMock(spec=...)` clients.
- `trader/trading_strategy.py` — strategy base + implementations + `StrategyComposer`.
- `trader/execution/gateway.py` — `TradeGateway.submit(intent, execute)`: idempotency → `policy.evaluate` → ledger → execute. The only path to a swap for the bot (via `AsyncAccount(gateway=...)`) and `main.py swap`.
- `trader/policy/policy.py` — pure `evaluate()` + TOML loader (`policy.toml` / `TRADER_POLICY_FILE`); defaults deny real mode. `load_policy(mode=...)` merges `[trading]`/`[limits]` with `[<mode>.trading]`/`[<mode>.limits]` (real/dry/paper); all sections are validated regardless of mode.
- `trader/ledger/ledger.py` — SQLite `.data/ledger-<mode>.sqlite3`; hash-chained `events`; positions/PnL restored on startup.
- `trader/paper/` — `SimulatedWallet` + `PaperJupiterProvider` (overrides `_do_swap` only). `trader/backtest/` — `TickRecorder`/`load_ticks` + `Backtester` (synthetic quotes via `ReplayQuoteClient`).
- `trader/logging_config.py` — console filter shows only `bot`/`trader.trading_strategy` at DEBUG (others WARNING); file logs → `.logs/`.

## Known issues / quirks
- Ruff: line-length 88, double quotes, ignores E501/B008. Pyright: `include=["trader/*"]`, basic mode.
- pytest: `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- `tests/trader/bot/test_async_websocket_bot.py` asserts exact mock call sequences — reordering provider calls breaks it.
- CLI `mode` defaults to `dry` for `run`/`start`/`swap`; `real` must be explicit.
- Swap retries only cover pre-broadcast failures; anything after `send_transaction` raises `TransactionSubmittedError` and is never retried (avoids double execution).
- Provider `buy`/`sell`/`swap_with_details` return `SwapResult` (quote amounts); `AsyncAccount` records fills from it. `swap` returns only the signature.
- `AsyncAccount` keeps a 0.02 SOL fee reserve when SOL is spent.
- Telegram credentials: `TELEGRAM_CHAT_ID`/`TELEGRAM_BOT_TOKEN` env vars.
- Jupiter quote/swap calls go to `api.jup.ag` (the old `lite-api.jup.ag` is being sunset). Override with `JUPITER_API_URL`; `JUPITER_API_KEY` is sent as `x-api-key` when set (optional — unauthenticated requests still work at a lower rate limit). The undocumented websocket price feed (`trench-stream.jup.ag`) and candles (`datapi.jup.ag`) are untouched by this — still frontend endpoints, still no fallback.
- `policy.toml`'s `[paper.limits]` must override `max_trade_usd` too (not just `max_daily_notional_usd`/`max_trades_per_hour`), or the first paper buy gets silently denied forever: the default paper wallet (100 USDC/0.5 SOL) times the default strategies' 50-100%-of-balance sizing is $50-100/trade, well above the base `max_trade_usd=25`. Denied intents still show up in `ledger list`/the `events` table (`intent_denied`, reason `"trade de N USD acima do limite 25 USD"`) — check there first if paper trading looks stuck.
- Tests run in a temp cwd (`isolated_workdir` autouse fixture) so `.data/`, `HALT` and `policy.toml` never touch the real ones. Close `Ledger`s you open (`-W error::ResourceWarning` stays clean).
- UNCONFIRMED intents block all trading until `main.py ledger resolve`; `main.py resume <mode>` re-arms the circuit breaker.
- On Windows, `timeout -s INT` doesn't reach the bot; stop a test run with `taskkill /PID <uv pid> /T /F`.
- Strategies must use `self.clock()` / `self.rng` (not `datetime.now()` / `random`) so backtests stay deterministic; `StrategyComposer` propagates `set_clock`/`seed` to children.
- Ruff runs mccabe `C90` (`max-complexity = 10`); split functions rather than suppress.
- Costs and net PnL:
  - `AsyncAccount._execute_order` calls `provider.fetch_swap_costs(result)`
    only after the gateway has marked the intent EXECUTED. That method must
    never raise; anything that can fail after confirmation must not go inside
    `_do_swap`, because retries would double-execute.
  - The parser is `trader/providers/jupiter/swap_costs.py`; the models are in
    `trader/models/costs.py`.
  - Never subtract LP fees or slippage from the actual amounts; they are
    already in them.
  - Ledger columns are added by `_migrate()` (ALTER TABLE). Don't put new
    columns only in `_SCHEMA`.
  - The Windows console is cp1252: avoid non-Latin-1 symbols in
    log/CLI output (use `[!]`).
- Agent-integration roadmap and open issues: `docs/plan.md`. Code-quality refactoring backlog: `docs/refactoring-backlog.md` (update it when you fix or add items).
- `trader/logging_config.py` `BotLoggerFileHandler` + `DictConfigurator` are legacy/complex — flag for a future refactor.
- `main.py start` is a near-duplicate of `run` — flag for a future broader strategy-wiring refactor.
