# Agent-authored strategies

Status as of 2026-09-28: **Phase 0 done; Phase 1 next.** This document replaces the
agent-integration part of [`plan.md`](plan.md) (its Phases 3–5). In the old
model, agents proposed individual trades over MCP. In the new model, **agents
only author strategies**. Track progress in §6 and record decisions in §7.

Related documents:

- [`architecture.md`](architecture.md) is a step-by-step tour of the code as it
  is today, plus the target layers.
- [`refactoring-backlog.md`](refactoring-backlog.md) items are scheduled into
  the phases below (§5.0).

---

## 1. Goal and decisions

An agent (Claude Code) never proposes a trade, never touches the key and never
edits policy. Its loop is:

1. Gather information: market data, the performance of its past strategies,
   and the policy limits.
2. Pick a pair and write a strategy.
3. The bot runs that strategy deterministically through the existing
   `TradeGateway → policy → ledger` path.

This is safer and simpler than the old plan. Every strategy can be validated
and backtested before it runs, execution can be replayed, and the existing
guardrails still apply to every trade.

Owner decisions (2026-09-28):

| # | Topic | Decision |
|---|---|---|
| 1 | Strategy form | A **declarative JSON spec** built from vetted blocks. A restricted expression language (e.g. `"rsi(14) < 30 and price < ema(50)"`) comes later as a new condition type. No agent-written Python. |
| 2 | Interface | **CLI commands with `--json` output** that Claude Code runs, on top of a service layer (`trader/agent_api/`). An MCP server can wrap that layer later. |
| 3 | Mints | **Owner registry only** (`SOLANA_MINTS` ∩ policy `allowed_symbols`). The agent cannot add mints. |
| 4 | Activation | **Automatic in paper; the owner approves real.** A spec that passes validation and the backtest gate starts in paper. Real mode needs the owner to run `strategy approve <id>`. |
| 5 | Architecture | **Strategy and execution are separate processes.** Each strategy has its own bucket and its own strategy-runner. Strategy-runners connect to a trade-runner (one per mode) that holds the single gateway and executes trades. A strategy does not know which mode it runs in. |
| 6 | Budgets | The **per-strategy budget is enforced by the bucket**, while `policy.evaluate()` stays wallet-wide. |

## 2. Architecture

```
 strategy-runner (spec A)      strategy-runner (spec B)       <- no key, no ledger, no mode
   own read-only Jupiter          own read-only Jupiter          (price ws + candles)
   SpecStrategy                   SpecStrategy
   TradeClient --+                TradeClient --+
                 |  hello / get_bucket / submit_order / heartbeat
                 v                              v
          trade-runner (one per mode; the ONLY process with key/wallet/ledger)
            BucketManager: one bucket per spec (allocation, position, PnL, status)
              -> bucket check (spend cap = allocation - open exposure, shrinks after losses)
              -> TradeGateway.submit  (idempotency -> policy.evaluate -> ledger -> execute)
              -> provider.swap (real/dry: on-chain; paper: shared SimulatedWallet)
            owns: retire/expiry/max_loss exits, aggregate reconcile, serialized submits
```

- **One interface, two transports.** A `TradeClient` protocol has two
  implementations:
  - `LocalTradeClient` calls a `TradeService` in the same process. Tests,
    backtests and the legacy `run` command use it.
  - `RemoteTradeClient` sends JSON lines over a localhost TCP socket to the
    trade-runner, which wraps the same `TradeService`.

  The strategy code is identical in every case. Running one process or many
  is only a wiring choice.
- **The mode lives only in the trade-runner.** A strategy-runner announces
  "I am spec `<id>`". The trade-runner accepts it only if that spec is active
  in its own mode's ledger (`active` in paper, `approved` in real), and then
  opens that spec's bucket.
- **Why this is safe:**
  - **Submits are serialized.** The trade-runner runs one asyncio loop with
    a submit lock. That prevents several strategy processes from racing on
    `.data/paper-wallet.json` (`SimulatedWallet` loads once and rewrites the
    whole file), on `TradeGateway._authorize` (check-then-record) and on the
    event hash chain.
  - **Balances are fresh.** Each buy re-reads the wallet balance.
  - **The trade-runner is the only process with the key.** Most of the old
    plan's isolated executor (`plan.md` Phase 5) comes with it.
