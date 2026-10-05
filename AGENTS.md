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
  `check.py` (the CI gate, one line per step), `smoke.py` (isolated `serve paper` +
  `connect`, then a report), `spec_check.py` (backtest headline), `ledger_dump.py` (read-only JSON
  of a ledger and the paper wallet), `live_vs_backtest.py SPEC --ticks FILE`
  (a `--record-ticks` file replayed next to the bucket's legs in the same
  window). Slash commands wrap them: `/check`,
  `/smoke`, `/spec`, `/diagnose`, `/phase`, `/sync-docs`.

## The CLI: `serve`, `connect`, `backtest`
```bash
uv run main.py serve paper                      # trade-runner: key, wallet, ledger
uv run main.py connect <spec.json> [--trader FILE] [--seed N] [--record-ticks FILE]   # one per spec, no key
uv run --env-file .env main.py serve real       # the owner only
uv run main.py backtest <spec.json> [--candles 1000 | --ticks FILE] [--seed N] [--fee-bps N] [--slippage-bps 10] [--network-fee-usd N] [--json]
```
- **Trading is `serve` + `connect` only** (B14; there is no `run`). One
  `serve <mode>` per mode (`trader/api/cli/lock.py`, an OS lock on
  `.data/trader-<mode>.lock`), plus one `connect spec.json` per spec, in its
  own terminal (even for a single spec). `serve` writes `{host, port, token, pid}` to `.data/trader-<mode>.json`;
  `connect` finds it (or `--trader FILE`), sends the spec's terms (B13:
  `SpecTerms`, from `spec.terms()`: id, symbol, budget, max loss, largest buy,
  expiry; the full spec stays in `connect`), and the trade-runner validates
  them with its own policy. Protocol: `docs/plan.md` §3.3,
  `trader/shared/trading_service/wire.py`, `trader/strategy/trading_service/remote.py`,
  `trader/execution/runner.py`, `trader/strategy/runner.py`. A `serve`
  and a `connect` from different versions may not talk (B6 renamed snapshot
  fields, B7 added the `price` op, B12 the `candles` op): restart them together.
- **Prices come from a hub** (`trader/execution/market/hub.py`, B7): one websocket for
  every mint plus a batched Price API poll for quiet ones. `serve` runs it and
  answers `connect`s through the `price` op (warm-up candles through the
  `candles` op, B12). A price older
  than 30s raises `StalePriceError` (no decisions on stale data), and
  `HubMarketData` paces each bot at one price per second. The hub is also
  the process's `PriceOracle` (B10): `build_trade_service(prices=hub)` gives
  it to the service, the sweep, the daily report and the provider's quote
  check, so only the hub calls the Price API. USDC/USDT = 1 USD is decided
  in one place, `usd_snapshot`/`price_fn` (`trader/execution/market/prices.py`).
- **Wallet checks** (`TradeService`): one balance cache for all buckets; a
  bucket whose budget doesn't fit the wallet (with the other open budgets) is
  refused; at the first open, the open positions of every bucket in the ledger
  are checked against the wallet, and a missing token blocks buys of it
  (`reconcile_mismatch` event).
- Modes: `paper` (simulated wallet `.data/paper-wallet.json`, real Jupiter
  quotes, no key) and `real` (needs `SOLANA_PRIVATE_KEY` + `HELIUS_RPC_URL`;
  optional `SOLANA_PUBLIC_KEY` is checked against the key). The mode has no
  default. There is no dry mode.
- At `hello`, the trade-runner validates the spec's terms against the mode's
  policy and trades in bucket `strategy:<spec_id>` with the spec's
  `budget_usd`; reaching `-max_loss_usd` retires the bucket. `connect
  --record-ticks FILE` writes every price the bot gets (and the quote's USD) for
  `backtest --ticks`. Telegram turns on when `TELEGRAM_CHAT_ID` and
  `TELEGRAM_BOT_TOKEN` are set: fills from `connect` (with the bucket's PnL and
  open mark-to-market) and, from `serve`, a daily report of the previous UTC
  day (`trader/execution/notification/daily_report.py`, once per day via a
  `daily_report` ledger event). Ctrl+C stops them (on Windows, a test run is
  stopped with `taskkill /PID <uv pid> /T /F`).
- `backtest` prints a short readable summary (errors as `erro: path: msg` on
  stderr, exit 1). `--json` prints one JSON object instead, with every trade
  (`{"ok": true, ...}` or `{"ok": false, "errors": [{"path","msg"}]}`, exit 1;
  decimals are strings); scripts and tests use that. It
  uses the spec's budget and max loss, replays each closed candle as an
  interpolated open -> low -> high -> close path, and refuses fewer than
  warm-up + 20 bars. A non-stable input replays two candle series (the
  ratio, plus the quote's USD in `Tick.quote_usd`; a third column in tick
  CSVs) and measures equity in USD. Costs per leg: `fee_bps` +
  `slippage_bps` and `network_fee_usd`, all taken from the replay quote's
  output. Without `--fee-bps`/`--network-fee-usd` both are **measured on
  Jupiter now** (`trader/backtest/costs.py`, B9): half the loss of a buy+sell
  quote at the spec's trade size, and (5000 + `max_priority_fee_lamports`)
  lamports at the SOL price; the result has `measured_costs`. Pass both flags
  for an offline, repeatable run. Every result has `round_trip_costs`.
- The pair is the spec's `symbol`, `OUTPUT-INPUT` (`SOL-USDC` buys SOL with
  USDC; `JUP-SOL` buys JUP with SOL). New symbols go in `SOLANA_MINTS`
  (`trader/shared/models/mints.py`).
