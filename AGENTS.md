# AGENTS.md

Solana trading bot (Jupiter DEX). Python 3.14, `uv`-managed. How the code
works, step by step: [`docs/architecture.md`](docs/architecture.md). The
roadmap: [`docs/plan.md`](docs/plan.md) (plan there first, then implement).

## Commands
- Setup: `uv sync --system-certs` (always pass `--system-certs`: this machine's
  corporate TLS store isn't in uv's CA bundle). Dev tools are in the `dev` group.
- Test: `uv run pytest .` · one test: `uv run pytest tests/test_main.py::test_name`
- Live checks: `uv run pytest -m live` runs `tests/live/` against the real
  Jupiter endpoints (paper and read-only; the conftest strips the key/RPC/Telegram
  env vars; ~1 min). `pytest .` and CI skip it. When you check something live by
  hand, add it to that suite.
- Lint/format/types: `uv run ruff check [--fix] .` · `uv run ruff format .` · `uv run pyright .`
- CI (`.github/workflows/ci.yml`, ubuntu + windows): ruff check, ruff format
  --check, pyright, pytest. `live.yml` runs the live suite on demand.
- Scripts in `.claude/scripts/`, run with `uv run --no-sync python .claude/scripts/<x>.py`:
  `check.py` (the CI gate, one line per step), `smoke.py` (isolated paper run +
  report), `spec_check.py` (backtest headline), `ledger_dump.py` (read-only JSON
  of a ledger and the paper wallet). Slash commands wrap them: `/check`,
  `/smoke`, `/spec`, `/diagnose`, `/phase`, `/sync-docs`.

## The CLI: `run` and `backtest` only
```bash
uv run main.py run paper docs/examples/spec-random.json [--seed N] [--record-ticks FILE]
uv run --env-file .env main.py run real <spec.json>
uv run main.py backtest <spec.json> [--candles 1000 | --ticks FILE] [--seed N] [--fee-bps 30] [--slippage-bps 10]
```
- Modes: `paper` (simulated wallet `.data/paper-wallet.json`, real Jupiter
  quotes, no key) and `real` (needs `SOLANA_PRIVATE_KEY` + `HELIUS_RPC_URL`;
  optional `SOLANA_PUBLIC_KEY` is checked against the key). The mode has no
  default. There is no dry mode.
- `run` validates the spec against the mode's policy and trades in bucket
  `strategy:<spec_id>` with the spec's `budget_usd`; reaching `-max_loss_usd`
  retires the bucket. Telegram turns on when `TELEGRAM_CHAT_ID` and
  `TELEGRAM_BOT_TOKEN` are set. Ctrl+C stops it (on Windows, a test run is
  stopped with `taskkill /PID <uv pid> /T /F`).
- `backtest` always prints one JSON object (`{"ok": true, ...}` or
  `{"ok": false, "errors": [{"path","msg"}]}`, exit 1; decimals are strings). It
  uses the spec's budget and max loss, replays each closed candle as an
  interpolated open -> low -> high -> close path, and refuses fewer than
  warm-up + 20 bars. Input must be USDC/USDT.
- The pair is the spec's `symbol`, `OUTPUT-INPUT` (`SOL-USDC` buys SOL with
  USDC). New symbols go in `SOLANA_MINTS` (`trader/models/mints.py`).
- No ledger, pnl, swap, halt or paper commands. Inspect state with
  `ledger_dump.py`. Reset paper by deleting `.data/paper-wallet.json` and/or
  `.data/ledger-paper.sqlite3`.

## Strategy specs (`trader/strategy_spec/`)
- Every strategy is a JSON spec run by `SpecStrategy`; examples in
  `docs/examples/`. Composition is `entry.mode`/`exit.mode` (`all`/`any`).
- Exactly one of `expires_at` or `ttl_days` (counted from the first tick).
- Add a condition type in three places: a model in `models.py` (with
  `lookback()`/`label()`), its union there (`EntryCondition` for entry-only
  blocks such as `below_last_exit`), and a predicate in `conditions.PREDICATES`.
  A test fails if they disagree.