- **Buckets:**
  - Each bucket has `account_id = "<mode>:strategy:<id>"`. The ledger's PnL,
    position restore (`last_executed_trade(account)`) and
    `pnl_report(account)` already work per account and need no change.
  - A bucket with no open position can spend
    `max(0, budget_usd + min(0, realized_usd))`. Buckets are long-only with
    one position at a time.
  - The bucket check runs **before** `policy.evaluate`. The wallet-wide policy
    is unchanged.
  - On activation, the sum of active budgets must be at most
    `max_total_allocated_usd`.
  - At validation, sizing must be at most `max_trade_usd`. Otherwise every
    buy is silently denied (see the AGENTS.md note on `[paper.limits]`).
- **Exits don't depend on the strategy process.** On retire, expiry or
  `max_loss_usd`, the trade-runner sells the bucket's open position itself.
  Sells skip the budget rules, so a dead or buggy strategy-runner cannot
  strand a position.
- **Specs live in the ledger**, in a `strategies` table. Every status change
  is also recorded as an event in the hash chain.
  - `submit` writes to the paper ledger only.
  - `approve` copies the immutable spec into the real ledger.
  - So no command the agent can run writes to the real ledger.
- **Validation at submit time is only advisory.** The agent could point
  `TRADER_POLICY_FILE` at a looser policy. The trade-runner therefore
  re-validates each spec against its own policy when it opens the bucket, and
  the gateway still checks every trade.
- **Protection against cherry-picked backtests.** `submit` runs its own
  backtest over a window the policy fixes (`backtest_candles`,
  `backtest_fee_bps`, `min_backtest_trades`).

### 2.1 Wire protocol (JSON lines on `127.0.0.1`, token auth)

The trade-runner holds a lock file. It writes `{port, token}` to
`data_dir()/trader-<mode>.json` when it starts and removes that file when it
exits.

| Message | Reply | Notes |
|---|---|---|
| `hello {spec_id, token}` | `{bucket}` or an error | Rejects a bad token, an inactive spec, or a second connection for the same spec |
| `get_bucket` | `{available_usd, position, realized_usd, status}` | `status: retiring` tells the strategy-runner to exit |
| `submit_order {side, quantity, price, rationale, idempotency_key}` | `{filled: order}` / `{denied: reasons}` / `{error}` | The strategy-runner creates the key; if it resends after a reconnect, the gateway deduplicates it |
| `heartbeat` | `ok` | A missed heartbeat is only logged; exits never depend on it |

The strategy-runner finds the trade-runner with `--trader <file|host:port>`.
It has no mode argument.

### 2.2 Layers and packages

Separation is enforced, not just documented. Every module belongs to one
layer, and `tests/test_architecture.py` (Phase 0) parses the imports and fails
when a module imports from a layer it may not use. The table of layers is in
[`architecture.md`](architecture.md) step 11. The key rule is that the
**strategy** and **strategy-side** layers can never import the execution,
venue or risk layers.

New code goes into these packages:

| Package | Layer | Contents |
|---|---|---|
| `trader/indicators.py` | core | Pure `Decimal` indicator math, `BarSeries` |
| `trader/market/` | market | `MarketData` protocol (price, candles); `JupiterMarketData`, which is read-only with no key; `ReplayMarketData` |
| `trader/strategy_spec/` | strategy | Spec models, conditions, `SpecStrategy`, `validate` |
| `trader/trading_service/protocol.py` | core | `OrderRequest`, `OrderReply`, `BucketSnapshot` and the wire codec (plain data) |
| `trader/trading_service/client.py`, `remote.py` | strategy-side | `TradeClient` protocol and `RemoteTradeClient` |
| `trader/trading_service/service.py`, `local.py`, `buckets.py` | execution | `TradeService`, buckets, `LocalTradeClient` |
| `trader/runners/strategy_runner.py` | strategy-side | Runs one spec: market data + `SpecStrategy` + `TradeClient` |
| `trader/runners/trade_runner.py` | app | Serves a `TradeService` over the socket, one per mode |
| `trader/agent_api/` | app | The service layer behind the agent CLI; MCP will wrap it later |

