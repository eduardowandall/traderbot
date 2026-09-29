# AGENTS.md

Solana trading bot (Jupiter DEX). Python 3.14, `uv`-managed.

## Commands
- Setup: `uv sync --system-certs` (always pass `--system-certs`; this machine uses a corporate TLS cert store that `uv`'s bundled CA bundle doesn't trust). Dev tools live in the `dev` dependency group, installed by default.
- Test: `uv run pytest .` · single test: `uv run pytest tests/trader/bot/test_async_websocket_bot.py::test_name`
- Live checks: `uv run pytest -m live` runs `tests/live/` against the real Jupiter endpoints (paper and read-only; the conftest strips the key/RPC/Telegram env vars; ~1 min). `pytest .` and CI skip it (`addopts -m "not live"`). When you check something live by hand, add it to that suite.
- Lint: `uv run ruff check .` / auto-fix: `uv run ruff check --fix .`
- Format: `uv run ruff format .` · Types: `uv run pyright .`
- CI: `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, `pyright`, `pytest` on push/PR.
- Helper scripts in `.claude/scripts/`, run with `uv run --no-sync python .claude/scripts/<x>.py`:
  - `check.py` is the whole CI gate, with one line per step (`--fix`, `--no-tests`, `[paths]`).
  - `smoke.py` is an isolated paper/dry run followed by a ledger/PnL report. It never uses real mode.
  - `spec_check.py` runs `strategy validate` plus `strategy backtest`.

  Slash commands wrap them: `/check`, `/smoke`, `/spec`, `/diagnose`, `/phase`, `/sync-docs`.

## Running the bot
```bash
uv run --env-file .env main.py run <mode> <SYMBOL> <strategy> '<key=value ...>'
# e.g. run dry SOL-USDC random 'sell_chance=20 buy_chance=40'
```
- Symbol is `OUTPUT-INPUT`: `SOL-USDC` buys SOL with USDC. New symbols must be added to `SOLANA_MINTS` in `trader/models/mints.py`.
- Modes: `dry` still requires `SOLANA_PRIVATE_KEY` + `HELIUS_RPC_URL` (real wallet, simulated send); `paper` needs neither (simulated wallet `.data/paper-wallet.json`, real Jupiter quotes); `real` trades. Optional `SOLANA_PUBLIC_KEY` is validated against the derived key.
- Backtest: `main.py backtest SYMBOL STRATEGY ARGS --ticks FILE|--candles N` (input must be USDC/USDT). Record ticks with `run ... --record-ticks FILE`.
- CLI strategies resolve via `STRATEGIES` in `trader/strategies_registry.py` (random, target_value, composer, spec). Register new top-level strategies there. `trader/__init__.py` must stay empty, because every `import trader.x` loads it. `WeightedMovingAverageStrategy`/`TrailingStopLossStrategy`/`TargetPercentStrategy` are only usable inside `StrategyComposer`.
- Strategy specs (agent-authored, `trader/strategy_spec/`): a JSON spec is run by `SpecStrategy`.
  - Run one yourself with `run paper SOL-USDC spec 'file=spec.json'` (it must be the spec's pair); see the example in `docs/examples/spec-sol-dip.json`.
  - Add a condition type in three places: a model in `models.py` (with `lookback()`/`label()`), the union there, and a predicate in `conditions.PREDICATES`. A test fails if the two sides disagree.
  - Stops and exits never wait for the warm-up; only entries do.

## Agent commands (always JSON on stdout; logs go to stderr)
```bash
uv run main.py market symbols | market price SOL | market candles SOL --n 100 | market summary SOL --interval 1_MINUTE
uv run main.py strategy schema | strategy validate spec.json [--mode paper] | strategy backtest spec.json [--candles 1000 | --ticks FILE]
```
- `{"ok": true, ...}` or `{"ok": false, "errors": [{"path","msg"}]}` with exit code 1. Decimals are strings.
- The commands live in `trader/agent_api/cli.py` (`main.py` only mounts them), and they are read-only: no key, no ledger writes.
- Tests swap `cli.MARKET_DATA` for a fake.
- `botconfigs.example.yaml` is a WIP not wired into the code (no YAML loader exists); config comes from CLI args only.

## One-shot swap
```bash
uv run --env-file .env main.py swap <mode> <SYMBOL_IN> <SYMBOL_OUT> <quantity> [--slippage-bps N]
# e.g. swap dry JUP USDC 1000  |  swap real SOL USDC 0.5 --slippage-bps 100
```
- `quantity` is in UI units of `SYMBOL_IN` (the token being spent), parsed as `Decimal`; converted via `ui_to_raw`.
- `--max-price-impact` (default 1%) rejects quotes above it (`SwapRejectedError`, not retried).
- `slippage_bps` (default 50, max 1000) is forwarded to `AsyncJupiterProvider.swap_with_details`; retries escalate to `slippage_bps + 25`, capped at `max_slippage_bps` (100).
- Runs as `TradeService.swap` in the `manual` bucket (ledger account `<mode>:manual`; older ledgers also have `<mode>:swap` rows): same lock, gateway and `execute_trade` pipeline as strategies, with the fill and costs recorded (`trader/trading_service/manual.py`). A denied/rejected/failed swap prints reasons to stderr and exits 1.

## Architecture
- `main.py` — Typer CLI (`run`, `swap`, `backtest`, `halt`/`resume`, `ledger`, `paper`, `market`, `strategy`). It only parses arguments; the per-mode wiring lives in `trader/wiring.py` (`build_provider`, `build_gateway`, `build_trade_service`, `keypair_from_env`).
- `trader/bot/` is the strategy side and knows no mode, key, provider or ledger. `BotConfig(name, symbol, strategy, market: MarketData, trader: TradeClient, notifier, on_tick)`. The loop is: price → `trader.bucket()` → `strategy.on_market_refresh(price, None, available_usd, position)` → `trader.submit(OrderRequest)` → `OrderReply`. `denied`/`rejected` replies pause orders for 30s; `error` goes through the backoff. It logs under the `bot` logger.
- `trader/trading_service/` is the seam.
  - `protocol.py` holds the plain data: `BucketSnapshot`, `OrderRequest`, `OrderReply`.
  - `client.py` is the `TradeClient` protocol, for one bucket.
  - `service.py` is `TradeService`. Buckets are accounts `"<mode>:<name>"` with an optional `budget_usd`; the cap is `max(0, budget + min(0, realized))`. Orders are serialized, and outcomes are classified into replies.
  - `local.py` is the in-process `LocalTradeClient`.
  - `run` uses the pair as the bucket name, so ledger accounts stay `"<mode>:<pair>"`.
- `trader/providers/jupiter/`:
  - `async_jupiter_svc.py`: `AsyncJupiterProvider[E]`, which quotes, applies the price-impact cap, retries, converts units and fetches costs, then delegates execution to an `Executor`;
  - `executor.py`: the `Executor` protocol plus `OnChainExecutor(keypair, rpc, client, is_dryrun)`;
  - `async_jupiter_client.py` (Jupiter HTTP), `async_rpc_client.py` (Solana/Helius RPC), `candles.py`.

  Build providers with `AsyncJupiterProvider.on_chain(keypair, ...)` or `trader.paper.paper_provider(wallet, ...)`. On-chain internals (sign, send, confirm, costs) live on `provider.executor`. Tests inject `AsyncMock(spec=...)` clients; for a mocked provider, use `factories.mock_provider()`, which sets real `native_fee_reserve`/`balances_track_fills` values.
- `trader/market/` — read-only `MarketData` (`JupiterMarketData`: price and candles, with no key and no RPC). Use it instead of a provider whenever you only need data.
- `trader/indicators.py` — pure `Decimal` indicators plus `BarSeries`, shared by specs and `market summary`. Naive timestamps are treated as local time (`to_utc`).
- `trader/trading_strategy.py` — strategy base + implementations + `StrategyComposer`.
- `trader/execution/gateway.py` — `TradeGateway.submit(intent, execute)`: idempotency → `policy.evaluate` → ledger → execute. It is the only path to a swap and to the ledger for the account, the service and the CLI. It also provides `restore(account)`, `record_fill(...)`, `add_event(...)` and `for_mode(mode, policy=None)`; `halt` and `paper reset` pass `Policy()` so that a broken `policy.toml` never stops them.
- `trader/policy/policy.py` — pure `evaluate()` + TOML loader (`policy.toml` / `TRADER_POLICY_FILE`); defaults deny real mode. `load_policy(mode=...)` merges `[trading]`/`[limits]` with `[<mode>.trading]`/`[<mode>.limits]` (real/dry/paper); all sections are validated regardless of mode.
- `trader/ledger/ledger.py` — SQLite `.data/ledger-<mode>.sqlite3`; hash-chained `events`; positions/PnL restored on startup.
- `trader/paths.py` — `data_dir()` (`TRADER_DATA_DIR`, else `<project root>/.data`) and `policy_file()` (`TRADER_POLICY_FILE`, else `<project root>/policy.toml`). All state (ledger, `HALT`, paper wallet) derives from these, never from the cwd, so `halt`/`resume`/`ledger resolve` reach the running bot from any directory. Relative env values resolve against the project root. Read per call, so env changes apply after import. Never use a bare `Path(".data")`.
- `trader/paper/` — `SimulatedWallet` + `SimulatedExecutor` + `paper_provider()`. `trader/backtest/` — `TickRecorder`/`load_ticks` + `Backtester`, which runs on `TradeService(TradeGateway.in_memory())` + `LocalTradeClient` (in-memory ledger, `Policy.unlimited()`, ignores the live `HALT`) with synthetic quotes via `ReplayQuoteClient` (optional `budget_usd`).
- `trader/logging_config.py` — console filter shows only `bot`/`trader.trading_strategy` at DEBUG (others WARNING); file logs → `.logs/`.

## Known issues / quirks
- Ruff: line-length 88, double quotes, ignores E501/B008. Pyright: `include=["trader/*"]`, basic mode.
- pytest: `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- `tests/trader/bot/test_async_websocket_bot.py` asserts exact mock call sequences — reordering provider calls breaks it.
- CLI `mode` defaults to `dry` for `run`/`swap`; `real` must be explicit. (`main.py start` was removed.)
- Swap retries only cover pre-broadcast failures; anything after `send_transaction` raises `TransactionSubmittedError` and is never retried (avoids double execution).
- Provider `buy`/`sell`/`swap_with_details` return `SwapResult` (quote amounts); `AsyncAccount` records fills from it. `swap` returns only the signature.
- `AsyncAccount` keeps the venue's SOL fee reserve when SOL is spent (`provider.native_fee_reserve`: 0.02 SOL on-chain and in paper, 0 in backtests). It takes `clock=` (the backtester passes the replay clock), and `Order.timestamp` comes from it. It also takes `spend_cap` (the bucket budget) and re-reads balances before a capped buy.
- `Backtester.run` silences the strategy loggers while it replays (`quiet_strategy_logs`).
- Test file basenames must be unique across `tests/`: there are no `__init__.py` files, so duplicate names collide. Shared helpers live in `tests/factories.py` (`make_intent`, `make_spec`, `open_ledger`).
- Telegram credentials: `TELEGRAM_CHAT_ID`/`TELEGRAM_BOT_TOKEN` env vars.
- Jupiter quote/swap calls go to `api.jup.ag` (the old `lite-api.jup.ag` is being sunset). Override with `JUPITER_API_URL`; `JUPITER_API_KEY` is sent as `x-api-key` when set (optional — unauthenticated requests still work at a lower rate limit). The undocumented websocket price feed (`trench-stream.jup.ag`) and candles (`datapi.jup.ag`) are untouched by this — still frontend endpoints, still no fallback.
- `policy.toml`'s `[paper.limits]` must override `max_trade_usd` too (not just `max_daily_notional_usd`/`max_trades_per_hour`), or the first paper buy gets silently denied forever: the default paper wallet (100 USDC/0.5 SOL) times the default strategies' 50-100%-of-balance sizing is $50-100/trade, well above the base `max_trade_usd=25`. Denied intents still show up in `ledger list`/the `events` table (`intent_denied`, reason `"trade de N USD acima do limite 25 USD"`) — check there first if paper trading looks stuck.
- Tests are isolated by the `isolated_workdir` autouse fixture: it points `TRADER_DATA_DIR`/`TRADER_POLICY_FILE` at `tmp_path` (so the ledger, `HALT` and `policy.toml` never touch the real ones) and also chdirs there to contain incidental relative writes (logs, tick CSVs). Write test policies with `policy_file().write_text(...)`. Close `Ledger`s you open (`-W error::ResourceWarning` stays clean).
- UNCONFIRMED intents block all trading until `main.py ledger resolve`; `main.py resume <mode>` re-arms the circuit breaker.
- On Windows, `timeout -s INT` doesn't reach the bot; stop a test run with `taskkill /PID <uv pid> /T /F`.
- Strategies must use `self.clock()` / `self.rng` (not `datetime.now()` / `random`) so backtests stay deterministic; `StrategyComposer` propagates `set_clock`/`seed` to children.
- Ruff runs mccabe `C90` with `max-complexity = 5` (`pyproject.toml`); split functions or use table-driven dispatch rather than suppress.
- Costs and net PnL:
  - `trader/execution/fills.py::execute_trade` is the only caller of
    `provider.fetch_swap_costs(result)`, and calls it only after the gateway
    has marked the intent EXECUTED. Every trade must go through it. That method must
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
- Docs: `docs/architecture.md` is a step-by-step tour of the code (read it first). `docs/plan.md` is the only roadmap: goal, progress score, target architecture (agents and the owner author strategy specs; strategy-runners talk to a per-mode trade-runner), stage A refactorings before stage B features, known issues and the decision log. Keep its progress table (§6) and score (§2) current, and add new backlog items there. Plan in `docs/` first, then implement.
- Layering is enforced by `tests/test_architecture.py`. Every `trader/` module is mapped to a layer (core, strategy, market, venue, risk, execution, strategy-side, app), and imports across layers are checked. A new module must be added to its map. Strategy code and the strategy-side layer must never import the execution, venue or risk layers.
- `trader/logging_config.py` `BotLoggerFileHandler` + `DictConfigurator` are legacy/complex — flag for a future refactor.