- Stops and exits never wait for the warm-up; only entries do. Warm-up is
  `spec.history()` (5x the period for EMA, 10x for RSI, capped at 900 bars).
  After an exit, an entry must turn false once (re-arm) before firing again.
- Strategies use `self.clock()` / `self.rng`, never `datetime.now()` / `random`,
  so backtests stay deterministic; predicates get the rng as `TickContext.rng`.

## Rules that are easy to break
- **Layering** (`tests/test_architecture.py`): every `trader/` module is mapped
  to a layer; a new module must be added to the map. Strategy code and the
  strategy side (`trader/bot/`) never import execution, venue or risk.
- **One strategy type**: the bot and the backtester take the `Strategy` protocol
  (`trader/bot/config.py`), which `SpecStrategy` implements, and share one
  per-tick decision (`trader/bot/decision.py`).
- **One path to a swap**: `TradeGateway.submit` (idempotency -> policy -> ledger
  -> execute), called only by `trader/execution/fills.py::execute_trade`.
- **Nothing raises after EXECUTED**: `execute_trade` is the only caller of
  `provider.fetch_swap_costs` (never raises), build orders with
  `order_from_fill` (falls back to the quote), `record_fill` failures are
  logged. Nothing that can fail after confirmation goes inside `_do_swap`.
  Retries only cover pre-broadcast failures; after `send_transaction` it's
  `TransactionSubmittedError`, never retried.
- **Costs**: never subtract LP fees or slippage from the actual amounts
  (they're already in them). USD values for pairs without a stable or SOL come
  from a Price API snapshot taken **before** the trade.
- **Ledger schema** is one `_SCHEMA` with a `user_version` in
  `trader/ledger/store.py`. There are no migrations: an older file is refused
  (`LedgerFormatError`); bump `SCHEMA_VERSION` when the schema changes.
- **UNCONFIRMED** (process killed mid-swap) blocks all trading in that mode
  until the ledger file is moved or deleted. The circuit breaker counts
  failures since the process started (a restart re-arms it).
- **State paths** go through `trader/paths.py` (`TRADER_DATA_DIR`,
  `TRADER_POLICY_FILE`, `TRADER_LOG_DIR`; relative values resolve against the
  project root). Never a bare `Path(".data")`.
- **Policy** (`policy.toml`, untracked; model `policy.example.toml`): real mode
  is off until `real_trading_enabled = true`. Paper has roomy limits by default
  (`PAPER_DEFAULTS`); `[limits]` applies to every mode, `[paper.*]`/`[real.*]`
  only to theirs.
- `trader/__init__.py` stays empty (every `import trader.x` loads it).
- Windows console is cp1252: no non-Latin-1 symbols in log/CLI output (use `[!]`).

## Tests
- `asyncio_mode = "auto"`; `-W error::ResourceWarning` (close the `Ledger`s you open).
- The autouse `isolated_workdir` fixture points the three `TRADER_*` paths at
  `tmp_path` and chdirs there. Write test policies with `policy_file().write_text(...)`.
- Test file basenames are unique across `tests/` (no `__init__.py`). Shared
  helpers are in `tests/factories.py` (`make_intent`, `make_spec`,
  `open_ledger`, `memory_gateway`, `mock_provider`, `StubStrategy` for bot and
  backtest fakes).
- Tests patch module paths such as `trader.cli.bot.AsyncWebsocketTradingBot`
  and swap `trader.cli.bot.MARKET_DATA` for a fake.
- Ruff: line length 88, mccabe `max-complexity = 5` (split functions rather
  than suppress). Pyright basic on `trader`, `tests`, `main.py`.

## Jupiter
- Quotes/swaps: `api.jup.ag` (`JUPITER_API_URL` overrides; `JUPITER_API_KEY` is
  sent as `x-api-key`, optional). Keyless requests hit `429` under bursts;
  HTTP calls retry with backoff (`_HTTP_RETRY`, pre-broadcast only).
- The price websocket (`trench-stream.jup.ag`) and candles (`datapi.jup.ag`) are
  undocumented frontend endpoints; prices fall back to the Price API V3,
  candles have no fallback.