Existing modules stay where they are, so there is no big-bang move. The
layering test classifies them as they are today. Known misplacements
(`models/bot_config.py`, the registry in `trader/__init__.py`) are listed as
explicit, documented exceptions. They are removed in the phases below, and
each exception is deleted from the test once it is fixed.

## 3. Spec format (JSON, version 1)

```json
{"version":1,"name":"sol-dip","agent_id":"claude","rationale":"...",
 "symbol":"SOL-USDC","timeframe":"1_MINUTE",
 "entry":{"mode":"all","conditions":[{"type":"rsi_below","period":14,"value":30},
                                     {"type":"price_below_ma","ma":"wma","window":50}]},
 "exit":{"stop":{"type":"trailing_stop","pct":3},
         "mode":"any","conditions":[{"type":"take_profit","pct":4},{"type":"max_hold","minutes":240}]},
 "sizing":{"type":"fixed_usd","usd":20},
 "budget_usd":50,"max_loss_usd":10,"cooldown_minutes":30,
 "expires_at":"2026-10-10T00:00:00Z","supersedes":null}
```

- **Conditions** are a pydantic discriminated union on `type`, with
  `extra="forbid"` and bounds on every parameter. A later `{"type":"expr",...}`
  will compile to the same comparison nodes. `eval` is never used.
- **`exit.stop` is required** (`stop_loss` or `trailing_stop`) and is always
  OR'd with the other exits, so `mode: all` cannot disable it.
- **`timeframe`** is an `Interval` value (`15_SECOND`, `1_MINUTE`, `1_HOUR`).
  Indicators run on bars of that timeframe, and the bar still forming takes
  the latest price (the same rule as
  `WeightedMovingAverageStrategy.set_parameters`). Conditions are evaluated
  on every tick. Live, stops fire per tick; in a backtest they fire only at
  bar close.
- **Symbol rules.** The input must be USDC or USDT, because the price feed is
  in USD (see `refactoring-backlog.md` 1.2). The output must not be a
  stablecoin.
- **Id.** `id = sha256(canonical JSON)[:12]`, hex only, because `AccountPnL`
  splits account ids on `-`. Specs are immutable. To change a strategy,
  submit a new spec with `supersedes` set.

## 4. Agent-facing commands

The agent may run these commands. All of them take `--json`.

| Command | Purpose |
|---|---|
| `market symbols` | Tradable symbols (registry ∩ allow-list) |
| `market price SYM` / `market candles SYM --interval I --n N` | Raw market data |
| `market summary SYM` | Returns, volatility, RSI and WMAs, using the same `indicators` code as the strategies |
| `policy show` | Limits, strategy caps and remaining budget |
| `strategy schema` | JSON Schema of the spec |
| `strategy validate FILE` / `strategy backtest FILE` | Local checks; nothing is stored |
| `strategy submit FILE` | Validate, run the fixed-window gate and store the spec. It auto-activates in paper. |
| `strategy list` / `show ID` / `perf ID` | Registry state and per-bucket PnL compared with the stored backtest |
| `strategy retire ID` | Mark the spec `retiring`; the trade-runner then closes its position |
| `strategy run ID [--trader FILE]` | Start a strategy-runner (no mode argument) |

**Owner only:** `trader serve <mode>`, `strategy approve ID`, `resume`,
`ledger resolve`, `paper reset`, and edits to `policy.toml`.

**JSON output rules:**
- Each command prints exactly one JSON object, with `ensure_ascii=True`.
- An error prints `{"ok": false, "errors": [{"path", "msg"}]}` and exits
  non-zero.

## 5. Phases

Each phase ships on its own.

### 5.0 Refactoring backlog items folded into these phases

Every open item in [`refactoring-backlog.md`](refactoring-backlog.md) was
re-checked against the code on 2026-09-28, and all of them are still present.
The ones that help the separation are scheduled below. When an item is done,
mark it in both documents.

