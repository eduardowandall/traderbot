# Plan

Status as of 2026-09-30. This is the only roadmap. It replaces the earlier
agent-readiness plan, `agent-strategies.md` and `refactoring-backlog.md`;
everything still open from them is carried over below. For how the code works
today, read [`architecture.md`](architecture.md).

Track progress in §6 and record decisions in §10. Plan here first, then
implement.

---

## 1. End goal

1. **Strategies are spec files written from the docs.** The owner or an
   agent writes a JSON spec following [`specs.md`](specs.md), backtests it,
   runs it in paper, and iterates on the file and on the code. Nobody needs a
   command to create, submit or register a strategy.
2. **Manual trades are possible but rare.** They go through the same gateway,
   policy and ledger as strategy trades.
3. **A structured way to build strategies:** declarative specs made of vetted
   blocks, validated and backtested before they run. No free-form code.
4. **One wallet, one bucket per strategy.** Each strategy runs in its own
   sub-account with its own budget, position and PnL, and buckets can never
   spend each other's funds.
5. **Every trade and cost is accounted for as accurately as possible:** real
   amounts, network fees, rent and failed-transaction fees, net PnL per bucket,
   and a ledger that can be reconciled against the chain.

## 2. Where we are

**Overall: about 76% of the end goal** (goal 1 was redefined on 2026-09-30:
specs are files written from the docs, not submitted through agent commands;
67% before stage U, 58% after the first recheck). The foundations (safety, ledger, execution seam,
spec format) are in place and the order path is short. What is missing is
mostly the part that makes strategies *live*: storing them, running many at
once, and approving them for real money.

