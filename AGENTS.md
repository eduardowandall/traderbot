# AGENTS.md

Solana trading bot (Jupiter DEX). Python 3.14, `uv`-managed. How the code
works, step by step: [`docs/architecture.md`](docs/architecture.md). The
roadmap: [`docs/plan.md`](docs/plan.md) (items A1..., plan there first, then
implement; finished items move to `docs/history.md`, whose stage labels such
as B7 are what code comments cite).

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
uv run main.py stop <spec.json> | --all [--trader FILE]   # manual stop: sell now, never buy again
uv run --env-file .env main.py serve real       # the owner only
uv run main.py backtest <spec.json> [--candles 1000 | --ticks FILE | --from ISO [--to ISO] | --scenario ID|tag:X|all ... | --rolling 7d --from ISO] [--max-hole 1h] [--vary PATH=V1,V2 ... [--write-best FILE]] [--seed N[,N2]] [--jobs N] [--costs 2x] [--shock -15%@50%] [--volume-slippage] [--fee-bps N] [--slippage-bps 10] [--network-fee-usd N] [--borrow-bps-hour 1] [--json]
```
- **Trading is `serve` + `connect` only** (B14; there is no `run`). One
  `serve <mode>` per mode (`trader/api/cli/lock.py`, an OS lock on
  `.data/trader-<mode>.lock`), plus one `connect spec.json` per spec, in its
  own terminal (even for a single spec). `serve` writes `{host, port, token, pid}` to `.data/trader-<mode>.json`;
  `connect` finds it (or `--trader FILE`), sends the spec's terms (B13:
  `SpecTerms`, from `spec.terms()`: id, symbol, budget, max loss, largest buy,
  expiry; the full spec stays in `connect`), and the trade-runner validates
  them with its own policy. Protocol: `docs/architecture.md` §9,
  `trader/shared/trading_service/wire.py`, `trader/strategy/trading_service/remote.py`,
  `trader/execution/runner.py`, `trader/strategy/runner.py`. A `serve`
  and a `connect` from different versions may not talk (B6 renamed snapshot
  fields, B7 added the `price` op, B12 the `candles` op, A12
  `market.add_collateral`, A20 the position's leg counts, A29 the reply's
  `retry_after`, A21 the `tick` op and the reply's `snapshot`): restart
  them together.
- **The manual stop** (A33): `stop` sends one `stop` op (token, no `hello`;
  the spec's terms, or `all` for every bucket this `serve` has open). The
  trade-runner opens the bucket if needed, `TradeService.stop` retires it
  and writes `bucket_stopped` (read back by `open_bucket`: the stop survives
  restarts), and sells the position at the hub's pair price; a failed sale
  is retried by the sweep. A running `connect` exits (`bucket_done`).
  `smoke.py --stop` ends a smoke run with it.
- **Prices come from a hub** (`trader/execution/market/hub.py`, B7): one websocket for
  every mint plus a batched Price API poll for quiet ones. `serve` runs it and
  answers `connect`s through the `tick` op (A21: the mints' USD prices and
  the bucket snapshot in one reply, read by `RemoteTicks`; warm-up candles
  through the `candles` op, B12). A price older
  than 30s raises `StalePriceError` (no decisions on stale data; a refused
  reply carries its `kind`, and `connect` raises `PriceUnavailableError`), and
  `HubMarketData` paces each bot at one price per second. The hub is also
  the process's `PriceOracle` (B10): `build_trade_service(prices=hub)` gives
  it to the service, the sweep, the daily report and the provider's quote
  check, so only the hub calls the Price API. USDC/USDT = 1 USD is decided
  in one place, `usd_snapshot`/`price_fn` (`trader/execution/market/prices.py`).
- **Wallet checks** (`TradeService`): one balance cache for all buckets, but
  every order reads the wallet fresh (`WalletBalances.fresh`, A5; the cache is
  for allocation, reconcile and snapshots); a
  bucket whose budget doesn't fit the wallet (with the other open budgets) is
  refused; a fresh read of a token (not SOL) also reads its account by
  address and keeps the larger (`token_balance`, A15: the RPC's owner index
  once missed an account); at the first open, the open positions of every bucket in the ledger
  are checked against the wallet, and a missing token blocks buys of it
  (`reconcile_mismatch` event). A sell the wallet can't cover (for SOL, after
  the wallet's fee reserve, `WalletBalances.spendable`: 0.02 SOL in real and
  paper, 0 in replays) is refused, never capped (`WalletShortfallError`): it
  blocks buys of the token and, once per position, writes `sell_shortfall`
  and sends Telegram from `serve`.
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
  `daily_report` ledger event), and network outages (A28,
  `notification/outages.py`): with any open position, one message once the
  positions' prices have been stale in the hub (checked every 10 s) or the
  perps sweep has failed for 120 s, and one when it is back. Ctrl+C stops them (on Windows, a test run is
  stopped with `taskkill /PID <uv pid> /T /F`).
- `backtest` prints a short readable summary (errors as `erro: path: msg` on
  stderr, exit 1). `--json` prints one JSON object instead, with every trade
  (`{"ok": true, ...}` or `{"ok": false, "errors": [{"path","msg"}]}`, exit 1;
  decimals are strings); scripts and tests use that. It
  uses the spec's budget and max loss, replays each closed candle as an
  interpolated open -> low -> high -> close path (a bar with no candle is one
  flat tick at the previous close: no trade, A4), and refuses fewer than
  warm-up + 20 bars. `--from/--to` (A34) replays a past window (no zone:
  UTC; warm-up from before it): `JupiterCandles.between` fetches 1000-bar
  chunks aligned to the epoch (`market/history.py`), cached once closed in
  `.data/candles/`; a run of missing bars over 1 h (edges and warm-up
  included) refuses the window (`backtest/window.py`: the API lacks it;
  `--max-hole` raises it for a thin token). `--scenario` (A35) runs the
  windows of `docs/scenarios.json` (or `--rolling`), each a fresh bucket
  valid until the window's end (`backtest_spec(until=)`, also for
  `--from`; the spec's own expiry is ignored there), measures each on the
  spec's pair and flags a tag it disagrees with (`TAG_CHECKS`,
  `backtest/scenarios.py`); a failed window is a row, not an exit.
  `--vary exit.stop.pct=3,5` (A36, `backtest/variants.py`; repeatable: the
  product; not `symbol`/`timeframe`/`market`) replays every variant (and
  every `--seed`) on each window, read once, and ranks them by the median
  return over the windows not tagged `holdout`, then the worst
  (`run_matrix`; its JSON keeps each cell's numbers, not the trades; cells
  run in processes, `--jobs`, default the CPU count, A37);
  `--write-best FILE` writes the winner's JSON. `--costs 2x` (fee and
  slippage) and `--shock -15%@50%` / `-10%~1h@50%` (A38,
  `backtest/stress.py`) add stresses, rows next to the base (ranking stays on
  the base; a variant that wins there and loses in some stress is
  `fragile`); `--volume-slippage` scales each bar's slippage by median
  volume / its volume (1x-10x). A perp refuses `--costs`/`--volume-slippage`
  (its engine has its own fee and impact). A
  non-stable input replays two candle series (the
  ratio, plus the quote's USD in `Tick.quote_usd`; a third column in tick
  CSVs) and measures equity in USD. Costs per leg: `fee_bps` +
  `slippage_bps` from the replay quote's output, and `network_fee_usd` as the
  leg's cost outside the fill, as live (A14: `ReplayExecutor` reports it as
  `fee_lamports`, so stops are measured from a live-like entry). Without `--fee-bps`/`--network-fee-usd` both are **measured on
  Jupiter now** (`trader/backtest/costs.py`, B9): half the loss of a buy+sell
  quote at the spec's trade size, and (5000 + `max_priority_fee_lamports`)
  lamports at the SOL price; the result has `measured_costs`. Pass both flags
  for an offline, repeatable run. Every result has `round_trip_costs`.
  A token's account rent is paid on its first buy and refunded when the
  replay ends flat, as `serve` does at expiry (A19: `rent_usd`,
  `rent_refund_usd`).
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
  `entry.mode`/`exit.mode` (`all`/`any`). `exit.partial` sells a part once
  per position (A12; a position with `partial_sells` doesn't reduce again,
  A20).
- Exactly one of `expires_at` or `ttl_days` (counted from the first tick).
- Add a condition type in four places: a model in `models.py` (with
  `lookback()`/`label()`), its union there (`EntryCondition` for entry-only
  blocks such as `below_last_exit`), a predicate in `conditions.PREDICATES`,
  and a row in `docs/specs.md`. Tests fail if any of them disagree. Most ideas
  fit an `expr` instead (`NAME(n, back)` reads an indicator `back` closed
  bars ago, A22: crosses; `trader/strategy/spec/expr.py`: `ast.parse` plus a
  whitelist walk and its own evaluator, never `eval`). An indicator is one
  entry in `INDICATORS` (`trader/shared/indicators.py`, with its bars in
  `lookback`/`history`, A17): the typed conditions and `expr` both read it.
  A recursive or weighted one can add a `PARTIAL` entry (A37: closed-bar
  state + one step, bit-identical; `test_indicator_split.py` checks it).
- Sizing is `fixed_usd` or `pct_of_bucket` (of what the bucket may spend now).
- Stops and exits never wait for the warm-up; only entries do. Warm-up is
  `spec.history()` (5x the period for EMA, 10x + 1 for RSI, capped at 900 bars).
  `BarSeries.seed(candles, until=clock())` fills candle gaps forward up to now;
  only a gap of more than `MAX_GAP_BARS` between live ticks resets it (A4). After an exit, an entry must turn false once (re-arm) before firing again.
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
  accounts, venues, ledger, policy, trading service); `trader/strategy/` the
  strategy-runner (bot, spec engine, remote client); `trader/shared/` what both
  import (models, the network-free feed `MarketData`/`HubMarketData`/pairs,
  the spec's terms and their policy checks, wire protocol, paths; the full
  spec language is in `trader/strategy/spec/`, B13); `trader/api/cli/` the commands;
  `trader/backtest/` both in one process. `execution` and `strategy` never
  import each other; `shared` imports neither. A model used by one side lives
  in that side's `models/`. Only the trade-runner talks to Jupiter: `connect`
  gets prices and its bucket (`tick` op) and warm-up candles (`candles` op)
  from `serve`.
- **Layering** (`tests/test_architecture.py`): every `trader/` module is mapped
  to a layer; a new module must be added to the map. Strategy code and the
  strategy side (`trader/strategy/`) never import execution, venue or risk.
- **One strategy type**: the bot and the backtester take the `Strategy` protocol
  (`trader/strategy/bot/config.py`), which `SpecStrategy` implements, and share one
  per-tick decision (`trader/strategy/bot/decision.py`).
- **One seam to the venue** (A7): `trade/gateway`, `trade/accounts` and
  `trade/trading_service` take a `Venue` (`trader/execution/models/venue.py`;
  today `SpotVenue` around the Jupiter provider, built in `wiring`/the
  backtest) and accounts are `BucketAccount`s (`trade/accounts/`: `SpotAccount`,
  `PerpAccount`); they never import `trade/venues` (tested). A23: a `Venue`
  is a `Wallet` (balances, fee reserve, token accounts; what
  `WalletBalances.source`) plus `PostTrade` and the swap; both accounts derive from
  `BaseAccount` (`accounts/base.py`), and `PerpAccount` takes only the
  `WalletBalances` and its `PerpVenue`, never the spot `Venue`; the
  service's perp side is `PerpDesk` (`trading_service/perps.py`: it builds
  and keeps the perp accounts, reconciles, sweeps and tops up).
  Tests reach the mock provider with `spot_provider(...)` (`tests/factories.py`).
  The ids of `docs/examples/` are pinned (`test_example_ids.py`): a new spec
  field with a default must stay out of `canonical_json`.
- **One path to a swap**: `TradeGateway.submit` (idempotency -> policy -> ledger
  -> execute), called only by `trader/execution/trade/gateway/fills.py::execute_trade`.
- **Before a real swap is sent** (B7): the quote must be within 2% of the Price
  API value (`max_quote_deviation_pct`, on in `trader/execution/wiring.py`; buys fail
  closed, sells only warn), and `OnChainExecutor` inspects the signed
  transaction (`tx_inspection.py`): allowed programs only, and a simulation
  returning the wallet's accounts where only the input leaves, up to
  `inAmount`. Both refusals are `SwapRejectedError`s. Test fakes of the RPC
  need `inspection_passes()` from `tests/factories.py`.
- **Nothing raises after EXECUTED**: `fills.settle` (from `execute_trade` and
  the A3 resolver) is the only caller of `PostTrade.fetch_costs` (the spot
  `Venue` or the `PerpVenue`; never
  raises), fills are written with `record_fill_safely`, build orders with
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
  the result or the error), and a single send that failed (a perp request,
  stop or cancel: no retries) carries its own on the
  `TransactionFailedOnChainError` (`failed_signatures_of`, A24);
  `execute_trade` books their fee on both paths (`failed_tx_fee` event +
  `PositionBook.charge`) and never raises doing it.
  Paper fills 10 bps below the quote (`SimulatedExecutor.slippage_bps`).
- **Priority fee (B9)** is one owner setting, `max_priority_fee_lamports` in
  `policy.toml` (default 100,000): real sends it to Jupiter as the
  `maxLamports` cap (`priority_fee()` in `client.py`), paper
  charges it in full on every leg, the backtest prices it into
  `network_fee_usd`. `wiring.build_trade_service` loads the policy once for
  the provider and the gateway.
- **Cost per round trip** (`RoundTripCosts`, `Ledger.round_trip_costs`): per
  closed position, entry spend x (exit tick price / entry tick price - 1)
  minus the net realized PnL, in USD and bps, less rent refunds in the window.
  It can be negative when fills beat the tick. The rent of a perp position
  account is left out and reported apart (`account_rent_usd`, A25): Jupiter
  never closes it and reuses it, so it is a one-time deposit; paper (the
  wallet's `perp_accounts`) and `ReplayPerpsVenue` charge it
  (`DEFAULT_PERP_POSITION_RENT_LAMPORTS`) only on the first open of a market
  and side, as real does.
- **Rent comes back on retirement (A15).** The first buy of a token pays its
  account's rent (a cost). When the bucket is retired and flat, the `serve`
  sweep closes the empty account (`TradeService.close_token_account`; never SOL,
  never while another ACTIVE bucket or any open position uses the token, never
  an account no fill of the mode paid for, never past the swaps' send gate:
  `real_trading_enabled`, unresolved intents, breaker) and credits the bucket that paid
  (`rent_refund_sent` before the send, `rent_refund` after; events, no schema
  change). Shown by the backtest, the daily report,
  `live_vs_backtest.py` and `ledger_dump.py`.
- **Perps (A8).** A spec with `market` (`PerpMarket`,
  `trader/shared/spec/terms.py`) gets a `PerpAccount`
  (`trade/accounts/perp.py`) on the mode's `PerpVenue`
  (`SimulatedPerpsVenue`, `trade/venues/paper/perps.py`; real: below). A perp leg rides the spot path: `TradeIntent.perp` (`PerpTerms`, ledger
  `instrument`/`perp_json`), `ExecutionResult.perp`/`Order.perp` (`PerpFill`,
  `trader/shared/models/perp.py`; `quantity` = size in base, `quote_amount` =
  collateral posted/returned, so PnL = back - posted); `Position.direction`
  comes from the entry's `perp`. One position per market and side; a sell
  smaller than the position (past the 1% dust rule) closes that part (A12).
  Exposure (collateral x leverage) is the notional the policy sees
  (`perps_enabled`, `max_leverage`, `allowed_perp_markets`). What each intent
  side means is declared on `IntentSide` (`spends`, `moves_position`,
  `is_trade`; `execution/models/intent.py`): `STOP`/`CANCEL` (a venue stop
  placed or cancelled, send rules only, A12/A20) and `ADD` (collateral
  added, budget rules; the entry grows, `PositionBook.add`). `Position`
  counts `partial_sells` and `top_ups` since the entry (A20). The sweep's
  `check_perps` reads the venue once (`PerpVenue.sweep`), books the exits it
  made (liquidation or its stop: `Ledger.record_external`, outside the
  policy, then `PerpVenue.acknowledge`), then adds collateral per
  `market.add_collateral`. A stale venue price refuses a buy before the
  intent (`PerpVenue.check_fresh`). One position per market and side is checked at the
  buy (`PerpVenue.has_position`), before any intent; at the first perp open,
  `reconcile_perps` (`execution/models/reconcile.py`) checks the venue's
  positions against the ledger's in one read (`perp_mismatch`; run by
  `PerpDesk`), and a market the venue has and the ledger doesn't blocks buys
  there (checked first in `PerpAccount.buy`). The resolver
  settles perp intents through `PerpVenue.resolve_send`. `backtest` replays perps
  through `ReplayPerpsVenue` (A9; `Policy.unlimited()` allows them;
  `round_trip_costs` measures a perp on its exposure and direction; paper
  and the replay fire the venue stop too).
- **Ledger schema** is one `_SCHEMA` with a `user_version` in
  `trader/execution/trade/ledger/store.py`. There are no migrations: an older file is refused
  (`LedgerFormatError`); bump `SCHEMA_VERSION` when the schema changes.
  Event types are names in `trade/ledger/events.py` (A16): add one there,
  never as an inline string, and never rename one (old ledgers keep it).
- **UNCONFIRMED/EXECUTING** (process killed mid-swap) blocks all trading in
  that mode until resolved (A3, `trader/execution/trade/gateway/resolve.py`):
  every send is an `intent_sent` event written before the broadcast (the
  gateway's `send_hook`, `announce_send` in the executor; if that write
  fails, nothing is sent), and `serve` asks the chain (or the paper wallet's
  `applied` log) about each one at start and on every sweep. Only an intent
  with a send still PENDING, or one from a build without the send log, keeps
  blocking; then the owner checks the chain. The circuit breaker counts
  failures since the process started (a restart re-arms it); provider
  rejections (`SwapRejectedError`, stored as REJECTED) don't count.
- **RPC commitment is Confirmed** everywhere (reads, simulation, confirmation;
  the one exception is the block height that decides a send expired, A3);
  Finalized lags ~13s and would show pre-swap balances.
- **State paths** go through `trader/shared/paths.py` (`TRADER_DATA_DIR`,
  `TRADER_POLICY_FILE`, `TRADER_LOG_DIR`; relative values resolve against the
  project root). Never a bare `Path(".data")`.
- **Policy** (`policy.toml`, untracked; model `policy.example.toml`): real mode
  is off until `real_trading_enabled = true`. Paper has roomy limits by default
  (`PAPER_DEFAULTS`); `[limits]` applies to every mode, `[paper.*]`/`[real.*]`
  only to theirs. `max_trades_per_hour` is wallet-wide;
  `max_trades_per_hour_per_bucket` caps one spec. A denial by a rolling
  limit (daily notional, trades per hour, daily loss) carries `retry_after`
  (A29: `_WINDOWS` in `policy.py`, the oldest counted record + the window -
  now), and the bot pauses that side's orders until then (at most 1 h);
  other denials pause 30 s (`denial_cooldown`).
- `trader/__init__.py` stays empty (every `import trader.x` loads it).
- Windows console is cp1252: no non-Latin-1 symbols in log/CLI output (use `[!]`).

## Tests
- `asyncio_mode = "auto"`; `-W error::ResourceWarning` (close the `Ledger`s you open).
  One event loop for the whole session, and `tests/conftest.py` overrides
  `tmp_path` with a counter-named dir: on Windows a loop per test (~9ms) and
  pytest's numbered `tmp_path` (~18ms) cost more than most tests.
- The autouse `isolated_workdir` fixture points the three `TRADER_*` paths at
  `tmp_path` and chdirs there. Write test policies with `policy_file().write_text(...)`.
- Test file basenames are unique across `tests/` (no `__init__.py`). Shared
  helpers are in `tests/factories.py` (`make_intent`, `make_spec`,
  `open_ledger`, `memory_gateway`, `mock_provider`, `StubStrategy` for bot and
  backtest fakes).
- Tests patch module paths such as `trader.strategy.runner.TradingBot`
  and swap `trader.api.cli.backtest.CANDLES` for a fake.
- Ruff: line length 88, mccabe `max-complexity = 5` (split functions rather
  than suppress). Pyright basic on `trader`, `tests`, `main.py`.

## Jupiter
- Quotes/swaps: `api.jup.ag` (`JUPITER_API_URL` overrides; `JUPITER_API_KEY` is
  sent as `x-api-key`, optional). Keyless requests hit `429` under bursts;
  HTTP calls retry with backoff (`HTTP_RETRY`, pre-broadcast only).
- The price websocket (`trench-stream.jup.ag`, several mints per subscription)
  and candles (`datapi.jup.ag`) are undocumented frontend endpoints; the hub
  backs prices with the Price API V3, candles have no fallback.
- Jupiter Perps (A10, read-only): `trader/execution/market/perps/` decodes the
  program's accounts by its Anchor IDL (`perpetuals_idl.json`, a trimmed
  community copy; `idl.py` is the Borsh reader) over JSON-RPC
  (`HELIUS_RPC_URL`, else the public RPC; never logged). The borrow rate is
  the custody's jump-rate curve; the price Jupiter uses is the Doves
  aggregated feed; a position is a PDA. Paper opens at the live borrow rate
  of the custody it borrows from (`collateral_mint`: USDC for a short, A18).
  If `tests/live/test_live_perps.py` fails, the IDL may have changed.
  A11a: `encode.py` builds Anchor instructions from the same IDL, and
  `trade/venues/jupiter_perps/requests.py` builds the open (size 0: add
  collateral), partial or whole close, venue-stop and cancel
  (`closePositionRequest2`, from the program's on-chain IDL: the community
  copy's `closePositionRequest` is gone) requests (counter = sha256 of the
  idempotency key); the live suite simulates them on mainnet. A11b:
  `JupiterPerpsVenue` signs and sends them through
  `OnChainExecutor.send_instructions` (the spot path: program allowlist +
  perps, one balance simulation that also sets the compute limit, A12's
  `compute_budget.py`, send logged first with a typed `SentTx.perp`), waits
  for the keeper (60 s), and is given to `serve real` only with
  `perps_enabled` in `[real]`. Custody and Doves price reads go through one
  cached `PerpsFeed` (`market/perps/feed.py`); an open refuses a Doves price
  older than 30 s. The venue stop is its own intent (`{key}:stop`); one left
  after a close is cancelled (`{key}:cancel`). The keeper removes it itself
  after a close, so `stop_left` reads it every 2 s for up to 30 s before
  calling it left, and a cancel that fails re-reads it once (`wait=False`):
  gone is no leftover (A24); `perp_stop_left` only if it is still there.
  Venue exits (its stop fired, a liquidation) are booked from the
  keeper's USDC payout in the sweep. A close's fill splits what the
  position was worth at the oracle less what came back: `borrow_usd` is
  the borrow it owed, read before the close, and `fees_usd` the rest (A27;
  an unread custody leaves it all in the fees and never blocks the close).
  With a position open, the bucket line and `bucket_line` add the entry's
  costs (`entrada: custos ... SOL (rent ...), taxas perp $...`): the
  bucket's summary counts them only at the close. `connect` logs one line
  per bar and the bucket line only when it changes (A32). A31: a closed perp's
  `PnLResult` carries `perp_fees_quote` (the close's `fees_usd` + the
  open's share of posted - collateral kept, `venue_costs`) and
  `perp_borrow_quote`; `PositionBook` and `AccountPnL` (from the legs'
  `order_json`, the entry followed through adds and partial sells) sum
  them, and `bruto` reads `(preço P, taxas perp -F, empréstimo -B)` with
  P = bruto + F + B. `connect`'s SELL line splits that close
  (`round_trip_line`); the position line shows the price move and the
  return on the collateral (`Position.return_on_collateral`), and
  `unrealized_usd(price, now)` takes the accrued borrow. Costs outside the collateral (A11c,
  A12): a request's fee and the rent of a position account it created come
  from its transaction's `meta`, filled or rejected (Jupiter keeps the
  account, nothing comes back), and a venue order's send fee is a
  `perp_stop_fee` event (in `pnl_totals`, the round-trip cost and the
  bucket's budget).
- **Every SOL cost is in the native PnL (A30).** A cost outside the legs (a
  venue stop's fee, a failed send's, minus a rent refund) goes into the
  book's `costs_sol` and `net_quote` as well as `realized_usd`
  (`PositionBook.charge_outside`; `AccountPnL._outside` rebuilds the same
  from the events). In the quote token it is exact for a SOL quote and a USD
  stable (`outside_cost_quote`); any other quote counts it as incomplete
  (`[!]`). `tx falhas`/`rent devolvido` in the summary are parts of
  `custos`, not extras.