| Backlog item | Still relevant? | Scheduled in | Why it matters here |
|---|---|---|---|
| 1.1 Executor protocol (paper subclasses the real provider, `_NoRPC`, `keypair=None`) | yes | **Phase 1** (read-only `MarketData` split) + **Phase 2** (`Executor`) | A strategy-runner needs market data with no key. The trade-runner needs one execution seam for on-chain and simulated venues. |
| 1.2 USD value for non-stable inputs | yes | Phase 6 | Specs are limited to USDC/USDT inputs until then. |
| 1.3 Gateway as the only path to the ledger | yes | **Phase 2** | `TradeService` must talk to the gateway only. This adds `record_fill`, `restore` and `for_mode`. |
| 2.1 Fee reserve belongs to the venue | yes | **Phase 2** (with the executor) | |
| 2.2 `balances_track_fills` instead of a mode check | yes | **Phase 2** | The trade-runner's reconcile depends on it, and strategies must not know the mode. |
| 2.3 Inject a clock into `AsyncAccount` | yes | **Phase 1** | `max_hold` and cooldown in backtests read order timestamps. |
| 2.4 Telegram off the event loop | yes | **Phase 4** | The trade-runner serializes submits; a 10s blocking post would stall every strategy. |
| 2.5 Log volume in strategies | yes | **Phase 1** (new code only) | `submit` runs backtests, so new strategy code logs only state changes at DEBUG, and the backtest silences strategy loggers. |
| 2.6 Class-level `clock`/`rng` | yes | **Phase 1** | `SpecStrategy` builds on this base class. |
| 2.7 Test factories | yes | **Phase 0** | The new tests need `make_intent` and a `ledger` fixture. |
| 2.8 Identical consecutive denials | yes | **Phase 4** | Many strategy-runners share one gateway. |
| 2.9 Ledger write batching | yes | **Phase 0** (the hash-chain lock) + **Phase 2** (`record_fill` in one commit) | |
| 2.10 Idempotency-key default | yes | **Phase 2** | `TradeService` gets a single intent factory. |
| 2.10 `main.py start` duplicate | yes | **Phase 2** | `run` is rewired onto `TradeService`, and `start` is removed. |
| 2.10 Unused re-exports, `TickRecorder` flush, `load_ticks` sort | yes, low value | not scheduled | |
| **New 1.5:** `trader/__init__.py` imports the strategy registry, so every `import trader.x` loads all strategies | new | **Phase 1** | Move it to `trader/strategies_registry.py` so the package init stays empty. |
| **New 1.6:** `trader/models/bot_config.py` (the core layer) imports execution, venue and strategy | new | **Phase 2** | Move the wiring to `trader/bot/config.py`. |
| **New 2.11:** `SwapRejectedError`/`TransactionSubmittedError` live in the venue module, but the gateway and the wallet import them | new | **Phase 2** | Move them to `trader/models/errors.py` (core). |
| **New 2.12:** `Interval` lives in the market client, but spec models (the strategy layer) need it | new | **Phase 1** | Move it to `trader/models/public_data.py`; the client re-exports it. |

### Phase 0 — Prerequisites and guard rails (S)

These are small, independent changes that the later phases rely on.

1. **Dependency:** add `pydantic>=2.13` to `pyproject.toml` (with
   `uv add --system-certs`). Today it is only a transitive dependency, through
   `solana`.
2. **Clean stdout for `--json`:**
   - the RichHandler in `trader/logging_config.py` writes to stderr;
   - the "Carteira paper criada" message in `main._build_provider` goes to
     stderr (`typer.echo(..., err=True)`).
3. **Hash-chain race (backlog 2.9, part 1):**
   - `Ledger.add_event` opens its transaction with `BEGIN IMMEDIATE` before it
     reads the last hash, because the CLI and the runners write concurrently;
   - the hash is **not** cached in memory;
   - test: two connections to one file interleave their writes, and
     `verify_chain()` still returns `None`.
4. **Fix `policy.example.toml`:**
   - The keys under the commented `# [paper.limits]` header are live, which
     makes `max_trade_usd` a duplicate key and the TOML invalid. Comment them
     out.
   - Add a test that parses the example file with `load_policy`, so it can't
     rot again.
5. **Fix `AGENTS.md`:** the mccabe limit is 5 (`pyproject.toml`), not 10.
   Link the new docs.