| Goal | Done | Missing | Score |
|---|---|---|---|
| 1. Strategies are spec files written from the docs | Spec format; `docs/specs.md` is the authoring contract (a test keeps it in step with the code); the trade-runner validates the spec's terms against the mode's policy and runs it in its own capped bucket with a max-loss stop (`serve` + `connect`, B14); `backtest` prints JSON | Running several spec files at once (B3) | **70%** |
| 2. Manual trades | Removed in stage U (no `swap` command) | Comes back with B2's wallet allocation | deferred |
| 3. Structured strategy building | Spec v1: 18 condition types plus a restricted `expr`, `fixed_usd` and `pct_of_bucket` sizing, any registry token as the input (prices in the quote token, money in USD), required stop, converged warm-up, re-arm after exits, `ttl_days`; backtests replay direction-aware OHLC paths with slippage and refuse too little data; feed gaps cool the spec down; cooldown, re-arm and expiry survive restarts | No crossovers (an `expr` compares levels on the current bar, not across bars); ratio candles approximate the bar's high/low | **88%** |
| 4. One wallet, bucket per strategy | Buckets with budget and max-loss caps; one bucket per spec id; per-bucket ledger account; partial exits kept per bucket; atomic authorization across processes | One strategy per process; no wallet treasury (a bucket without a budget can spend another's tokens, B2); no aggregate reconcile | **50%** |
| 5. Accurate trades and costs | Real amounts and fees from the confirmed tx; rent; net PnL; USD values on every pair; fills never lost after EXECUTED; partial sells keep their cost basis; fees of failed transactions booked per bucket; mark-to-market and a daily report; paper fills below the quote; replays pay a network fee; live vs backtest comparison | Replay network fee is a flat USD estimate and ignores rent; `Order.timestamp` is naive local time (B7) | **94%** |
| (Foundations: safety, tests, layering) | Policy, breaker, idempotency, UNCONFIRMED blocking, transaction inspection before signing, quotes checked against the Price API, no decisions on stale prices, 717 tests, enforced layering | Key still shares a process with strategies under `run`; no owner approval for large trades | 90% |

The overall figure is the plain average of goals 1, 3, 4 and 5 (goal 2 is
deferred).

### 2.1 What has been built

Stages A, R, S, U and T are done; their designs and progress notes are in
[`history.md`](history.md).

## 3. Target architecture

```
 owner or agent: writes docs/examples/<spec>.json from docs/specs.md,
                backtests it, runs it in paper, iterates on the file and the code
                 |
 owner starts:  strategy-runner (spec A)   strategy-runner (spec B)   <- no key, no ledger, no mode
   MarketData (read-only)     MarketData (read-only)
   SpecStrategy               SpecStrategy
   TradeClient --+            TradeClient --+
                 |  hello / get_bucket / submit_order / heartbeat
                 v                          v
        trade-runner (one per mode; the ONLY process with key, wallet and ledger)
          TradeService: one bucket per spec
            -> bucket check (spend cap = budget - losses)
            -> TradeGateway.submit (idempotency -> policy -> ledger -> execute)
            -> Executor (on-chain, or the shared SimulatedWallet in paper)
          owns: retire / expiry / max_loss exits, aggregate reconcile, notifications
```

### 3.1 Principles

- **Strategies are files.** Agents (and the owner) write spec files from
  `docs/specs.md`; they never trade, touch the key or edit the policy, and there
  is no agent-facing command. The owner starts every run.
- **The policy decides deterministically.** `evaluate()` is pure and owned by
  the owner. It stays wallet-wide; per-strategy budgets are enforced by the
  bucket.
- **Execute exactly once.** One broadcast per idempotency key; nothing is
  retried after a send.
- **The ledger is the source of truth** for positions, PnL, budgets and the
  audit trail, and is reconciled against the wallet.
- **Fail closed.** No ledger or a broken policy means no trade.

### 3.2 Buckets

- A bucket is `account_id = "<mode>:<name>"`: `strategy:<id>` for a spec.
- With no open position a bucket may spend
  `max(0, budget_usd + min(0, realized_usd))`. Buckets are long-only, one
  position at a time.
- The bucket check runs before `policy.evaluate`.
- The trade-runner refuses (at `hello`) a spec whose sizing is above the mode's `max_trade_usd`
  (otherwise every buy would be denied). With B2, the budgets of the open
  buckets must fit the wallet.
- **Exits don't depend on the strategy process.** On retire, expiry or
  `max_loss_usd`, the trade-runner sells the bucket's position itself. Sells
  skip budget rules.

### 3.3 Wire protocol (JSON lines on `127.0.0.1`, token auth)

One request, one reply, one JSON object per line. TCP because asyncio on
Windows has no Unix sockets. The trade-runner writes `{port, token, pid}` to
`data_dir()/trader-<mode>.json` (removed on exit); the token keeps other local
processes out, and the guard hook keeps agent sessions from reading the real
one.

| Request | Reply | Notes |
|---|---|---|
| `{"op": "hello", "token", "terms"}` | `{"ok": true, "bucket"}` or `{"ok": false, "error"}` | Validates the spec's terms (`SpecTerms`, B13: id, name, symbol, budget, max loss, largest buy, expiry; decimals as strings) against the trade-runner's policy and opens (or re-attaches to) bucket `strategy:<id>`; rejects a bad token or a second live connection for the same spec |
| `{"op": "bucket"}` | `{"ok": true, "snapshot"}` | `BucketSnapshot` (position with its orders, PnL, status) |
| `{"op": "submit", "request"}` | `{"ok": true, "reply"}` | `OrderReply` (filled / denied / rejected / error); the client always sends an idempotency key, so a resend after a reconnect executes once |
| `{"op": "price", "mint"}` | `{"ok": true, "price", "age"}` | The trade-runner's price hub (B7): USD price and its age in seconds; a price older than 30s is an error, never a value |
| `{"op": "candles", "mint", "interval", "qty"}` | `{"ok": true, "candles": [...]}` | Warm-up candles read by the trade-runner (B12): `qty` 1–1000, `interval` an `Interval` value; each candle `{timestamp, open, high, low, last}`, decimals as strings |

The strategy-runner finds the trade-runner with `--trader FILE` or, by
default, the only `trader-*.json` in `data_dir()`. It has no mode argument.

### 3.4 Layers

Every module belongs to one layer, and `tests/test_architecture.py` fails on
a disallowed import or an unmapped module. The table is in
[`architecture.md`](architecture.md) §2. The rule that matters most:
**strategy and strategy-side code never import execution, venue or risk.**

Planned modules and their layers: `trader/strategy/trading_service/remote.py`
(`RemoteTradeClient`, strategy-side), `trader/strategy/runner.py`
(strategy-side), `trader/execution/runner.py` (app).

### 3.5 Commands

Owner commands only; specs are created as files, never through a command.

| Command | Purpose |
|---|---|
| `run <mode> spec.json` | Run one spec (done) |
| `backtest spec.json` | Replay a spec on candles or recorded ticks, JSON result (done) |
| `serve <mode>` | The trade-runner: key, wallet, ledger, one bucket per connected spec (B3) |
| `connect spec.json [--trader FILE]` | A strategy-runner for one spec file, no key (B3) |

## 4. Decisions that stand

| Topic | Decision |
|---|---|
| Strategy form | A **declarative JSON spec** from vetted blocks. A restricted expression language comes later as one more condition type. Never agent-written Python, never `eval`. |
| Interface | **Files and docs.** Specs are JSON files written from `docs/specs.md`; agents don't use a CLI. The owner runs `run` and `backtest`. |
| Mints | **Owner registry only** (`SOLANA_MINTS` ∩ `allowed_symbols`). |
| Activation | **The owner starts every run**, paper or real; real also needs `real_trading_enabled`. |
| Processes | One execution process per mode (`run` or `serve`, OS file lock); one strategy-runner per spec connected to `serve`. Strategies are mode-agnostic. |
| Budgets | Per-strategy budget in the bucket; `policy.evaluate()` stays wallet-wide. |
| Spec storage | **Files in the repo** (`docs/examples/`); the id is the hash of the behaviour, so each version gets its own bucket. |
| Wallet | One wallet for all strategies for now. Real mode uses a dedicated low-balance hot wallet. |

## 5. Roadmap

The refactoring stages (A, R, S, U, T) are done and archived in
[`history.md`](history.md); what is left is stage B. Each item ships on its own with the suite green (`ruff
check`, `ruff format --check`, `pyright`, `pytest`). Size: **S** under an hour,
**M** several modules, **L** a design change.

### Stage B — Features

Stages A, R, S, U and T are done ([`history.md`](history.md)). The order
below was set on 2026-09-30, after strategies became spec files written from
the docs:

1. **B4 first**: it is small and it is the one item that guards real money.
   Agents now only write spec files, so a hook that stops an agent session
   from starting a real run or reading the key is the cheapest protection
   before the first real run.
2. **The owner's paper soak** (a week of `run paper` on a few specs).
3. **B3 with B2**: several specs at once, the key in one process, and the
   wallet checks that only matter once buckets share a trade-runner. Today
   every spec has a `budget_usd` and a stablecoin input, so buckets can't spend
   each other's tokens; the worst case is a buy failing for lack of funds.
4. **B5**, then **B6**, then **B8** (perps), then **B7**.

#### B4. Real-mode guardrails for agent sessions — S (next)
Claude Code rules and hooks are guardrails for agent sessions, not a sandbox:
the owner still starts real runs from a terminal, on a dedicated low-balance
hot wallet. Design:
- **`.claude/settings.json` deny rules:** `Read` of `.env` (not
  `.env.example`); `Edit`/`Write` of `.env` and `policy.toml`.
- **`.claude/hooks/guard_commands.py`**, a PreToolUse hook on the Bash and
  PowerShell tools, blocks a command (with the reason shown to Claude) when
  it:
  - starts real mode: `main.py run real ...`, or any `--env-file` (it loads the
    key);
  - points the policy elsewhere: `TRADER_POLICY_FILE`;
  - reads or changes the key file from the shell (`.env`, but not
    `.env.example`);
  - touches the real ledger file (`ledger-real.sqlite3`: `sqlite3`, `rm`,
    `mv`, `del`, `Remove-Item`...), which is also how UNCONFIRMED intents are
    cleared.
  Paper runs, backtests, `ledger_dump.py`, and deleting the paper ledger or
  wallet stay allowed.
- **Pure checker + tests:** the hook is a pure `blocked(command) -> reason |
  None` plus a thin stdin/stdout wrapper; `tests/test_guard_commands.py`
  covers every rule, the allowed commands above, quoting and PowerShell
  syntax.
- **Docs:** `AGENTS.md` (what agents can't do and why) and README (the owner
  runs real mode in a terminal).

#### Owner task: a one-week paper soak
Run a few specs from `docs/examples/` with `run paper` for a week; check them
with `ledger_dump.py` and `/diagnose`. Anything that goes wrong becomes an
item here.

#### B3. Trade-runner and strategy-runners — L (with B2)
Each spec runs as its own **strategy-runner** process (no key, no ledger, no
mode) talking to one **trade-runner** per mode, the only process with the
key, the wallet and the ledger (§3.1–3.3). Design:
- **One execution process per mode.** `trader/api/cli/lock.py`: an OS file
  lock on `data_dir()/trader-<mode>.lock` (`msvcrt.locking` / `fcntl.flock`,
  released by the OS if the process dies), taken by both `run` and `serve`.
  Two `run` processes in the same mode are no longer possible; several specs
  at once go through `serve`.
- **Wire codec** `trader/shared/trading_service/wire.py` (core): `BucketSnapshot`,
  `OrderRequest`, `OrderReply` and `Position` to and from JSON, reusing the
  `Order` codec (`order_to_json`); decimals as strings.
- **`trader/execution/runner.py`** (`main.py serve <mode>`, app):
  1. takes the lock, builds the `TradeService` (`wiring.build_trade_service`),
     runs the aggregate reconcile (B2), serves on `127.0.0.1:<free port>` and
     writes the connection file;
  2. `hello`: parse and validate the spec against **its own** policy (what a
     strategy-runner checked is advisory), check the allocation (B2), open the
     bucket once and keep it open across reconnects;
  3. every 30s, sells what is left in buckets that are retiring or whose spec
     expired, at the Price API price (exits don't depend on the
     strategy-runner);
  4. on exit (Ctrl+C), removes the connection file and closes the ledger.
  Notifications stay with the strategy-runner's bot (one per fill, as today).
- **`trader/strategy/trading_service/remote.py`** (strategy-side): `RemoteTradeClient`,
  a `TradeClient` over the socket. It fills a missing idempotency key before
  sending, and on a dropped connection reconnects (backoff 1s -> 30s),
  re-sends `hello`, and resends the pending request with the same key.
- **`trader/strategy/runner.py`** (strategy-side):
  `main.py connect spec.json [--trader FILE] [--seed N]` builds the same bot as
  `run` with a `RemoteTradeClient`; it needs no key and no RPC.
- **Tests:** codec round trip; socket round trip through a paper trade-runner;
  bad token, invalid spec and a second live connection rejected; a resend
  after a dropped connection executes once; the sweep sells a retiring
  bucket's leftover once; the lock refuses a second process; `connect` works
  with `SOLANA_PRIVATE_KEY` and `HELIUS_RPC_URL` unset.

#### B2. Wallet allocation and reconciliation — M (with B3)
Only meaningful once one process holds every bucket of a mode (B3's lock).
- **One balance cache per `TradeService`**, shared by its accounts and
  invalidated on every fill (today each account caches the wallet for up to 3
  minutes, so bucket A doesn't see what bucket B just spent).
- **Allocation:** opening a bucket fails when the budgets of the open buckets
  plus the new one exceed the wallet's quote balance (spendable), so budgets
  always fit the wallet.
- **Aggregate reconcile at startup:** for each token, the sum of the open
  positions of every bucket in the ledger must be at most the wallet balance
  (1% tolerance). A shortfall records a `reconcile_mismatch` event and blocks
  buys of that token in this process until the owner checks the wallet.
- **Report:** `ledger_dump.py` adds each open bucket's position and the wallet
  so the allocation can be read in one place.

#### B5. Accounting and performance — M
- Mark-to-market: unrealized PnL per bucket at the current price (in the
  report and the notifications).
- A daily report (fills, costs, PnL per bucket) through the notifier.
- Live vs backtest: record ticks with `--record-ticks`, then `backtest --ticks`
  on the same window and compare with the bucket's PnL.
- Closer simulated fills: a slippage model for paper, and the network fee in
  replays.
- The fee of a transaction that failed on-chain (retried since the gap review)
  is paid but not recorded: read `meta.fee` for its signature and book it as a
  cost of the bucket.

Design (2026-10-03). No ledger schema change (paper ledgers from the soak keep
opening):
- **Mark-to-market** (core): `Position.unrealized_usd(price)` = value at the
  price minus the entry's USD cost and its known costs (what selling now would
  realize, before the exit's own costs). The bot's tick log and the fill
  notification add the bucket's PnL summary and, with a position, the
  unrealized value; the daily report prices every open position with the
  Price API.
- **Daily report** (`trader/execution/notification/daily_report.py`, app):
  `DailyReporter` runs next to the execution process (`run` and `serve`, which
  own the ledger). Every minute it checks whether the previous UTC day was
  reported (a `daily_report` event with the day in the ledger, so restarts
  don't resend); if not, it builds per bucket of the mode: fills that day,
  costs paid (SOL and USD), realized PnL that day and in total, failed-tx
  fees, the open position with its unrealized PnL, and sends it through the
  notifier. Nothing to report (no fills, no positions): nothing is sent, the
  day is still marked. `Reports.pnl_totals(account, start, end)` does the
  windowed sums (it also sums `costs_usd`).
- **Paper slippage** (venue): `SimulatedExecutor(slippage_bps=10)` fills
  `outAmount x (1 - bps)` but never below the quote's `otherAmountThreshold`,
  so paper records the quoted and the actual amount like real mode
  (`costs.slippage_raw`). The backtest's executor uses 0 (its slippage is in
  the replay quote).
- **Network fee in replays** (app): `Backtester(network_fee_usd=...)`; the
  replay quote takes it out of each leg's output (buy: `fee / price` tokens;
  sell: `fee` USD), so equity and realized PnL both pay it. `backtest
  --network-fee-usd` (default `0.002`: the base fee plus a small priority fee
  at ~$200/SOL; take a better value from your own ledger's `fee_lamports`).
  The one-time, refundable rent of a token account is not modelled.
- **Failed-tx fees** (venue -> execution -> risk): the provider's retry loop
  collects the signatures of attempts that raised
  `TransactionFailedOnChainError`; they ride on the `SwapResult`
  (`failed_signatures`) or on the error that ends the loop (`SwapFailedError`,
  a `RuntimeError`, for exhausted retries; `SwapRejectedError` and
  `TransactionSubmittedError` carry them too). `execute_trade` reads each
  `meta.fee` (`provider.fetch_failed_fees`, never raises, bounded by a
  timeout), values it with the SOL price of the pre-trade snapshot, and books
  it on both paths: a `failed_tx_fee` event (account, intent, signatures,
  lamports, USD), and `PositionBook.charge` (realized USD minus the fee, so the
  budget and `max_loss_usd` see it). `pnl_totals` subtracts these events from
  `net_usd`, so a restart restores the same number. A booking failure is
  logged, never raised.
- **Live vs backtest** (`trader/backtest/compare.py`, app, and
  `.claude/scripts/live_vs_backtest.py <spec> --ticks FILE [--mode paper]`):
  replays the recorded ticks the way the live bot saw them: the strategy is
  warmed up first with the candles that closed before the first tick
  (`Backtester(warmup=...)` calls `strategy.setup`, like the bot's startup),
  then trades on the ticks. It prints, as JSON, the backtest's trades and PnL
  next to the bucket's executed legs and realized PnL in the same window, and
  the differences (trade count, PnL, fill prices). It reads the ledger
  read-only.
- **Tests:** unrealized PnL (with and without costs/USD rates); the report's
  windowed sums, its text, idempotency across a restart and the empty day;
  the paper fill below the quote but not below the threshold; the replay fee
  lowering PnL; a failed-then-filled swap and an exhausted one booking the fee
  (event, book, restore); the comparison on a fake market and an in-memory
  ledger.

#### B6. More expressive strategies — M
- Sizing as a percent of the bucket.
- The `expr` condition type: a restricted parser that compiles to the same
  comparison nodes.
- Non-stable inputs for specs and backtests: prices in input-token units
  (`price(out) / price(in)`), budgets converted to USD with the Price API, and
  two price series in replays.
- A new condition type is four edits: the model and its union in
  `strategy_spec/models.py`, the predicate in `conditions.PREDICATES`, and a
  row in `docs/specs.md` (tests check all four).

Design (2026-10-04). No ledger schema change; existing USDC/USDT specs trade
and backtest exactly as before (same ids, same numbers):
- **Units rule.** The strategy works in the pair's **quote token** (the
  input): the price it sees is `price(out) / price(in)`, its balance is what
  the bucket may spend now in quote units, and positions are judged on
  `Order.fill_price` (quote per token). For USDC/USDT these are the USD
  numbers they are today. `Order.price` stays USD per token for the ledger
  and reports. Budgets, max loss, `fixed_usd` and the policy stay in USD;
  the bucket converts with the quote's USD price.
- **Percent sizing** (`models.py`): `sizing` is a union of `fixed_usd` and
  `{"type": "pct_of_bucket", "pct": 1-100}`, a percent of what the bucket may
  spend now. `validate` refuses `pct x budget_usd` above `max_trade_usd`.
- **`expr`** (`trader/strategy/spec/expr.py`, strategy layer):
  `{"type": "expr", "expr": "rsi(14) < 30 and price < sma(20) * 0.98"}`. Python's
  `ast.parse` reads it and a whitelist walk (never `eval`) accepts only:
  numbers, `price`, `entry_price`, `peak`, `last_exit_price`, the indicator calls `sma/ema/wma/rsi/high/low/
  volatility(n)` (n an integer 2-500), `+ - * /`, unary minus, one comparison
  (`< <= > >=`) per operand pair, `and/or/not`, parentheses. Max 200
  characters. Parsed when the spec is validated (a bad expression
  is a spec error with its position); the text is stored normalized, so
  whitespace doesn't change the spec id. A missing value (indicator not warm,
  no position, division by zero) makes the comparison false, like every
  other condition. `lookback()`/`history()` come from the calls in it. Allowed
  wherever market conditions are (entry and exit).
- **The strategy's price feed** (`trader/shared/market/pair.py`, market layer):
  `market_for(symbol, factory)` returns the usual `MarketData` for a USDC/USDT
  quote, else a `PairMarketData` over two feeds (one websocket per mint):
  `get_price(token)` = token USD / quote USD, `get_candles` = the two candle
  series divided bar by bar (matching open times; high/low widened to cover
  open and close). The bot, `connect` and `backtest` get it from the CLI, so
  the bot itself doesn't change.
- **Strategy protocol:** `on_market_refresh(price, balance, position,
  quote_usd=1)`; `order_for` passes the snapshot's `quote_usd` (`fixed_usd`
  needs it). `BucketSnapshot.available_usd` becomes `available` (quote units)
  plus `quote_usd` (None when unknown: the bucket then offers 0, fail closed).
- **Execution:** `TradeService` converts the USD cap to quote units with its
  price oracle (`get_bucket`, `buy(limit=...)`, the allocation check); a
  non-stable quote without a price can't open a budgeted bucket. A sell's
  notional is `quantity x price x quote_usd`. `order_from_fill` values the
  token as `quote_usd x fill_price` (the signal price is in quote units now).
  The trade-runner's exit sweep prices leftovers as token USD / quote USD.
- **Replays with two series:** `Tick.quote_usd` (None for USDC/USDT); candles
  of both mints make ratio ticks carrying the quote's USD close; recorded
  ticks get a third CSV column when the quote isn't a stablecoin (the bot
  passes `snapshot.quote_usd` to `on_tick`). The `Backtester` funds the
  wallet with `budget_usd / quote_usd` of the quote, gives the service a
  replay price oracle (quote and token USD from the tick, so realized PnL is
  in USD), takes the network fee in USD, and measures equity in USD.