- **Two units (B6).** The strategy side sees prices and the bucket's
  `available` in the **quote token** (`market_for` in `trader/shared/market/pair.py`
  divides two USD feeds; positions use `Order.quote_price`); budgets, PnL,
  `Order.price` and the policy stay USD (`TradeService.quote_usd` converts,
  failing closed). For USDC/USDT both are the same numbers.
- No ledger, pnl, swap, halt or paper commands. Inspect state with
  `ledger_dump.py`. Reset paper by deleting `.data/paper-wallet.json` and/or
  `.data/ledger-paper.sqlite3`.

## Strategy specs (`trader/strategy/spec/`)
- Strategies are created as **spec files written from
  [`docs/specs.md`](docs/specs.md)** (the whole contract), kept in
  `docs/examples/`. There is no command to create or submit one, and agents
  don't use a CLI: write the file, `backtest` it, `/smoke --spec` it (an
  isolated `serve paper` + `connect`), iterate.
- Every strategy is a JSON spec run by `SpecStrategy`. Composition is
  `entry.mode`/`exit.mode` (`all`/`any`).
- Exactly one of `expires_at` or `ttl_days` (counted from the first tick).
- Add a condition type in four places: a model in `models.py` (with
  `lookback()`/`label()`), its union there (`EntryCondition` for entry-only
  blocks such as `below_last_exit`), a predicate in `conditions.PREDICATES`,
  and a row in `docs/specs.md`. Tests fail if any of them disagree. Most ideas
  fit an `expr` instead (`trader/strategy/spec/expr.py`: `ast.parse` plus a
  whitelist walk and its own evaluator, never `eval`; an indicator there is one
  entry in `FUNCTIONS`).
- Sizing is `fixed_usd` or `pct_of_bucket` (of what the bucket may spend now).
- Stops and exits never wait for the warm-up; only entries do. Warm-up is
  `spec.history()` (5x the period for EMA, 10x for RSI, capped at 900 bars).
  After an exit, an entry must turn false once (re-arm) before firing again.
- Strategies use `self.clock()` / `self.rng`, never `datetime.now()` / `random`,
  so backtests stay deterministic; predicates get the rng as `TickContext.rng`.