6. **Layering test (`tests/test_architecture.py`):**
   - It parses every `trader/**/*.py` with `ast`, maps each module to its
     layer (§2.2, `architecture.md` step 11), and fails on a disallowed
     import.
   - Known exceptions are listed with the backlog item that removes them.
   - Unknown (unmapped) modules also fail, so every new module must be given
     a layer.
7. **Test factories (backlog 2.7):**
   - `make_intent(**overrides)` and a `ledger` fixture go in
     `tests/conftest.py`;
   - `test_gateway.py`, `test_ledger.py` and `test_policy.py` use them.

**Exit criteria:** `ruff check`, `ruff format --check`, `pyright` and `pytest`
are all green, and the layering test passes with only the documented
exceptions.

**Done (2026-09-28):**
- `pydantic>=2.13` is a direct dependency.
- Console logs go to stderr via `logging_config.stderr_rich_handler`; the
  paper-wallet message uses `err=True`.
- Every ledger write goes through `Ledger._write()` (`BEGIN IMMEDIATE`).
  `test_concurrent_writers_do_not_fork_the_chain` fails without the lock.
- `policy.example.toml` parses, and it is tested for every mode to equal the
  code defaults.
- `AGENTS.md` now states the mccabe limit of 5, the docs map and the layering
  rule.
- `tests/test_architecture.py` has no exceptions today. The only entries in
  `MISPLACED` are the package init and `models/bot_config.py`.
- `tests/factories.py` provides `make_intent` / `open_ledger`, and the
  conftest has an autouse close plus a `ledger` fixture. The three duplicate
  helpers are gone.

### Phase 1 — Spec, indicators, `SpecStrategy`, local backtest (no trading)
- **`trader/indicators.py`:**
  - pure `Decimal` functions: `wma` (lifted from
    `WeightedMovingAverageStrategy.weighted_moving_average`), `sma`, `ema`,
    `rsi` (Wilder), `pct_change`, `volatility` and `rolling_high`;
  - `BarSeries(timeframe)`, which uses UTC-aware timestamps and can be seeded
    from `TickerData`.
- **`trader/strategy_spec/models.py`:** the pydantic models.
  `model_json_schema()` produces the output of `strategy schema`.
- **`trader/strategy_spec/conditions.py`:**
  - a table that maps each `type` to a stateless predicate over a
    `TickContext`;
  - the context holds the price, the time, the position, the entry time, the
    peak price, and an `IndicatorBank`;
  - the bank is updated once per tick and caches indicators by (name, params).
- **`trader/strategy_spec/strategy.py`:** `SpecStrategy(TradingStrategy)`.
  - It uses `self.clock()` and `self.rng`.
  - It exposes `warmup() -> (Interval, n)`.
  - It never signals before its indicators are warm.
  - Sizing is `min(sizer_usd, balance) / price`, where `balance` is what the
    bucket has available. There is no hard-coded cap.
- **`trader/strategy_spec/validate.py`:** `validate(spec, policy) ->
  list[SpecError]`. It checks that:
  - both legs are allowed;
  - the input is a stablecoin and the output is not;
  - the sizing is at most `max_trade_usd` and at most `budget_usd`;
  - the expiry is within the limit.
- **`trader/agent_api/market.py`:** `symbols`, `price`, `candles` and
  `summary`.
- **`trader/agent_api/strategies.py`:** `schema`, `validate` and `backtest`.
  - It reuses `Backtester` and `ticks_from_candles`.
  - `main._fetch_candles` moves here.
- **`main.py`:** `market_app` and `strategy_app` sub-Typers, as thin
  wrappers.
- **`trader/market/`** (backlog 1.1, first half):
  - a `MarketData` protocol: `get_price(mint)`,
    `get_candles(mint, interval, n)`, `aclose()`;
  - `JupiterMarketData` wraps `AsyncJupiterClient` and needs no key and no RPC;
  - the candle-to-`TickerData` conversion moves out of
    `AsyncJupiterProvider.get_candles`, which then delegates to it.

  This replaces `main._fetch_candles`, which builds a paper provider just to
  read candles.
