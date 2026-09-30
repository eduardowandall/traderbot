# Plan

Status as of 2026-09-29. This is the only roadmap. It replaces the earlier
agent-readiness plan, `agent-strategies.md` and `refactoring-backlog.md`;
everything still open from them is carried over below. For how the code works
today, read [`architecture.md`](architecture.md).

Track progress in §6 and record decisions in §10. Plan here first, then
implement.

---

## 1. End goal

1. **One gateway for humans and agents to create strategies**, which then run
   automatically. An agent and the owner use the same commands, the same spec
   format and the same checks.
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

**Overall: about 63% of the end goal** (67% before stage U, which removed
manual trades and the agent JSON commands to make the code easier to follow;
58% after the first recheck). The foundations (safety, ledger, execution seam,
spec format) are in place and the order path is short. What is missing is
mostly the part that makes strategies *live*: storing them, running many at
once, and approving them for real money.

| Goal | Done | Missing | Score |
|---|---|---|---|
| 1. One gateway to create strategies that run automatically | Spec format; `run` validates the spec against the mode's policy and runs it in its own capped bucket with a max-loss stop; `backtest` prints JSON | Agent-facing commands (schema, market data), storing specs (`submit`), a registry, runners that start stored specs, real-mode approval | **35%** |
| 2. Manual trades | Removed in stage U (no `swap` command) | Comes back with B2's wallet allocation | deferred |
| 3. Structured strategy building | Spec v1: 18 condition types, required stop, converged warm-up, re-arm after exits, `ttl_days`; backtests replay direction-aware OHLC paths with slippage and refuse too little data; feed gaps cool the spec down; cooldown, re-arm and expiry survive restarts | One sizer, USDC/USDT inputs only, no expression language or crossovers | **78%** |
| 4. One wallet, bucket per strategy | Buckets with budget and max-loss caps; one bucket per spec id; per-bucket ledger account; partial exits kept per bucket; atomic authorization across processes | One strategy per process; no wallet treasury (a bucket without a budget can spend another's tokens, B2); no aggregate reconcile | **50%** |
| 5. Accurate trades and costs | Real amounts and fees from the confirmed tx; rent; net PnL; USD values on every pair; fills never lost after EXECUTED; partial sells keep their cost basis | No mark-to-market; paper fills are the quote's; failed-tx fees are no longer recorded (no resolve) | **88%** |
| (Foundations: safety, tests, layering) | Policy, breaker, idempotency, UNCONFIRMED blocking, 466 tests, enforced layering | Key still shares a process with strategies | 85% |

The overall figure is the plain average of goals 1, 3, 4 and 5 (goal 2 is
deferred).

### 2.1 What has been built

Stages A, R and S are done; their designs and progress notes are in
[`history.md`](history.md).

## 3. Target architecture

```
 owner (CLI)            agent (Claude Code)          <- same commands, same spec format
      \                      /
       strategy submit / list / perf / retire         (writes the paper ledger only)
       strategy approve  (owner only)                 (copies a spec into the real ledger)
                 |
 strategy-runner (spec A)   strategy-runner (spec B)   <- no key, no ledger, no mode
   MarketData (read-only)     MarketData (read-only)
   SpecStrategy               SpecStrategy
   TradeClient --+            TradeClient --+
                 |  hello / get_bucket / submit_order / heartbeat
                 v                          v
        trade-runner (one per mode; the ONLY process with key, wallet and ledger)
          TradeService: one bucket per spec, plus a `manual` bucket
            -> bucket check (spend cap = budget - losses)
            -> TradeGateway.submit (idempotency -> policy -> ledger -> execute)
            -> Executor (on-chain, or the shared SimulatedWallet in paper)
          owns: retire / expiry / max_loss exits, aggregate reconcile, notifications
```

### 3.1 Principles

- **Agents author strategies; they never trade, touch the key or edit the
  policy.** Humans use the same path. A manual `swap` is the only other way to
  trade, and it goes through the same gateway.
- **The policy decides deterministically.** `evaluate()` is pure and owned by
  the owner. It stays wallet-wide; per-strategy budgets are enforced by the
  bucket.
- **Execute exactly once.** One broadcast per idempotency key; nothing is
  retried after a send.
- **The ledger is the source of truth** for positions, PnL, budgets and the
  audit trail, and is reconciled against the wallet.
- **Fail closed.** No kill-switch reading, no ledger, or a broken policy
  means no trade.

### 3.2 Buckets

- A bucket is `account_id = "<mode>:<name>"`: `strategy:<id>` for a spec, the
  pair for the legacy `run`, `manual` for manual swaps.
- With no open position a bucket may spend
  `max(0, budget_usd + min(0, realized_usd))`. Buckets are long-only, one
  position at a time.
- The bucket check runs before `policy.evaluate`.
- On activation, the sum of active budgets must be at most
  `max_total_allocated_usd`. At validation, sizing must be at most
  `max_trade_usd` (otherwise every buy is silently denied).
- **Exits don't depend on the strategy process.** On retire, expiry or
  `max_loss_usd`, the trade-runner sells the bucket's position itself. Sells
  skip budget rules.

### 3.3 Wire protocol (JSON lines on `127.0.0.1`, token auth)

The trade-runner holds `data_dir()/trader-<mode>.lock` and writes
`{port, token}` to `data_dir()/trader-<mode>.json` (removed on exit). TCP
because asyncio on Windows has no Unix sockets.

| Message | Reply | Notes |
|---|---|---|
| `hello {spec_id, token}` | `{bucket}` or an error | Rejects a bad token, an inactive spec, or a second connection for the same spec |
| `get_bucket` | `{available_usd, position, realized_usd, status}` | `status: retiring` tells the strategy-runner to exit |
| `submit_order {side, quantity, price, rationale, idempotency_key}` | `{filled: order}` / `{denied: reasons}` / `{rejected}` / `{error}` | The strategy-runner creates the key; a resend after reconnect is deduplicated |
| `heartbeat` | `ok` | A missed heartbeat is only logged; exits never depend on it |

The strategy-runner finds the trade-runner with `--trader <file|host:port>`.
It has no mode argument.

### 3.4 Layers

Every module belongs to one layer, and `tests/test_architecture.py` fails on
a disallowed import or an unmapped module. The table is in
[`architecture.md`](architecture.md) step 11. The rule that matters most:
**strategy and strategy-side code never import execution, venue or risk.**

Planned modules and their layers: `trader/trading_service/remote.py`
(`RemoteTradeClient`, strategy-side), `trader/runners/strategy_runner.py`
(strategy-side), `trader/runners/trade_runner.py` (app), `trader/cli/` (app).

### 3.5 Commands

Since stage U only `run` and `backtest` exist; this table is the stage B
target, and each command comes back with the item that needs it.

Agent and owner commands print one JSON object on stdout
(`{"ok": true, ...}` or `{"ok": false, "errors": [{"path","msg"}]}` with exit
code 1; decimals are strings; `ensure_ascii=True`).

| Command | Who | Purpose |
|---|---|---|
| `market symbols / price / candles / summary` | both | Registry and market data (done) |
| `policy show` | both | Limits, strategy caps and remaining budget |
| `strategy schema / validate / backtest` | both | Local checks, nothing stored (done) |
| `strategy submit FILE` | both | Validate, run the fixed-window gate, store; auto-activates in paper |
| `strategy list / show ID / perf ID` | both | Registry state and bucket PnL vs the stored backtest |
| `strategy retire ID` | both | Mark `retiring`; the trade-runner closes the position |
| `strategy run ID [--trader FILE]` | both | Start a strategy-runner (no mode) |
| `wallet show <mode>` | both | Wallet total, allocation per bucket, unallocated funds |
| `strategy approve ID` | owner | Copy a paper-proven spec into the real ledger |
| `trader serve <mode>`, `swap`, `halt/resume`, `ledger resolve`, `paper reset`, `policy.toml` edits | owner | Operations |

## 4. Decisions that stand

| Topic | Decision |
|---|---|
| Strategy form | A **declarative JSON spec** from vetted blocks. A restricted expression language comes later as one more condition type. Never agent-written Python, never `eval`. |
| Interface | **CLI with JSON output**. Today only `backtest`; the agent commands return with B1. MCP can wrap them later. |
| Mints | **Owner registry only** (`SOLANA_MINTS` ∩ `allowed_symbols`). |
| Activation | **Automatic in paper; the owner approves real.** |
| Processes | One strategy-runner per spec; one trade-runner per mode holding the gateway, key and ledger. Strategies are mode-agnostic. |
| Budgets | Per-strategy budget in the bucket; `policy.evaluate()` stays wallet-wide. |
| Spec storage | In the ledger (`strategies` table), so nothing an agent runs writes to the real ledger. |
| Wallet | One wallet for all strategies for now. Real mode uses a dedicated low-balance hot wallet. |

## 5. Roadmap

Refactorings that make the features cheaper come first (stages A and U),
then the features (stage B). Each item ships on its own with the suite green (`ruff
check`, `ruff format --check`, `pyright`, `pytest`). Size: **S** under an hour,
**M** several modules, **L** a design change.

### Stage U — Simplification (added 2026-09-30)

The owner found the code hard to follow: where a trade goes and where it came
from. A paper buy went through about 11 objects and 45 calls, of which five do
real work, and changed shape 9 times; the CLI had 18 commands, with two
backtests that disagreed on the same spec; dry mode added about 20 mode checks
and still needed a real key. The owner's decisions for this stage:

- **Two modes:** `real` and `paper`. Dry mode is removed.
- **Two commands:** `run <real|paper> spec.json` and `backtest spec.json`; the
  mode has no default. `swap`, `halt`/`resume`, `ledger *`, `pnl`, `paper *`,
  `market *` and `strategy *` are deleted with the code only they used. Stage
  B adds commands back when it needs them.
- **One backtest engine** (the spec's budget, the OHLC path replay, the
  warm-up check), JSON output.
- **No kill switch.** Stopping the process stops trading. The circuit breaker
  counts failures since the process started, so a restart re-arms it.
- **UNCONFIRMED intents stay blocked**; recovery is moving or deleting the
  ledger file.
- **The `events` table stays; the hash chain and `verify` go.**
- **Order:** U1, then stage T (minus the items U1 makes obsolete), then U2 and
  U3.

#### U1. Cut and delete — M
1. Dry mode: `RunningMode.DRY`, the `MODES` tuple (derived from the enum),
   `is_dryrun` in `OnChainExecutor`/`AsyncRPCClient`/`on_chain`, the `[dry.*]`
   policy sections.
2. CLI: only `run` and `backtest`; delete `trader/cli/{swap,safety,ledger,pnl,paper}.py`,
   `trader/agent_api/`, `trading_service/manual.py` (+ `SwapRequest`,
   `TradeService.swap`), `execution/resolve.py`. Telegram turns on when
   `TELEGRAM_CHAT_ID`/`TELEGRAM_BOT_TOKEN` are set (no `--notification-*`).
3. One backtest engine in `trader/backtest/`; the close-only replay and the
   no-interval `ticks_from_candles` branch go.
4. Ledger: no hash chain; delete the queries only the removed commands used;
   one `_SCHEMA` without the legacy migrations (an old-format file is refused
   with a "move or delete it" error), after checking that the real ledger
   holds no trades.
5. No kill switch; breaker since process start.
6. Dead code: `botconfigs.example.yaml`, the `spread` and `type_order`
   parameters, `AsyncJupiterProvider.swap`, `stop_when_error`, unused
   `TickerData` fields, `PositionType`, `SPEC_VERSION`,
   `NullNotificationService`, unused re-exports, `wiring.build_gateway`,
   duplicate helpers.
7. Defaults: roomy paper limits in code; `policy.toml` untracked
   (`policy.example.toml` stays).
8. Dev tooling: `check.py`/`spec_check.py`/`smoke.py` follow the new CLI
   (`.claude/scripts/ledger_dump.py` replaces the ledger commands for
   `/smoke` and `/diagnose`); CI drops the redundant `uv python install`;
   unused dev dependencies go; the bot test that pinned 17 RPC calls in order
   uses a `FakeTrader`; the architecture map drops modules that don't exist.

#### U2. Straighten the order path — M (after stage T)
1. `trader/async_account.py` → `trader/execution/account.py`.
2. The service passes the spend cap to `buy` as a value (no `spend_cap`
   callback).
3. One call dispatches on side (no `_place` → `_reply(lambda)` →
   `place_order`); each exception is logged once.
4. One balance read per order.
5. The provider takes the spend amount from the intent instead of
   recomputing `quantity * price`.
6. One per-tick decision shared by the bot and the backtester. The backtester
   then keeps selling a retiring bucket that still holds a position, like the
   live bot (an intended fix).
7. One `Strategy` protocol in `bot/config.py` replaces `TradingStrategy`,
   `WarmsUp`, `Resumes`; `SpecStrategy` owns its clock and rng.
8. `total_realized_pnl` folds into `pnl_totals` (EXECUTED only); no double
   `realized_pnl_detail()`.

#### U3. Docs you can follow — S
- `architecture.md` becomes a tour of one traced paper buy (numbered hops with
  file:line, what each is for, where state lives), then real mode, backtest
  and safety.
- `AGENTS.md` keeps commands and hard rules and points to `architecture.md`
  for the module map; README leads with paper mode.
- Example spec rationales match their conditions.

### Stage T — Third recheck findings (added 2026-09-29, NOT implemented)

The third review ran after stage S. Per the owner's instruction, this stage is
**written down but not implemented**, and waits for the owner's review. The
first three items are regressions or gaps in stage S itself and should come
first; I checked the excepthook override and the startup-resume gap in the
code.

**Approved by the owner on 2026-09-30, to run after U1.** U1 deletes `ledger
resolve`, so T4's "resolving a partial sell" and T6's two resolve items are
obsolete, and T6's pruning item no longer applies (the old log files are gone).

#### T1. Regression from S4: a fresh EXECUTING buy no longer blocks the bucket — high
- **Where:** `ledger/policy_state.py::_unresolved_ids`, together with
  `gateway.submit` and `AsyncAccount.buy`.
- **What goes wrong:** R1's fail-closed rule ("a failed `mark_executed`
  stays EXECUTING and blocks trading") relied on EXECUTING counting as
  unresolved. S4 exempted EXECUTING intents younger than 300s, so:
  1. a buy that executed but could not be marked (`database is locked`)
     returns an `error`;
  2. on the next tick the book is still empty, the policy passes, and a new
     random key is minted;
  3. the bot **buys again**.

  A hard kill mid-swap followed by a restart within 5 minutes does the same.
  A later `resolve` also reorders `legs_since_last_buy` (it orders by
  `updated_at`).
- **Fix:** EXECUTING intents of the **same account** always count as
  unresolved (a bucket lives in one process); the 300s grace applies only
  to other accounts. Alternatively, the gateway latches "blocked" in memory
  after a failed `mark_*`. `legs_since_last_buy` should order by
  `created_at`, or by execution time, not `updated_at`.

#### T2. Regression from S3: `_close_if_retiring` can recurse without bound — medium-high
- **Where:** `service.submit_order` → `_close_if_retiring` → `close_bucket`
  → `submit_order` again.
- **What goes wrong:** a rejected or failed remainder sell (the SOL
  reserve, the minimum size, HALT, the breaker, an RPC error) leaves the
  position open, so the cycle recurses at once: it re-authorizes, writes
  intents, can trip the breaker, and ends in `RecursionError`.
- **Fix:** attempt the close at most once per `submit_order` (a `closing`
  flag on `_Bucket`, or a direct `_place` sell), and retry on later ticks.

#### T3. S5 does not work: Typer replaces the redacting excepthook — high (secrets)
- **Where:** `main.py` and typer 0.27.1 (`typer/main.py:57`,
  `1134-1135`).
- **What goes wrong:** typer saves `sys.excepthook` when it is imported and
  reinstalls its own hook in `app()`. With pretty exceptions off, that hook
  calls the **original** (Python's default) hook, so an escaping exception
  still prints an unredacted traceback, including a chained httpx2 error
  with the Helius key. The S5 test only calls the hook function directly.
- **Fix:** in `main.py`, wrap `app()` in try/except and route the exception
  through `redacted_excepthook` before exiting with code 1 (or patch
  `typer.main._original_except_hook`). Add a subprocess test that raises a
  secret-bearing error.

#### T4. Stage S gaps in state and restore — medium
- **Startup resume.** A transient error on `trader.bucket()` at startup
  permanently skips `strategy.resume()`, because `_opened` is set first, so
  the restored cooldown, re-arm and `ttl_days` clock are lost. **Fix:** a
  separate `_resumed` flag.
- **Long gaps.** A gap of `history` bars or more (the laptop slept, a long
  backoff) fills the whole window with flat bars while `_warm` stays true:
  RSI goes to 0 or 100 and volatility to about 0, which can trigger entries.
  The same happens at the seams of an appended `--record-ticks` file.
  **Fix:** a gap longer than a few bars resets warm-up and re-seeds from
  candles; replays reset the series at a seam.
- **`ttl_days` for a spec that never traded.** `opened_at` comes from the
  first intent, so a spec that never fired restarts its clock on each
  restart. **Fix:** a `bucket_opened` event on the first `open_bucket`
  (later B1's `strategies.created_at`).
- **Late `mark_executed` on a resolved row.** `_transition` skips the
  status change, but the caller still records the fill: `attach_order`
  writes the order and PnL onto a FAILED row. `total_realized_pnl` doesn't
  filter on status, and `_add_failed_fees` counts that swap's fee as a
  failed-transaction fee. **Fix:** `_transition` reports the skip; the
  gateway raises; `attach_order` requires EXECUTED; `total_realized_pnl`
  filters on EXECUTED.
- **Resolving a partial sell.** `resolved_fill` always closes the position,
  so resolving a partial sell drops the remainder. **Fix:** mirror
  `AsyncAccount.sell` with `remainder_entry` / `reduce` /
  `closes_position=False`.
- **A capped sell whose `record_fill` failed.** Memory closes the position,
  but restore reopens a remainder over 1% from the intent's quantity, which
  since S3 happens at the next order. That leaves an unsellable remainder
  that blocks buys. **Fix:** store the closes-position flag on the intent
  row.

#### T5. Backtest bias that remains — medium (matters for the B1 gate)
- The path is always open → low → high → close. That is conservative for
  stops but **optimistic** for a dip entry followed by a take-profit in the
  same bar: a probe made 30 round trips, every one inside a single bar.
- **Fix:** use open → high → low → close when close < open (the usual
  heuristic), or forbid a non-stop exit in the bar of the entry.

#### T6. Resolve and CI gaps — medium/low
- **`ledger resolve ... executed` in real mode is not re-runnable.** It uses
  `fetch_swap_costs`, which never raises, so an RPC failure, a missing
  transaction or a missing signature silently falls back to the intent's
  requested amounts, and the intent is then resolved for good. **Fix:**
  call `executor.fetch_costs` (it raises); abort without writing unless
  there is `--estimate`.
- **Status check too late.** Resolving an already-resolved intent does the
  network work first, then crashes with a traceback. **Fix:** check the
  status right after `ledger.get` and raise `BadParameter`.
- **Unclosed ledgers pass the tests.** `-W error::ResourceWarning` does not
  catch an unclosed sqlite connection, because pytest reports it as a
  `PytestUnraisableExceptionWarning`, which is only a warning. **Fix:** add
  `-W error::pytest.PytestUnraisableExceptionWarning`.
- **Pruning misses the old files.** `prune_logs` only matches `trader-*`, so
  the 116 older files (231 MB, including the six with the leaked key) stay.
  **Fix:** widen the pattern, or say so in §9.
- **Paper wallet on Windows (suspected).** `os.replace` can fail while
  another process reads the file outside the lock, and the lock wait uses a
  blocking `time.sleep` inside the event loop. **Fix:** read under the lock
  (or retry the replace), and use an async wait.
- **Docs:**
  - `architecture.md` still says entries wait for "the largest
    `lookback()`" (it is `spec.history()` now);
  - README omits `--slippage-bps`.

### Stage B — Features

#### B1. Strategy registry and `submit` — M
The unified entry point for humans and agents.
- **Policy:** a `[strategies]` section plus `[<mode>.strategies]` overrides,
  parsed into `StrategyLimits`: `max_strategy_budget_usd`,
  `max_total_allocated_usd`, `max_active_strategies`, `max_strategy_days`,
  `backtest_candles`, `backtest_fee_bps`, `min_backtest_trades`,
  `max_backtest_drawdown_pct`, `min_backtest_return_pct`,
  `min_paper_days_for_approval`. Unknown keys still fail. Update
  `policy.example.toml` and `policy.toml`. `DEFAULT_MAX_DAYS` moves here.
- **Ledger:** a `strategies` table (`id`, `status`, `spec_json`,
  `backtest_json`, `author`, `created_at`, `updated_at`, `approved_by`,
  `reason`), created in `_SCHEMA`, later columns through `_migrate`. Every
  status change is also an event (`strategy_submitted`,
  `_rejected`, `_activated`, `_retiring`, `_retired`, `_approved`).
- **Statuses:** paper `rejected | active | retiring | retired`; real
  `approved | retiring | retired`.
- **`submit`:** validate → fixed-window backtest gate (the policy fixes the
  window, so backtests can't be cherry-picked) → active-count and
  total-allocation caps → store as `active` or `rejected` with reasons and
  the hash of the candle window. Resubmitting the same content is a no-op.
  Specs are immutable (`id = sha256(canonical JSON)[:12]`); a change is a new
  spec with `supersedes`.
- **Also:** `strategy list/show/perf/retire` and `policy show` (limits and
  remaining budget from `policy_state()`). `author` is the spec's `agent_id`,
  or `owner`.
- **Tests:** idempotent resubmit, every gate threshold, the caps, policy parsing with mode overrides and unknown keys.

#### B2. Wallet allocation and reconciliation — M
- `wallet show <mode>`: wallet balances, each bucket's budget, open position
  and realized PnL, and the **unallocated** remainder.
- Manual swaps may spend only unallocated funds and may not sell a token that
  an open bucket position holds (unless `--force`, owner only).
- **Aggregate reconcile:** for each mint, the sum of bucket positions must be
  at most the wallet balance. A mismatch records an event and blocks buys on
  that mint until resolved.
- **Wallet treasury (from the recheck):** one balance cache in
  `TradeService`, invalidated on every fill; today each account caches the
  shared wallet separately for up to 3 minutes. Per-bucket reservations of
  quote allocation, token holdings and the fee reserve, so a bucket without a
  budget can no longer spend SOL that another bucket holds as its position.
  Budgets become USD values converted with the A4 oracle; today `_cap` (USD)
  is compared with input-token amounts, which is only safe for stable
  inputs. The ATA rent is tracked as refundable.
- (The `ledger resolve` order fix moved to R1.)

#### B3. Trade-runner and strategy-runners — L
- **`trader/runners/trade_runner.py`** (`trader serve <mode>`, owner):
  1. takes the lock and writes the connection file (§3.3);
  2. serves on `127.0.0.1`;
  3. on `hello`, re-validates the spec against **its own** policy (submit-time
     validation is advisory: an agent could point `TRADER_POLICY_FILE` at a
     looser file) and opens the bucket;
  4. every 30s closes buckets that are retiring, expired or past
     `max_loss_usd`, and marks them `retired`;
  5. runs the aggregate reconcile (B2) at startup;
  6. sends notifications for every fill (A8), never under the submit lock.
- **`trader/trading_service/remote.py`:** `RemoteTradeClient` with reconnect
  and backoff; a resent `submit_order` reuses its idempotency key.
- **`trader/runners/strategy_runner.py`:** `JupiterMarketData` + `SpecStrategy`
  + `RemoteTradeClient` in the existing bot loop (the bot code doesn't
  change).
- **CLI:** `strategy run <id> [--trader FILE]`, and `strategy run-all` (one
  subprocess per active spec).
- **Tests:** socket round trip; bad token, inactive spec and duplicate
  connection rejected; reconnect + resend executes once; a strategy-runner
  killed while in a position, then retired, gives exactly one sell by the
  trade-runner; the strategy-runner works with `SOLANA_PRIVATE_KEY` and
  `HELIUS_RPC_URL` unset.

#### B4. Real-mode approval and agent guardrails — M
- **`strategy approve <id>`** (owner): needs a TTY and the owner typing the
  id; requires minimum paper age and performance; re-validates against
  `load_policy(mode="real")`; inserts the spec into the real ledger as
  `approved`. The real trade-runner accepts approved specs only.
- **`.claude/settings.json`:** allow the agent commands of §3.5; deny
  `Read(.env)`, `Edit/Write(policy.toml)` and the owner commands.
- **`.claude/hooks/block_owner_commands.py`:** a PreToolUse hook blocking
  `strategy approve`, `trader serve real`, `resume`, `ledger resolve`,
  `paper reset`, `swap real`, `TRADER_POLICY_FILE` and `sqlite`, with unit
  tests.
- **Agent guide:** `docs/agent-guide.md` and
  `.claude/skills/strategy-author/SKILL.md`: `policy show` → `market ...` →
  write the spec → `validate` → `backtest` → `submit` → later `perf`, then
  `retire` or a replacement with `supersedes`.
- Say plainly that Claude Code rules and hooks are guardrails, not a sandbox.
  Real mode runs on a dedicated low-balance hot wallet.

#### B5. Accounting and performance reports — M
- Mark-to-market: unrealized PnL per bucket at the current price, in
  `wallet show` and `strategy perf`.
- `strategy perf` compares live bucket results with the stored backtest.
- A daily report (fills, costs, PnL per bucket) through the notifier.
- Better simulated fills: a slippage model for paper, and the network fee in
  replays, so paper/backtest PnL is closer to real.

#### B6. More expressive strategies — M
- Sizers: percent of the bucket (now meaningful, since buckets exist).
- The `expr` condition type: a restricted parser that compiles to the same
  comparison nodes.
- Lift the USDC/USDT-only input rule for specs and backtests (needs A4): feed
  strategies the output priced in input-token units (`price(out) / price(in)`)
  and replay two price series.
- Adding a condition type stays a three-place change (model, union,
  `PREDICATES`), checked by a test. (Porting the legacy strategies moved to
  A11.)

#### B7. Later
- An MCP server over the agent commands (once they come back with B1).
- Transaction inspection in the trade-runner: only Jupiter, Token,
  Token-2022, ATA, ComputeBudget and System programs; the only decreasing
  balance is the wallet's source account, by at most the quoted `inAmount`.
- Market-sanity checks: quote within 2% of the independent price, reject
  market data older than 30s.
- A shared price-websocket hub for many strategy-runners.
- Per-bucket hourly limits.
- Human approval for large trades (Telegram inline buttons), then graduated
  autonomy.
- A service entrypoint (systemd, Docker or a Windows service).

## 6. Progress

| Item | Status | Notes |
|---|---|---|
| A0–A12, R0–R9, S1–S8 | **done** (2026-09-23 → 30) | See [`history.md`](history.md) |
| U1 Cut and delete | **done** (2026-09-30) | 456 tests (was 540). CLI is `run` + `backtest`; dry mode, kill switch, hash chain, manual swap, agent/ledger/pnl/paper commands gone; `trader/` 8,989 -> 7,722 lines, `tests/` 8,739 -> 7,564. The old ledgers in `.data/` are refused (old format) |
| T1 EXECUTING blocks own bucket (S4 regression) | **done** (2026-09-30) | An EXECUTING intent of the same account always blocks (`policy_state(account=)`); legs ordered by `created_at` |
| T2 Bounded remainder close (S3 regression) | **done** (2026-09-30) | `_Bucket.closing`: one remainder sell per order (the test hit `RecursionError` without it) |
| T3 Excepthook overridden by Typer (S5 gap) | **done** (2026-09-30) | `main.main()` routes an escaping error through `redacted_excepthook`; subprocess test with a fake api-key |
| T4 State/restore gaps | **done** (2026-09-30) | `_resumed` retried apart from `_opened`; gaps over `MAX_GAP_BARS` restart the series and cool the spec; `bucket_opened` event starts `ttl_days`; late marks are reported and `attach_order` needs EXECUTED; `closes_position` on the intent row. The resolve item is obsolete (U1) |
| T5 Same-bar entry+exit bias | **done** (2026-09-30) | Falling bars replay open -> high -> low -> close |
| T6 Resolve/CI/docs gaps | **done** (2026-09-30) | `-W error::pytest.PytestUnraisableExceptionWarning`; wallet reads under the lock, `os.replace` retried on `PermissionError` (the lock wait stays synchronous: it is held for milliseconds). Resolve and log-pruning items obsolete; docs items in U3 |
| U2 Straighten the order path | **done** (2026-09-30) | `trader/execution/account.py`; the cap is `buy(limit_usd=)` (no callback); `TradeService._place`/`_execute` classify and dispatch, errors logged once; one balance read per buy; `provider.buy(spend_amount)`; `trader/bot/decision.py` shared by the bot and the backtester (a retiring bucket with a leftover keeps being replayed); one `Strategy` protocol (no `TradingStrategy`, `WarmsUp`, `Resumes`); `pnl_totals` replaces `total_realized_pnl`. Also removed the per-order `sync_with_ledger` (it only followed manual resolutions). Paper wallet file re-reads kept (only on cache misses) |
| U3 Docs you can follow | **done** (2026-09-30) | `architecture.md` (424 -> 174 lines) follows one paper buy hop by hop with file:line; README (192 -> 94) leads with paper; AGENTS.md (120 dense lines -> 113 short ones); example rationales fixed; `.env.example` lists every variable. Stage U total: `trader/` 8,989 -> 7,788 lines, 466 tests |
| B1 Registry + submit | not started | |
| B2 Wallet allocation + reconcile | not started | |
| B3 Trade-runner / strategy-runners | not started | |
| B4 Approval + guardrails | not started | |
| B5 Accounting reports | not started | |
| B6 More expressive strategies | not started | |
| B7 Later | not started | |

Owner task, outside the code: a one-week paper soak run.

## 7. Known issues and limitations

### 7.1 Money and accounting

- **Costs and net PnL (how it works today).**
  - After each swap the costs actually paid are recorded: the network fee
    (base + priority, from `meta.fee`), rent for token accounts the wallet
    opened (a cost, marked refundable), and other SOL the route charged.
    Fees of failed transactions are recorded too.
  - Real mode reads the confirmed transaction, including the real in/out
    amounts from the token-balance deltas. Paper simulates the base fee and
    first-time rent, and uses the quote's amounts.
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
- **Prices are USD, balances are in the input token.** `Order.price` is the
  real fill only for USDC/USDT inputs; `Order.fill_price` keeps the raw
  input-per-token price. Strategy sizing is only meaningful for stable
  inputs, which is why specs and backtests require them (B6 lifts this).
- **No commands to inspect or repair the ledger** (stage U). An UNCONFIRMED
  intent (a process killed mid-swap) blocks the mode's ledger until the file
  is moved or deleted; `.claude/scripts/ledger_dump.py` prints it read-only.
- **Paper fills use the quoted `outAmount`**, with no slippage model; replays
  ignore the network fee (B5).
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
  tests. Many strategy-runners on one keyless IP (B3) will need an API key
  or the shared price hub (B7).

### 7.3 Security

- The private key lives in the same process as strategy code until B3.
- `repr(Keypair)` is safe, but **`str(Keypair)` is the base58 secret**. Never
  format a keypair with `str()` or `{keypair!s}`.

## 8. Top risks

1. **A runaway or prompt-injected agent.** Mitigated: agents only author
   specs; policy and bucket budgets bound every trade; real
   needs owner approval.
2. **Double execution or lost confirmations.** Mitigated by idempotency
   keys, no re-send after broadcast, UNCONFIRMED blocking and reconciliation.
3. **Key compromise through agent-reachable code.** Mitigated by the
   trade-runner split (B3) and a low-balance hot wallet.
4. **Jupiter changes to undocumented endpoints.** Mitigated by the Price API
   fallback (A4) and configurable hosts.
5. **Buckets drifting from the wallet** (manual trades, hand-resolved
   intents). Mitigated by the aggregate reconcile and allocation rules (B2).
6. **Strategy quality.** Mitigated by the backtest gate, paper first, and
   `perf` against the backtest.

## 9. Open questions for the owner

- Rotate the Helius key if that hasn't happened yet (R0); the leaked log
  files are gone.
- Which pairs, and what maximum notional per trade and per day, are you
  comfortable with in real mode?
- May a manual swap spend funds allocated to a bucket, or only unallocated
  funds (the B2 default)?
- Approval channel for large trades later: Telegram inline buttons, CLI, or
  both?
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