## Guardrails for agent sessions (B4)
Agent sessions work in paper and backtests only. `.claude/settings.json` denies
reading `.env` and editing `.env`/`policy.toml`, and the PreToolUse hook
`.claude/hooks/guard_commands.py` blocks shell commands that start real mode
(`main.py serve real`, and the old `run real`), use `--env-file`, set `TRADER_POLICY_FILE`, mention `.env`,
print `SOLANA_PRIVATE_KEY`/`HELIUS_RPC_URL`, mention `ledger-real.sqlite3` or
`trader-real` (the real trade-runner's token and lock), or run `serve real`.
It matches the command text, so edit docs that mention these with the Edit
tool, not a shell heredoc. Don't work around a block: give the owner the exact
command to run in a terminal. The rules are tested in
`tests/test_guard_commands.py`; they are guardrails, not a sandbox.

## Rules that are easy to break
- **Folders follow the processes** (B11, B12): `trader/execution/` is the
  trade-runner, split into `market/` (everything that reads Jupiter: client,
  candles, `PriceHub`, USD oracle; never moves funds) and `trade/` (gateway,
  venues, ledger, policy, trading service); `trader/strategy/` the
  strategy-runner (bot, spec engine, remote client); `trader/shared/` what both
  import (models, the network-free feed `MarketData`/`HubMarketData`/pairs,
  the spec's terms and their policy checks, wire protocol, paths; the full
  spec language is in `trader/strategy/spec/`, B13); `trader/api/cli/` the commands;
  `trader/backtest/` both in one process. `execution` and `strategy` never
  import each other; `shared` imports neither. A model used by one side lives
  in that side's `models/`. Only the trade-runner talks to Jupiter: `connect`
  gets prices (`price` op) and warm-up candles (`candles` op) from `serve`.
- **Layering** (`tests/test_architecture.py`): every `trader/` module is mapped
  to a layer; a new module must be added to the map. Strategy code and the
  strategy side (`trader/strategy/`) never import execution, venue or risk.
- **One strategy type**: the bot and the backtester take the `Strategy` protocol
  (`trader/strategy/bot/config.py`), which `SpecStrategy` implements, and share one
  per-tick decision (`trader/strategy/bot/decision.py`).
- **One path to a swap**: `TradeGateway.submit` (idempotency -> policy -> ledger
  -> execute), called only by `trader/execution/trade/gateway/fills.py::execute_trade`.
- **Before a real swap is sent** (B7): the quote must be within 2% of the Price
  API value (`max_quote_deviation_pct`, on in `trader/execution/wiring.py`; buys fail
  closed, sells only warn), and `OnChainExecutor` inspects the signed
  transaction (`tx_inspection.py`): allowed programs only, and a simulation
  returning the wallet's accounts where only the input leaves, up to
  `inAmount`. Both refusals are `SwapRejectedError`s. Test fakes of the RPC
  need `inspection_passes()` from `tests/factories.py`.
- **Nothing raises after EXECUTED**: `execute_trade` is the only caller of
  `provider.fetch_swap_costs` (never raises), build orders with
  `order_from_fill` (falls back to the quote), `record_fill` failures are
  logged. Nothing that can fail after confirmation goes inside `_do_swap`.
  Retries only cover pre-broadcast failures and transactions the chain
  confirmed as failed (`TransactionFailedOnChainError`: nothing moved, the
  intent ends FAILED). Any other failure after `send_transaction` is
  `TransactionSubmittedError`: never retried, the intent is UNCONFIRMED.
- **Costs**: never subtract LP fees or slippage from the actual amounts
  (they're already in them). USD values for pairs without a stable or SOL come
  from a Price API snapshot taken **before** the trade. Attempts that failed
  on-chain carry their signatures out of the provider (`failed_signatures` on
  the result or the error); `execute_trade` books their fee on both paths
  (`failed_tx_fee` event + `PositionBook.charge`) and never raises doing it.
  Paper fills 10 bps below the quote (`SimulatedExecutor.slippage_bps`).
- **Priority fee (B9)** is one owner setting, `max_priority_fee_lamports` in
  `policy.toml` (default 100,000): real sends it to Jupiter as the
  `maxLamports` cap (`priority_fee()` in `async_jupiter_client.py`), paper
  charges it in full on every leg, the backtest prices it into
  `network_fee_usd`. `wiring.build_trade_service` loads the policy once for
  the provider and the gateway.
- **Cost per round trip** (`RoundTripCosts`, `Ledger.round_trip_costs`): per
  closed position, entry spend x (exit tick price / entry tick price - 1)
  minus the net realized PnL, in USD and bps. It can be negative when fills
  beat the tick. Shown by the backtest, the daily report,
  `live_vs_backtest.py` and `ledger_dump.py`.
- **Ledger schema** is one `_SCHEMA` with a `user_version` in
  `trader/execution/trade/ledger/store.py`. There are no migrations: an older file is refused
  (`LedgerFormatError`); bump `SCHEMA_VERSION` when the schema changes.
- **UNCONFIRMED** (process killed mid-swap) blocks all trading in that mode
  until the ledger file is moved or deleted. The circuit breaker counts
  failures since the process started (a restart re-arms it); provider
  rejections (`SwapRejectedError`, stored as REJECTED) don't count.
- **RPC commitment is Confirmed** everywhere (reads, simulation, confirmation);
  Finalized lags ~13s and would show pre-swap balances.
- **State paths** go through `trader/shared/paths.py` (`TRADER_DATA_DIR`,
  `TRADER_POLICY_FILE`, `TRADER_LOG_DIR`; relative values resolve against the
  project root). Never a bare `Path(".data")`.
- **Policy** (`policy.toml`, untracked; model `policy.example.toml`): real mode
  is off until `real_trading_enabled = true`. Paper has roomy limits by default
  (`PAPER_DEFAULTS`); `[limits]` applies to every mode, `[paper.*]`/`[real.*]`
  only to theirs. `max_trades_per_hour` is wallet-wide;
  `max_trades_per_hour_per_bucket` caps one spec.
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
- Tests patch module paths such as `trader.strategy.runner.AsyncWebsocketTradingBot`
  and swap `trader.api.cli.backtest.MARKET_DATA` for a fake.
- Ruff: line length 88, mccabe `max-complexity = 5` (split functions rather
  than suppress). Pyright basic on `trader`, `tests`, `main.py`.

## Jupiter
- Quotes/swaps: `api.jup.ag` (`JUPITER_API_URL` overrides; `JUPITER_API_KEY` is
  sent as `x-api-key`, optional). Keyless requests hit `429` under bursts;
  HTTP calls retry with backoff (`_HTTP_RETRY`, pre-broadcast only).
- The price websocket (`trench-stream.jup.ag`, several mints per subscription)
  and candles (`datapi.jup.ag`) are undocumented frontend endpoints; the hub
  backs prices with the Price API V3, candles have no fallback.