- **Backlog items in this phase:**
  - **2.3:** `AsyncAccount(clock=...)` is used for `Order.timestamp` and the
    balance-cache expiry, and the backtester passes the replay clock.
  - **2.5:** `SpecStrategy` logs only state changes, at DEBUG with lazy
    arguments. `Backtester` silences strategy loggers while it runs.
  - **2.6:** the class-level `clock`/`rng` are dropped (annotation only), and
    the test fakes call `super().__init__()`.
  - **1.5:** the `STRATEGIES` registry moves to
    `trader/strategies_registry.py` (it registers `spec` too), and
    `trader/__init__.py` becomes empty.
  - **2.12:** `Interval` moves to `trader/models/public_data.py`.
- **Tests:**
  - indicator vectors, and WMA matching the old strategy;
  - spec models and every validate rule;
  - `SpecStrategy`:
    - entry with `all` and with `any`;
    - the stop fires even under `mode=all`;
    - cooldown;
    - `max_hold` with an injected clock;
    - repeated backtests give identical results;
  - CLI output parses as JSON.

### Phase 2 — `TradeService` and buckets, in-process (the seam)
- **Execution seam (backlog 1.1 second half, 2.1, 2.2):**
  - an `Executor` protocol in `trader/execution/executor.py`, covering
    `execute(quote) -> SwapResult`, `balances()`, `fetch_costs(result)`,
    `balances_track_fills` and `native_fee_reserve`;
  - two implementations: `OnChainExecutor(keypair, rpc)`, where the keypair is
    required, and `SimulatedExecutor(wallet, fee_lamports)`;
  - `AsyncJupiterProvider` keeps quoting, the price-impact cap and retries,
    and delegates execution to its executor;
  - `PaperJupiterProvider` and `_NoRPC` go away;
  - `AsyncAccount` reads the fee reserve from the venue, and the bot's
    `mode != DRY` check becomes `balances_track_fills`.
- **Gateway as the only ledger path (backlog 1.3, 2.9):**
  - `TradeGateway.record_fill(intent, order, pnl)` commits `mark_executed` and
    `attach_order` in one transaction;
  - `restore(account_id)`, `add_event(...)`, and
    `TradeGateway.for_mode(mode, policy=...)`;
  - `halt` keeps a path that works without a policy;
  - `AsyncAccount` stops touching `gateway.ledger`.
- **Relocations:**
  - **1.6:** `trader/models/bot_config.py` moves to `trader/bot/config.py`.
  - **2.11:** the swap error classes move to `trader/models/errors.py`.
  - Remove the matching exceptions from `tests/test_architecture.py`.
- **`trader/trading_service/protocol.py`** (core):
  - `BucketSnapshot`, `OrderRequest` and `OrderReply`, all
    JSON-serialisable;
  - they reuse `order_to_json` / `order_from_json` from
    `trader/ledger/ledger.py`. Those move to `trader/models/order.py` if the
    layering test asks for it.
- **`trader/trading_service/service.py`:** `TradeService(gateway, provider)`.
  - It has one intent factory, which also owns the idempotency-key default
    (backlog 2.10).
  - `open_bucket(spec_id, budget)` creates one `AsyncAccount` per bucket,
    with `account_id="<mode>:strategy:<id>"` and `source="spec:<id>"`, and
    calls `restore_from_ledger()`.
  - It also provides `get_bucket`, `submit_order` and `close_bucket`.
  - An `asyncio.Lock` serializes submits.
  - `AsyncAccount` gets an optional `spend_cap` and forces a balance refresh
    before each buy.
  - The request's `rationale` is copied onto the intent. This fills the
    ledger column that is unused today.
- **`trader/trading_service/client.py`:** the `TradeClient` protocol and
  `LocalTradeClient`.
- **`trader/bot/async_websocket_bot.py`:**
  - It takes a `TradeClient` plus a read-only market-data provider, instead
    of building `AsyncAccount` itself.
  - It passes `bucket.available_usd` to the strategy as the balance.
  - A public `arun()` is added.
  - It stops when the bucket is `retiring`.
  - It gets warm-up candles from `strategy.warmup()` when the strategy has
    one.
  - It must keep the provider call order that `test_async_websocket_bot.py`
    asserts.