- **Validation:** the input may be any registry token except the output; the
  output still can't be a stablecoin (long-only).
- **Tests:** percent sizing and its limit; the parser (grammar, precedence,
  errors with positions, normalization, depth/length limits, lookback/
  history) and evaluation against the same predicates the typed conditions
  use; a JUP-SOL spec end to end in a replay with two series (USD budget and
  PnL); the pair feed and ratio candles; the bucket cap in quote units; the
  sweep's pair price; every existing USDC number unchanged.

#### B9. Costs that agree across real, paper and backtest — M
From the soak ([`soak-test.md`](soak-test.md) F7): a 5 USD SOL-USDC round trip
cost -0.011 USD in paper and about -0.043 in the backtest. Real swaps send no
priority fee setting (Jupiter decides, uncapped), paper charges none, and the
backtest assumes a flat 30 bps fee whatever the pair. The owner chose three
fixes (2026-10-04). No ledger schema change.

Design (2026-10-04):
- **One priority-fee setting.** `max_priority_fee_lamports` in `policy.toml`
  (`[trading]`, per-mode override like the other keys; default 100,000 =
  0.0001 SOL; must be > 0). The owner sets it; agents can't edit the file.
  - Real: `OnChainExecutor` passes it to `get_swap_transaction`, which sends
    `prioritizationFeeLamports: {priorityLevelWithMaxLamports: {maxLamports,
    priorityLevel: "veryHigh", global: false}}`. `veryHigh` because a swap
    that doesn't land in 30 s becomes UNCONFIRMED and blocks the mode; the
    cap bounds what that costs. What was paid is still read from the chain.
  - Paper: `SimulatedExecutor` charges the cap on every leg (conservative):
    the wallet pays base + cap, `TradeCosts.fee_lamports` = base + cap,
    `priority_fee_lamports` = cap.
  - Backtest: the default `network_fee_usd` is (5,000 + the real policy's cap)
    lamports at the SOL price (Price API) instead of a flat 0.002 USD.
  - `wiring.build_trade_service` loads the policy once and gives the cap to
    the provider and the policy to the gateway.
- **Each pair's fee is measured.** `trader/backtest/costs.py`:
  `measure_costs` quotes a buy of the spec's trade size
  (`sizing.max_usd(budget_usd)`, in quote units) and a sell of what it
  returns, on Jupiter, now. The round-trip loss in bps is spread + pool fees +
  price impact at that size; `fee_bps` per leg is half of it. The same call
  gets the SOL price for the network fee. `backtest` (and
  `live_vs_backtest.py`) measure by default; `--fee-bps` and
  `--network-fee-usd` given explicitly skip the measurement (offline,
  repeatable). A failed measurement is an error that names those flags.
  `--slippage-bps` stays 10 (paper's fill below the quote). The output says
  which costs were measured and how. With `--ticks`, costs are today's, not
  the recording's.
- **Cost per round trip in every report.** For each closed position: the
  move a costless trade would have made, entry spend x (exit tick price /
  entry tick price - 1) (the intents' `price` is the strategy's tick price),
  minus the net realized PnL. That is every cost (spread, pool fees,
  slippage, network and priority fees) in one number, in USD and in bps of the
  entry spend, computed the same way everywhere because the backtest replays
  through the same ledger code. Partial sells count by raw amount. Pairs
  without a stable are measured in the quote token and converted with the
  sell's quote USD price. `RoundTripCosts` (core, `models/costs.py`),
  `Reports.round_trip_costs(account, start, end)` (ledger). Shown in the
  backtest summary and JSON, the daily report (per bucket, the day's round
  trips), `live_vs_backtest.py` (both sides) and `ledger_dump.py` (per
  bucket).
- **Tests:** policy parsing of the key; the swap request body; paper charges
  base + cap and records the priority part; the backtest's default network fee
  from the cap and a SOL price; `measure_costs` on a fake quote client (bps,
  half per leg, non-stable quote); the CLI measures by default and skips with
  both flags; round-trip cost on hand-made ledgers (full, partial, a
  non-stable pair) and in the backtest result; the daily report line. Live:
  measuring SOL-USDC gives a small positive fee; Jupiter builds a swap
  transaction with the priority-fee body.

#### B10. Review cleanup — M
From the `/simplify` review of B5–B9 (2026-10-04). No behaviour change for a
USDC pair. Items numbered as the owner chose them; 3 (one rule for when orders
re-read the wallet) and 7 (one round trip per `connect` tick) wait for a
discussion.

- **C1 Stablecoin = 1 USD, decided once.** `usd_snapshot` and `price_fn`
  (`market/prices.py`) answer 1 for USDC/USDT with or without an oracle;
  oracles only return market prices. `priced_mints` always includes the quote.
  Gone: the stable branches in `TradeService.quote_usd` and
  `AsyncAccount._notional_usd`, the bot's `quote_is_usd` flag (the tick
  recorder always writes `quote_usd`, 1 for a stable pair), and the
  `quote_usd = 1` defaults (`BucketSnapshot`, `Strategy.on_market_refresh`,
  `log_position`), which become required.
- **C2 `IntentStatus.REJECTED`.** A provider refusal (`SwapRejectedError`)
  ends the intent REJECTED instead of FAILED with a `recusado: ` text prefix;
  the circuit breaker counts `status = FAILED` only. `status` is free text,
  so no schema change; old rows keep their prefix and the breaker only counts
  rows since the process started.
- **C4 Real types from the test fakes.** `mock_provider` and the fakes return
  `SwapResult` and `TradeCosts`, so `execute_trade` drops its `getattr` and
  `isinstance` guards.
- **C5 One base error for failed attempts.** `SwapAttemptsError` carries
  `failed_signatures`; `SwapRejectedError`, `SwapFailedError` and
  `TransactionSubmittedError` subclass it, and the provider catches the base.
- **C6 Every USD price through the hub** (`run` and `serve`). `PriceHub`
  is a `PriceOracle` (`usd_prices`: its latest prices, a Price API call now
  for new or stale mints). `build_trade_service(prices=hub)` gives it to the
  service, the sweep, the daily report and the provider's quote check (the
  provider reads `usd_prices`, which defaults to its client's). One Price API
  poller per process instead of three. `JupiterPriceOracle` stays for
  callers without a hub.
- **C8 One policy query.** `_account_buys` folds into `_spending` (the same
  rows; the per-bucket hour count is taken in Python).
- **C9 Fewer pass-through parameters.** `swap_with_details` and
  `_do_swap_with_retry` merge; `fail_closed` is keyword-only without a
  default below the public methods. The priority-fee default lives on
  `Policy` only: `build_provider`, `AsyncJupiterProvider.on_chain` and
  `OnChainExecutor` require it.
- **C10 Background tasks of the trade-runner.** `TradeRunner.background`
  (like `BotConfig.background`) runs the hub and the daily report beside the
  server; `_serve` keeps only the closing.
- **C11 `ledger_dump.py`** takes each bucket's PnL from the restore it
  already does instead of a second `pnl_totals`.

Tests: the existing suite, adjusted where a fake or a default changes, plus
the hub as an oracle (fresh, new, stale mints; no API call when fresh) and a
REJECTED intent not tripping the breaker.

#### B11. Folder layout by process — S
Moves only, no behaviour change (2026-10-05). `trader/` is split by the process
that runs the code:
- `trader/execution/` is the trade-runner: wallet, ledger, policy, venues, swaps.
- `trader/strategy/` is the strategy-runner: bot loop, spec engine, remote client.
- `trader/shared/` holds what both sides import: models, market data, the spec
  contract, the wire protocol, paths.
- `trader/api/cli/` holds the entry points.
- `trader/backtest/` composes both.

`execution` and `strategy` never import each other, and `shared` imports
neither (`tests/test_architecture.py`). Two definitions move so that holds:
- the `Notifier` Protocol goes to `shared/notification`;
- spec models, `expr` and validation go to `shared/spec`, because the
  trade-runner validates specs too.

Tests mirror the new tree.

#### B12. Only the trade-runner talks to Jupiter — S
After B11, `connect` still downloaded its warm-up candles from Jupiter itself,
so the market client had to live in `shared/`, and with it
`get_swap_transaction`. Now the trade-runner reads all market data, and the
strategy side gets prices and candles only from it (2026-10-05).
- **New wire op `candles`.** `{"op": "candles", "mint", "interval", "qty"}`
  answers `{"ok": true, "candles": [...]}` (decimals as strings, timestamps
  ISO). `qty` is 1–1000; `interval` is an `Interval` value. `serve` answers it
  from one `JupiterMarketData`. `RemoteTradeClient.candles` asks for them, and
  `RemoteCandles` turns that into the candle half of a `MarketData`, so
  `connect` builds `HubMarketData(trader.price, RemoteCandles(trader))`.
  `strategy_bot` no longer takes a candle source. The client reads lines up to
  4 MiB (900 bars are about 120 KB; asyncio's default limit is 64 KB). A
  `connect` and a `serve` from before B12 don't talk: restart them together.
- **`execution/market/`** (read-only, the market layer): `jupiter/` (the HTTP
  and websocket client, quote models, candle parsing), `JupiterMarketData`,
  `PriceHub` and `websocket_prices`, and the USD oracle, `usd_snapshot` and
  `price_fn`.
- **`execution/trade/`**: `gateway/`, `venues/`, `ledger/`, `policy/`,
  `trading_service/`. `runner.py`, `wiring.py`, `models/` and
  `notification/` stay at the root of `execution/`.
- **`shared/market/`** keeps what doesn't touch the network: the `MarketData`
  protocol and `HubMarketData` (`feed.py`), and `market_for` /
  `ratio_candles` (`pair.py`).
- `run` and `backtest` are unchanged: they live in `api/` and `backtest/` and
  build their own feeds.

Tests: the `candles` op (round trip through a real `TradeRunner`, a bad
`qty`, a reply larger than 64 KB), `connect` built without a candle source,
and the live `connect`-through-`serve` test warming up through the op.

#### B13. The spec belongs to the strategy side — S
After B11 and B12 the whole spec language sat in `shared/spec/`, because the
trade-runner parsed the full spec at `hello`. It only uses the money and
lifetime terms, so only those are shared now (2026-10-05).
- **`SpecTerms`** (`shared/spec/terms.py`): `spec_id`, `name`, `symbol`,
  `budget_usd`, `max_loss_usd`, `max_trade_usd` (the largest buy the sizing
  allows), `sizing_field` (for error paths), and `expires_at` or `ttl_days`,
  plus `expiry()`. `StrategySpec.terms()` builds it; the id is still the hash
  of the whole spec, computed on the strategy side. The model checks the same
  invariants as the spec (one expiry, max loss within budget), because it
  arrives over the wire.
- **`validate(terms, limits)`** (`shared/spec/validate.py`) keeps the policy
  checks (symbol and allowed symbols, trade size vs the policy and the budget,
  expiry window) with the same error paths and messages. `SpecError` and
  `SpecLimits` stay with it.
- **`strategy/spec/`** gets `models.py` (`StrategySpec`, every condition),
  `expr.py`, and `parse.py` (`parse_spec`, `SpecParseError`), next to
  `strategy.py` and `conditions.py`.
- **Wire: `hello` sends `terms`** instead of the full spec:
  `{"op": "hello", "token", "terms"}`, decimals as strings. The trade-runner
  checks the terms against its own policy as before and keeps them for the
  sweep. A `serve` and a `connect` from before B13 don't talk.
- Nothing that protects funds changes: budget, max loss, symbol and expiry
  come from the terms, and the gateway's policy re-checks each order. A wrong
  condition only makes a worse strategy; the trade-runner never judged that.

Tests: `StrategySpec.terms()` (fixed and pct sizing, both expiries), terms
refused at `hello` (bad JSON shape, over the policy), the bucket opened from
terms, and the live `connect`-through-`serve` test.

#### B14. Trading is `serve` + `connect` only — S
`run` was a second way to trade: the bot and the trade-runner in one process,
with its own hub, its own candle reads and its own wiring. Every spec now
trades through a trade-runner (2026-10-05).
- **`run` is gone**, with `_run_bot` and `_check_limits`. To trade one spec:
  `serve <mode>` in one terminal, `connect spec.json` in another. The
  trade-runner validates the spec's terms at `hello` (B13), so `connect`
  checks no policy itself.
- **`--record-ticks` moves to `connect`**: each price the bot receives, and the
  quote's USD for a non-stable pair, goes to the CSV that `backtest --ticks`
  and `live_vs_backtest.py` replay.
- **`trader/api/cli/bot.py` becomes `backtest.py`** (`backtest`, the cost
  measurement, `MARKET_DATA`, `COST_QUOTES`, `SPEC_HELP`). `mode_lock` and
  `limits_from_policy` move to `runners.py`, next to `serve`.
- The process lock is one `serve` per mode. The daily report and the Telegram
  fills come from `serve`.
- **`/smoke`** starts an isolated `serve paper` plus a `connect` for the spec,
  then reports both logs and the ledger.
- The guard hook still blocks `run real` (an old habit) and blocks `serve
  real`; its message points to `serve real`.
- `LocalTradeClient` stays: the backtest and the tests use it.

Tests: the CLI has `backtest`, `connect` and `serve`; `connect --seed` and
`--record-ticks` reach the bot; an unreadable spec fails before connecting;
`/smoke` end to end by hand.

#### B8. Perpetual futures — L (planned in [`perps.md`](perps.md))
Long and short perp positions with leverage, on Jupiter Perps, through the
same gateway, policy, ledger and buckets. The spec sets its leverage, capped
by `max_leverage` in `policy.toml` (default 3); every real perp position also
holds a stop-loss on the venue. Starts after B6: a no-behaviour-change
refactor first (P0: venue and bucket-account seams, direction on the strategy
context), then the perp model and paper (P1, which bumps the ledger schema and
so waits for the end of the paper soak), backtest, and the real adapter.
Design, coupling assessment and phases in [`perps.md`](perps.md).

#### B7. Later
- Transaction inspection in the trade-runner: only Jupiter, Token,
  Token-2022, ATA, ComputeBudget and System programs; the only decreasing
  balance is the wallet's source account, by at most the quoted `inAmount`.
- Market-sanity checks: quote within 2% of the independent price; reject
  market data older than 30s.
- A shared price-websocket hub for many strategy-runners.
- Per-bucket hourly limits.
- Owner approval for large trades (for example Telegram inline buttons).
- A service entrypoint (systemd, Docker or a Windows service).
- `Order.timestamp` in UTC: `AsyncAccount` defaults its clock to the naive
  `datetime.now` (found in B5; the ledger's own timestamps are UTC).

Design (2026-10-04). The owner chose the safety core and the price hub;
Telegram approval and a service entrypoint stay open. No ledger schema change:
- **Transaction inspection** (`trader/execution/trade/venues/jupiter/tx_inspection.py`,
  venue; called by `OnChainExecutor` after signing, before sending, in place of
  the plain simulation): (1) every top-level instruction's program is one of
  Jupiter v6, Token, Token-2022, ATA, ComputeBudget, System (checked live on
  real swap transactions); (2) one simulation that returns the wallet and all
  its token accounts (`AsyncRPCClient.simulate_with_accounts`, a solders
  request: solana-py's `simulate_transaction` can't ask for accounts) must show
  no token account of the wallet decreasing except the input mint's, by at most
  the quote's `inAmount`, and the wallet's lamports falling by at most the
  input (if SOL) plus `MAX_NATIVE_SPEND_LAMPORTS` (fees, rent of new accounts).
  A failure is `TransactionInspectionError` (a `SwapRejectedError`: nothing
  was sent, not retried). Paper and replays don't build transactions.
- **Quote sanity** (`AsyncJupiterProvider`): the quote's output, valued with
  the Price API, must be at least 98% of the input's value
  (`max_quote_deviation_pct = 2`; `None` in replays). Without prices a buy is
  refused (fail closed) and a sell goes on with a warning (an exit is never
  stuck on the Price API).
- **Stale market data**: a price older than 30s is never used. The hub
  (below) raises `StalePriceError` instead of returning one, so the bot's tick
  fails and backs off; orders don't go out on old data.
- **Price hub** (`trader/execution/market/hub.py`, market layer): `PriceHub` keeps the
  latest `(price, received_at)` of every mint asked for, from one websocket
  subscription for all of them (reconnects with the new list when a mint is
  added) plus the Price API, polled every 2s in one batched call for mints the
  websocket hasn't updated for 5s (this also fixes the soak's F2: a quiet
  mint no longer costs a 30s timeout per tick). `HubMarketData` is a
  `MarketData` over it (candles still from `JupiterMarketData`). `serve` runs
  one hub and answers a new wire op `{"op": "price", "mint"}` (after `hello`)
  with `{price, age}`; `connect` builds its feed from `RemoteTradeClient.price`
  (`trader/strategy/trading_service/remote.py`), so N strategy-runners share one
  websocket and one Price API poller. `run` uses an in-process hub as a bot
  background task. `PairMarketData` composes two hub feeds as before.
- **Per-bucket hourly limit** (policy): `max_trades_per_hour_per_bucket`
  (default 6; paper 60), counting the bucket's buys in the last hour, next to
  the wallet-wide `max_trades_per_hour`. `PolicyState.account_trades_last_hour`.
- **`Order.timestamp` in UTC**: `AsyncAccount` and `TradeService` default to
  `datetime.now(UTC)` (`to_utc` already reads old naive values as local).
- **Tests:** program allow-list and balance rules on built transactions and
  faked simulations (including a drain of another token and SOL over the
  allowance); quote deviation on both sides and without prices; the hub (one
  subscription for several mints, the REST poll of quiet mints, staleness,
  resubscribe on a new mint) with a fake stream; the `price` op over the
  socket; the per-bucket limit; UTC order timestamps. Live: the hub serves SOL
  and a quiet token; a real swap transaction passes the program check.


| Item | Status | Notes |
|---|---|---|
| A0–A12, R0–R9, S1–S8 | **done** (2026-09-23 → 30) | See [`history.md`](history.md) |
| U1–U3, T1–T6 | **done** (2026-09-30) | See [`history.md`](history.md) |
| B4 Real-mode guardrails for agent sessions | **done** (2026-09-30) | 546 tests (43 new). `.claude/settings.json` denies Read `.env` and Edit `.env`/`policy.toml`; `.claude/hooks/guard_commands.py` (PreToolUse, Bash/PowerShell) denies real runs, `--env-file`, `TRADER_POLICY_FILE`, `.env`, printing the key/RPC URL and the real ledger. Checked live: it blocked this session's own command. It matches command text, so docs that mention these are edited with the Edit tool |
| Owner: one-week paper soak | in progress (since 2026-10-03) | `serve paper` + 3 `connect`s. First review (2026-10-04, 12.5 h) in [`soak-test.md`](soak-test.md): execution path clean; candle warm-up collapses on thin tokens (F1), a quiet websocket costs 30 s per tick (F2), SOL `connect` logs keep only ~2.5 h (F3). F4 fixed (2026-10-04): the trade-runner logs bucket opens, connects and disconnects |
| B3 Trade-runner / strategy-runners | **done** (2026-09-30) | `serve <mode>` / `connect spec.json`; `trader/runners/{lock,trade_runner,strategy_runner}.py`, `trading_service/{wire,remote}.py`; one execution process per mode (OS lock, `run` too); exit sweep every 30s. 12 new tests plus a live test (a separate `connect` process trades through `serve paper`). The guard also blocks `serve real` and `trader-real.json` |
| B2 Wallet allocation + reconcile | **done** (2026-09-30) | `WalletBalances` shared by the service's accounts; budgets must fit the wallet (positions at cost); startup reconcile of every bucket's positions blocks buys of a missing token; per-account `reconcile_position` removed; `ledger_dump.py` shows each bucket's position |
| Gap review after B3/B2 | **done** (2026-09-30) | 575 tests. A transaction the chain confirmed as failed is retried with the slippage escalation and ends FAILED (was UNCONFIRMED: the mode was blocked); RPC reads at Confirmed (was Finalized: stale balances right after a swap) and sells re-read the wallet; provider rejections (`recusado: ...`) don't count toward the breaker; a balance read that overlaps a fill isn't cached; a leg without a SOL price uses the other leg's; sells keep a client-given idempotency key; `connect` re-reads the connection file after a trade-runner restart and gives up after 5 reconnects so the bot loop backs off |
| B5 Accounting and performance | **done** (2026-10-03) | 603 tests (28 new) plus 2 live checks. `Position.unrealized_usd`, in fill notifications and the tick log; `trader/execution/notification/daily_report.py` (`DailyReporter`, run beside `run` via `BotConfig.background` and beside `serve`; a `daily_report` event per UTC day); paper fills `slippage_bps=10` below the quote, floored at `otherAmountThreshold`; `backtest --network-fee-usd` (0.002) taken from each replay leg; failed-on-chain attempts ride on `SwapResult.failed_signatures` / `SwapFailedError`, and `execute_trade` books their `meta.fee` (`failed_tx_fee` event, `PositionBook.charge`, subtracted by `pnl_totals`); `trader/backtest/compare.py` + `.claude/scripts/live_vs_backtest.py`. No ledger schema change. Checked live: the warm-up candles end right before the first tick; a real-quote paper fill lands below the quote |
| B6 More expressive strategies | **done** (2026-10-04) | 663 tests (60 new) plus 2 live checks. `pct_of_bucket` sizing; `expr` (`trader/strategy/spec/expr.py`: `ast` plus a whitelist, three-valued logic, normalized text); prices in the quote token everywhere on the strategy side (`Order.quote_price`, `BucketSnapshot.available` + `quote_usd`, `on_market_refresh(..., quote_usd)`); `trader/shared/market/pair.py` (`market_for`, `PairMarketData`, `ratio_candles`); `TradeService` converts USD caps with the quote's price (fail closed without it); replays with two series (`Tick.quote_usd`, a third CSV column, `ReplayPrices`, USD equity); `docs/examples/spec-jup-sol-expr.json`. No ledger schema change; USDC numbers unchanged. Checked live: a JUP-SOL backtest on two real candle series, the pair feed against the Price API, and a JUP-SOL paper smoke run |
| B7 Later | **safety core and price hub done** (2026-10-04); owner approval and a service entrypoint open | 717 tests (34 new) plus 2 live checks. `trader/execution/trade/venues/jupiter/tx_inspection.py` (program allow-list; simulated balances: only the input leaves, up to `inAmount`) in `OnChainExecutor` before sending; `AsyncJupiterProvider` quote check (2% vs the Price API, on in `wiring.py`; buys fail closed, sells warn); `trader/execution/market/hub.py` (`PriceHub`: one websocket for all mints + batched Price API for quiet ones, `StalePriceError` after 30s; `HubMarketData` paces the bot at 1 price/s) used by `run` in-process and by `serve`/`connect` through the new `price` op (fixes the soak's F2); policy `max_trades_per_hour_per_bucket` (6, paper 60); `Order.timestamp` in UTC. Checked live: one websocket subscription carries several mints; the hub keeps SOL and NOBODY fresh; real swap transactions use only allowed programs; the whole live suite (a `connect` trading through `serve paper`) passes. The B6/B7 wire changes mean a `serve` and a `connect` from before B6 can't talk to these: restart all of them together |
| B10 Review cleanup | **done** (2026-10-04) except C3 and C7 (wait for the owner) | 722 tests (4 new, 1 dropped with the mock guard it tested) plus the live suite. `PriceHub.usd_prices` (the process oracle in `run`/`serve`, `build_trade_service(prices=)`, `AsyncJupiterProvider.usd_prices`); stable = 1 in `usd_snapshot`/`price_fn` only (the backtest always uses `ReplayPrices`, identical output); `IntentStatus.REJECTED`; `SwapAttemptsError`; `swap_with_details` merged with the retry loop; priority-fee cap required below `Policy`; `TradeRunner.background`; one policy query; one `restore` per bucket in `ledger_dump.py`. No ledger schema change |
| B8 Perps | planned (2026-10-03) | Phases P0–P5 and their progress in [`perps.md`](perps.md) |
| B9 Costs across modes | **done** (2026-10-04) | `max_priority_fee_lamports` (policy, default 100,000): real's Jupiter `maxLamports` (`veryHigh`), charged by paper, in the backtest's network fee; `trader/backtest/costs.py` measures the pair fee and network fee on Jupiter unless `--fee-bps`/`--network-fee-usd` are given (`measured_costs`); `RoundTripCosts` + `Ledger.round_trip_costs` in the backtest, daily report, `live_vs_backtest.py`, `ledger_dump.py`. No ledger schema change. Checked live: Jupiter keeps a real swap's priority fee under the cap; SOL-USDC measures ~0 bps at 5 USD |


## 7. Known issues and limitations

### 7.1 Money and accounting

- **Costs and net PnL (how it works today).**
  - After each swap the costs actually paid are recorded: the network fee
    (base + priority, from `meta.fee`), rent for token accounts the wallet
    opened (a cost, marked refundable), and other SOL the route charged.
    Attempts the chain confirmed as failed paid a fee too: it is a
    `failed_tx_fee` event and comes off the bucket's realized PnL (an
    unreadable fee counts as the 5000-lamport base fee). An UNCONFIRMED
    intent's fee is still not recorded (there is no resolve step).
  - Real mode reads the confirmed transaction, including the real in/out
    amounts from the token-balance deltas. Paper simulates the base fee and
    first-time rent, and fills 10 bps below the quote (never below its
    `otherAmountThreshold`).
  - LP/AMM fees, price impact and slippage are already in the actual amounts
    and are never subtracted again; they are logged for information.
  - PnL is kept in native units (the quote token, plus costs in SOL) with a
    USD estimate from rates taken from the trade itself. What the trade
    can't price (the SOL price on pairs without SOL, the USD price of a
    non-stable token spent) comes from a Price API V3 snapshot taken
    **before** the trade. If the API is down those values stay unknown:
    the costs are flagged `[!] incompleto`, and a buy with an unknown USD
    value is denied unless `allow_unknown_notional = true`.
  - Costs are fetched only after the ledger marks the intent EXECUTED, and a
    failure there never retries or fails the swap.
- **Two units since B6.** The strategy side works in the pair's quote token
  (prices, the bucket's `available`, `Order.quote_price`); `Order.price`,
  budgets, PnL and the policy are USD. On a non-stable quote, the USD side
  needs the quote's Price API price: without it a budgeted bucket offers 0 and
  can't open. A backtest's return on such a pair includes the quote token's
  own USD move (the wallet holds SOL, valued in USD).
- **No commands to inspect or repair the ledger** (stage U). An UNCONFIRMED
  intent (a process killed mid-swap) blocks the mode's ledger until the file
  is moved or deleted; `.claude/scripts/ledger_dump.py` prints it read-only.
- **Replays model costs, they don't measure them.** `fee_bps` +
  `slippage_bps` per leg and a flat `--network-fee-usd` (default 0.002);
  the one-time rent of a token account is not modelled. Compare with a live
  bucket (`live_vs_backtest.py`) to calibrate them; the comparison assumes the
  bucket started the window flat and with its whole budget.
- **`Order.timestamp` is naive local time** (the account clock is
  `datetime.now`), unlike the UTC intent and event timestamps; the comparison
  uses the intent's `created_at` (B7).
- **One tick per second.** Bots take prices from a hub (B7) at one per
  second, where before they ticked on every websocket message (several a
  second for SOL, one per 30s for a quiet token). Conditions that count
  ticks, like `random_chance`, now fire at a steady rate (soak F5).
- **The daily report needs Telegram** (`TELEGRAM_*`); without it the day is
  still marked as reported in the ledger.
- Policy budgets are wallet-wide by design; per-bucket limits are the
  bucket's job.

### 7.2 External APIs

- Quotes and swaps use `api.jup.ag` `/swap/v1/quote` and `/swap/v1/swap`,
  unauthenticated or with `JUPITER_API_KEY` (`x-api-key`); override the host
  with `JUPITER_API_URL`. Deliberately **not** `/swap/v2/order`: with a
  `taker` it switches to "ultra" mode and rejects an explicit `slippageBps`,
  which would break the slippage escalation. Confirmed against the live API.
- `priceImpactPct` is a **fraction** (0.01 = 1%), confirmed against the live
  API; the cap scales it to a percent.
- **The price websocket (`trench-stream.jup.ag`) and candles
  (`datapi.jup.ag`) are undocumented frontend endpoints**, reached with a
  spoofed `Origin`/`User-Agent`. Prices fall back to the documented Price
  API V3 when the websocket fails or is quiet for 30s (A4); candles still
  have no fallback.
- **Keyless rate limit.** Without `JUPITER_API_KEY`, `api.jup.ag` answers
  `429 Too Many Requests` under bursts (seen in the live suite: quote calls
  right after candle and price calls). The live suite pauses 5s between
  tests. Since B7, strategy-runners take prices from the trade-runner's hub
  (one websocket and one batched Price API poll for all of them), so only
  their warm-up candles and the trade-runner's quotes, quote checks and Price
  API snapshots hit the API.

### 7.3 Security

- With `run`, the private key lives in the same process as the strategy;
  `serve` + `connect` keeps it in the trade-runner only (B3).
- `repr(Keypair)` is safe, but **`str(Keypair)` is the base58 secret**. Never
  format a keypair with `str()` or `{keypair!s}`.

## 8. Top risks

1. **A runaway or prompt-injected agent.** Mitigated: agents only write
   spec files; policy and bucket budgets bound every trade; the owner starts
   every run, and real mode needs `real_trading_enabled`.
2. **Double execution or lost confirmations.** Mitigated by idempotency
   keys, no re-send after broadcast, UNCONFIRMED blocking and reconciliation.
3. **Key compromise through agent-reachable code.** Mitigated by the
   trade-runner split (B3) and a low-balance hot wallet.
4. **Jupiter changes to undocumented endpoints.** Mitigated by the Price API
   fallback (A4) and configurable hosts.
5. **Buckets drifting from the wallet** (several buckets on one wallet,
   paper wallet edits). Mitigated by the aggregate reconcile and allocation
   rules (B2).
6. **Strategy quality.** Mitigated by backtests on real candles, paper runs
   before real, and iterating on the spec file.

## 9. Open questions for the owner

- Rotate the Helius key if that hasn't happened yet (R0); the leaked log
  files are gone.
- Which pairs, and what maximum notional per trade and per day, are you
  comfortable with in real mode?
- Real mode: one hot wallet for every strategy (the current plan), or one per
  strategy later?

## 10. Decision log

- **2026-09-23:** real mode off by default; `dry` is the CLI default; the
  policy is TOML (stdlib `tomllib`), not YAML; one ledger file per mode.
- **2026-09-25:** net PnL records costs actually paid; slippage and LP fees
  are never subtracted again; costs are fetched only after EXECUTED.
- **2026-09-27:** stay on `/swap/v1` (not `/swap/v2/order`); `priceImpactPct`
  is a fraction.
- **2026-09-28:** agents author strategies, never trades. Declarative JSON
  specs; CLI + JSON; owner mint registry; auto-activate in paper, owner
  approves real.
- **2026-09-28:** strategy-runners are separate processes, one per spec, each
  with its own bucket, connected to one trade-runner per mode over JSON lines
  on localhost TCP with a token. Budgets live in the bucket, not the policy.
- **2026-09-28:** specs are stored in the ledger, so approval copies them into
  the real ledger and nothing an agent runs writes there.
- **2026-09-28:** layering is enforced by `tests/test_architecture.py`.
- **2026-09-29:** `mark_executed` and `record_fill` stay two commits (costs
  are fetched between them).
- **2026-09-29:** market data and the provider use separate Jupiter clients,
  matching the future process split; `run` names its bucket after the pair so
  existing ledger accounts keep restoring.
- **2026-09-29:** agent commands always print JSON; stops never wait for the
  warm-up; v1 sizing is `fixed_usd` only; the strategy layer takes
  `SpecLimits`, not `Policy`.
- **2026-09-29:** the three planning documents were merged into this one.
  Refactorings that simplify the trade path (stage A) come before the
  features (stage B); manual swaps become a `manual` bucket; USD valuation
  (Price API V3) moves ahead of the registry because budgets and cost
  accounting depend on it.
- **2026-09-30:** the legacy strategies (random, target value, the composer
  family) are ported to spec examples and deleted (A11); specs are the only
  strategies, and composition is `entry`/`exit` `mode` over blocks.
- **2026-09-30 (stage U):** the owner found the code hard to follow and cut
  it down. Dry mode is removed (modes: `real`, `paper`); the CLI is `run` and
  `backtest` only, with a required mode; `swap`, `halt`/`resume`, `ledger`,
  `pnl`, `paper` and the agent JSON commands are deleted until stage B needs
  them; one backtest engine. No kill switch: stopping the process stops
  trading, and the breaker re-arms on restart. UNCONFIRMED intents stay
  blocked (move or delete the ledger to recover). The events table stays
  without the hash chain. `policy.toml` is untracked and
  `botconfigs.example.yaml` deleted. Order: U1, stage T, U2, U3.
- **2026-09-30:** strategies are created as **spec files written from the
  docs** (`docs/specs.md`, kept in step with the code by
  `tests/test_spec_docs.py`), and we iterate on the files and the code. Agents
  don't interact through a CLI, so the agent commands, the registry and
  `submit` (B1), the registry-based approval (B4) and the MCP server (B7) are
  dropped. Goal 1 is redefined accordingly.
- **2026-09-30:** stage B reordered: B4 (guardrails for agent sessions) first,
  then the owner's paper soak, then B3 with B2, then B5, B6, B7. B1 is removed
  (specs are files); B2 loses the manual-swap rules; stages U and T are
  archived in `history.md`.
- **2026-10-03 (B5):** no ledger schema change, so the soak's paper ledgers
  keep opening: failed-tx fees and sent daily reports are events, not
  columns. Background work of the execution process (the daily report) runs
  through `BotConfig.background` in `run`, so the bot stays unaware of the
  ledger.
- **2026-10-04 (B7):** the owner chose the safety core and the price hub;
  Telegram approval and a service entrypoint wait. The transaction check runs
  on the signed transaction in the executor (one simulation that also returns
  the wallet's accounts) rather than in the trade-runner, so `run` gets it too.
  The quote check fails closed for buys and open for sells, so an exit never
  waits on the Price API. `run` uses the hub in-process too, so there is one
  price path.
- **2026-10-04 (B6):** the strategy works in the pair's quote token and money
  stays in USD, rather than converting everything to USD: a JUP-SOL spec
  should judge the JUP/SOL ratio, since its gains are paid in SOL. The `expr`
  language is read by Python's `ast` and checked against a whitelist (the
  hand-written parser first used redid what `ast` already does), with
  three-valued logic so a warming indicator never fires a `not`.
- **2026-10-04 (B9):** the priority fee is one owner setting in `policy.toml`
  (a cap), used by real as Jupiter's `maxLamports`, charged in full by paper,
  and priced into the backtest's network fee. The backtest's fee is measured
  per pair from a live round-trip quote at the spec's size, not a flat 30 bps;
  explicit flags keep it offline and repeatable. Cost per round trip is
  "costless move minus net PnL" against the strategy's tick prices, so it
  reads the same in real, paper and backtest.
- **2026-10-04 (B10):** the price hub is the one Price API client of an
  execution process: its oracle refreshes a mint only when the hub's own loop
  let it pass `max_age` (the hub not running yet), so the per-tick budget
  check and every trade's USD snapshot cost no request. A provider refusal is
  its own status (REJECTED) rather than a text prefix on FAILED; old rows keep
  the prefix, and the breaker only counts rows since the process started.
- **2026-10-03:** perps planned as B8 in [`perps.md`](perps.md). The owner
  chose Jupiter Perps, long and short, leverage set by the spec and capped by
  `max_leverage` (default 3), a venue-side stop on every real perp position,
  and B8 after B6. Its ledger schema bump waits for the end of the paper soak.