- **`main.py run` / `backtest`:** rewired onto `TradeService` +
  `LocalTradeClient` with an implicit bucket. Legacy strategies keep working.
  `main.py start` is removed (backlog 2.10).
- **Layering:** after this phase the bot loop (`trader/bot/`) moves to the
  **strategy-side** layer. It no longer imports `AsyncAccount` or the
  execution errors.
- **Tests:**
  - a bucket never spends above its cap, and the cap shrinks after a loss;
  - two buckets on one paper wallet never overspend (randomised ticks);
  - each bucket restores independently;
  - `close_bucket` produces exactly one sell.

### Phase 3 — Registry, policy extension, submit
- **Policy:** `trader/policy/policy.py` gets a `[strategies]` section, plus
  `[<mode>.strategies]` overrides, parsed into `StrategyLimits`.
  - Keys:
    - `max_strategy_budget_usd`
    - `max_total_allocated_usd`
    - `max_active_strategies`
    - `max_strategy_days`
    - `backtest_candles`
    - `backtest_fee_bps`
    - `min_backtest_trades`
    - `max_backtest_drawdown_pct`
    - `min_backtest_return_pct`
    - `min_paper_days_for_approval`
  - Extend `_SECTIONS` / `_settings`, and keep rejecting unknown keys.
  - Update `policy.example.toml` and `policy.toml`.
- **Ledger:** `trader/ledger/ledger.py` gets a new `strategies` table.
  - It is created in `_SCHEMA`; any later column goes through `_migrate`.
  - Columns: `id`, `status`, `spec_json`, `backtest_json`, `created_at`,
    `updated_at`, `approved_by`, `reason`.
  - Methods: `insert_strategy`, `get_strategy`, `list_strategies` and
    `set_strategy_status`.
  - Each write emits an event: `strategy_submitted`, `_rejected`,
    `_activated`, `_retiring`, `_retired` or `_approved`.
- **Statuses:**
  - paper: `rejected | active | retiring | retired`;
  - real: `approved | retiring | retired`.
- **`agent_api.strategies.submit`:**
  1. Validate the spec.
  2. Run the fixed-window gate.
  3. Check the active-strategy and total-allocation caps.
  4. Store the spec as `active`, or as `rejected` with the reasons. Either way,
     store the hash of the candle window with it.

  Resubmitting the same content does nothing.
- **Other `agent_api.strategies` functions:** `list`, `show`, `retire` and
  `perf`. Also `policy_show()`, which reports the limits and the remaining
  budget from `ledger.policy_state()`.
- **Tests:**
  - resubmitting is idempotent;
  - every gate threshold;
  - the caps;
  - `verify_chain()` is clean;
  - policy parsing, including mode overrides and unknown keys.

### Phase 4 — Trade-runner and strategy-runner processes
- **Backlog items in this phase:**
  - **2.4:** notifications are sent by the trade-runner, which sees every
    fill, through an async `send_message` (`httpx.AsyncClient`) scheduled as a
    task. They never block the submit lock.
  - **2.8:** consecutive identical denials, keyed by (account, side, reasons),
    are collapsed into one ledger row with a counter.
- **`trader/runners/trade_runner.py`:** `TradeRunner(mode)`. It:
  1. takes `data_dir()/trader-<mode>.lock` and writes
     `data_dir()/trader-<mode>.json`;
  2. serves `asyncio.start_server` on `127.0.0.1`;
  3. on `hello`, re-validates the spec against its own policy and opens the
     spec's bucket;
  4. polls the registry every 30s, and calls `close_bucket` and marks the
     spec `retired` when it is retiring, has expired or has hit
     `max_loss_usd`;
  5. at startup, runs an aggregate reconcile. For each mint, the sum of the
     bucket positions must be at most the wallet balance. If it isn't, the
     runner records an event and blocks buys on that mint.
- **`trader/trading_service/remote.py`:** `RemoteTradeClient`, with
  reconnect and backoff. A resent `submit_order` reuses its idempotency key.
- **`trader/runners/strategy_runner.py`** (strategy-side): runs one spec. It
  wires `JupiterMarketData`, `SpecStrategy` and `RemoteTradeClient` into the
  bot loop.
- **CLI:**
  - `main.py trader serve <mode>` (owner);
  - `main.py strategy run <id> [--trader FILE]`;
  - optionally `strategy run-all`, which spawns one subprocess per active
    spec.
- **Tests:**
  - a round trip over a localhost socket;
  - a bad token, an inactive spec, or a duplicate connection is rejected;
  - a reconnect followed by a resend executes the order once;
  - a strategy-runner killed while holding a position, then retired, results
    in exactly one sell, made by the trade-runner;
  - the strategy-runner works with `SOLANA_PRIVATE_KEY` and `HELIUS_RPC_URL`
    unset.

### Phase 5 — Real-mode approval and agent guardrails
- **`strategy approve <id>`** (owner only).
  - It requires a TTY and the owner typing the id.
  - The spec must have a minimum paper age and minimum paper performance.
  - It re-validates the spec against `load_policy(mode="real")`.
  - It inserts the spec into the real ledger as `approved`.
  - The real trade-runner accepts approved specs only.
- **`.claude/settings.json`:**
  - `permissions.allow` for the agent commands in §4;
  - `deny` for `Read(.env)`, `Edit/Write(policy.toml)` and the owner commands.
- **`.claude/hooks/block_owner_commands.py`:** a PreToolUse hook that blocks
  `strategy approve`, `trader serve real`, `resume`, `ledger resolve`,
  `paper reset`, `TRADER_POLICY_FILE` and `sqlite`. It has unit tests.
- **Agent guide:** `docs/agent-guide.md` plus
  `.claude/skills/strategy-author/SKILL.md`. The guide describes the loop:
  1. run `policy show` and `market symbols`;
  2. run `market summary` and `market candles`;
  3. write the spec;
  4. run `validate`, then `backtest`, then `submit`;
  5. later, run `perf`, then `retire` or submit a replacement with
     `supersedes`.
- **Docs:** update `plan.md`, `AGENTS.md`, `architecture.md` and
  `refactoring-backlog.md`.
- **Say plainly** that Claude Code rules and hooks are guardrails, not a
  sandbox. Use a low-balance hot wallet for real mode.

### Phase 6 — Later
- The `expr` condition type: a restricted parser that compiles to the Phase 1
  comparison nodes.
- An MCP server over `trader/agent_api`.
- Transaction inspection in the trade-runner (`plan.md` §5.3).
- A shared price-websocket hub.
- Per-bucket hourly limits.
- Deprecating the composer strategies.
- USD value for non-stable inputs (backlog 1.2), which lifts the USDC/USDT-only
  rule for specs.

## 6. Progress

| Phase | Status | Notes |
|---|---|---|
| 0 — Prerequisites + guard rails | **done** (2026-09-28) | 315 tests (12 new); ruff, format and pyright clean. Built on the data-path refactor (`trader/paths.py`, uncommitted at the time). Nothing committed yet. |
| 1 — Spec + indicators + local backtest | not started | |
| 2 — TradeService + buckets | not started | |
| 3 — Registry + policy + submit | not started | |
| 4 — Trade-runner / strategy-runner | not started | |
| 5 — Approval + guardrails | not started | |
| 6 — Later | not started | |

## 7. Decision log

- **2026-09-28:** agents author strategies and never trades. The spec is
  declarative JSON, the interface is CLI + JSON, mints come from the owner
  registry only, specs auto-activate in paper, and the owner approves real
  mode.
- **2026-09-28:** strategy-runners are separate processes, one per spec, each
  with its own bucket. They connect to one trade-runner per mode, which owns
  the gateway, the key and the ledger. Strategies are mode-agnostic. The
  budget is enforced by the bucket, not by making the policy account-aware.
- **2026-09-28:** specs are stored in the ledger, not as files, so that
  approval can copy a spec into the real ledger and nothing the agent runs
  writes there.
- **2026-09-28:** the IPC is JSON lines over localhost TCP with a token.
  asyncio on Windows has no Unix sockets, and this needs no new dependency.
- **2026-09-28:** separation is enforced by a layering test
  (`tests/test_architecture.py`), not only by convention. Existing modules
  are not moved in a big bang: misplacements are documented exceptions that
  are removed phase by phase. All open refactoring-backlog items were
  re-checked and scheduled (§5.0).
