# Refactoring backlog

These are open refactoring items from the code-quality review of 2026-09-25
(`/simplify`, covering reuse, simplification, efficiency and altitude). The
fixes that were applied then are listed at the end for reference. Everything
above that section is still open.

File references name functions rather than line numbers, because line numbers
drift. Size: **S** is under an hour and self-contained, **M** touches several
modules, **L** is a design change.

**Scheduling (2026-09-28):** every open item was re-checked against the code,
and all of them are still present. Most are now scheduled into the phases of
[`agent-strategies.md`](agent-strategies.md) §5.0, which has the full table.
Each item below carries a *Scheduled* line.

---

## 1. Structural (design changes)

### 1.1 Execution interface for real and paper trading — **M**
- **Where:**
  - `trader/paper/provider.py` (`PaperJupiterProvider`, `_NoRPC`)
  - `trader/providers/jupiter/async_jupiter_svc.py` (`keypair: Keypair | None`,
    the `pubkey` guard, the guard in `_get_signed_transaction`)
  - `main.py` `_fetch_candles`, which builds a paper provider just to read
    candles
- **Problem:** Paper trading subclasses the real provider and then switches
  off its RPC and signing state: it passes `keypair=None` and a `_NoRPC`
  sentinel. Because of that, the real provider has to handle "no key" in
  several places.
- **Change:** Put one seam behind an `Executor` protocol:
  - `execute(quote) -> SwapResult`
  - `balances() -> list[MintBalance]`
  - an attribute such as `balances_track_fills`
  - a `native_fee_reserve` value

  There would be two implementations, `OnChainExecutor(keypair, rpc)` and
  `SimulatedExecutor(wallet, fee_lamports)`. `AsyncJupiterProvider` would take a
  `jupiter_client` and an executor, and would keep quoting, the price-impact
  cap and retries shared between them.
- **Unlocks:** 2.1 and 2.2 become trivial. `keypair` becomes required
  again, inside `OnChainExecutor`.
- **Scheduled:** agent-strategies Phase 1 (read-only `MarketData` split) and
  Phase 2 (`Executor`).

### 1.2 USD value for pairs whose input is not a stablecoin — **M**
- **Where:**
  - `trader/async_account.py` (`_notional_usd`, `_to_order` price selection,
    `_quote_is_usd`)
  - `main.py` `_execute_swap` (`quantity if mint_in.is_usd_stable else None`)
  - `trader/backtest/replay.py` (`Backtester.__init__` rejects non-stable
    inputs)
- **Problem:** The price feed is in USD, but balances and fills are in the
  input token. Four callers each work around that mismatch on their own. On
  pairs like `USDC-SOL`, the USD value of a trade (its notional) becomes
  `None`. Such trades are denied unless `allow_unknown_notional = true`. When
  they are allowed, they sit outside every USD budget.
- **Change:** Add one helper, e.g. `usd_value(mint, amount)`. It returns the
  amount as-is for USDC/USDT; for any other token it multiplies by that
  token's Jupiter USD price. Price the output token in input-token units using
  `price(output) / price(input)`, and use the helper for all notional values
  and order prices. This also fixes the sizing issue in `docs/plan.md` §3.1.
  - **Blocked by:** the Jupiter API review (the price feed currently
    subscribes to a single asset).
  - **Update 2026-09-25:** net PnL now uses rates taken from the trade itself
    (`trade_rates`). A SOL→USD price source would complete the cost conversion
    for pairs without SOL, which are flagged `[!] incompleto` today.
- **Scheduled:** agent-strategies Phase 6. Until then, strategy specs are
  limited to USDC/USDT inputs.

### 1.3 Gateway as the only path to the ledger — **S**
- **Where:**
  - `trader/async_account.py`: `restore_from_ledger`, `reconcile_position` and
    `_record_order` all call `self.gateway.ledger` directly.
  - `main.py`: `halt` writes events straight into the ledgers.
- **Problem:** The gateway is supposed to be the single path to the ledger,
  but the account also writes orders, reconcile events and restores directly.
- **Change:**
  - Add `TradeGateway.record_fill(intent, order, pnl)`, `restore(account_id)`
    and `add_event(...)`, so the account only talks to the gateway.
  - Add a `TradeGateway.for_mode(mode)` factory to replace `main._build_gateway`.
  - `halt` must keep working without loading the policy, so it needs a
    policy-free path, e.g. `for_mode(mode, policy=None)`.
- **Scheduled:** agent-strategies Phase 2.

### 1.5 The package init loads the strategy registry — **S** (new 2026-09-28)
- **Where:** `trader/__init__.py` imports `trading_strategy` to build
  `STRATEGIES`.
- **Problem:** every `import trader.<anything>` loads every strategy, which
  couples the package root to the strategy layer.
- **Change:** move the registry to `trader/strategies_registry.py` and leave
  `trader/__init__.py` empty.
- **Scheduled:** agent-strategies Phase 1.

### 1.6 Wiring lives in the models package — **S** (new 2026-09-28)
- **Where:** `trader/models/bot_config.py` imports `TradeGateway`,
  `AsyncJupiterProvider`, `TradingStrategy` and the notification services.
- **Problem:** `trader/models` is meant to be plain data with no
  dependencies, but this module pulls in execution, venue and strategy code.
- **Change:** move `BotConfig` / `create_bot_config` / `get_keypair_from_env`
  to `trader/bot/config.py`. Keep `RunningMode` in models.
- **Scheduled:** agent-strategies Phase 2.

---

## 2. Smaller items

### 2.1 The fee reserve belongs to the execution venue — **S** (easier after 1.1)
- **Where:**
  - `trader/backtest/replay.py` `Backtester.run` passes both `fee_lamports=0`
    and `sol_fee_reserve=Decimal("0")`.
  - `trader/async_account.py` `sol_fee_reserve` constructor parameter.
- **Problem:** Two settings have to agree on "this venue charges SOL network
  fees", and each caller has to know to set both.
- **Change:** The provider or executor exposes `native_fee_reserve`: 0.02 SOL
  on-chain, derived from `fee_lamports` for paper, 0 when fees are off.
  `AsyncAccount` reads it, and its constructor parameter goes away.
- **Why skipped:** a new provider attribute breaks the many
  `AsyncMock(spec=AsyncJupiterProvider)` mocks, because a spec'd mock returns a
  `Mock`, not a `Decimal`. Do it together with 1.1.
- **Scheduled:** agent-strategies Phase 2.

### 2.2 Replace the mode string check with a provider fact — **S** (after 1.1)
- **Where:** `trader/bot/async_websocket_bot.py` `_startup`
  (`if self.mode != RunningMode.DRY`).
- **Problem:** The real question is "do the provider's balances reflect our
  own fills?". That is true for real and paper, false for dry. It is a
  property of the provider, not of the mode.
- **Change:** Check a `balances_track_fills` attribute on the provider or
  executor.
- **Scheduled:** agent-strategies Phase 2.

### 2.3 Inject a clock into `AsyncAccount` — **S**
- **Where:** `trader/async_account.py`: the balance cache expiry in
  `get_balance`, and `Order.timestamp` in `_to_order`, both use
  `datetime.now()`.
- **Problem:** Backtest orders get wall-clock timestamps.
  `BacktestTrade.timestamp` already uses the tick time, but `Order.timestamp`
  does not.
- **Change:** Accept `clock: Callable[[], datetime]`, the same type as
  `TradingStrategy.set_clock`, and have the backtester pass the replay clock.
- **Scheduled:** agent-strategies Phase 1.

### 2.4 Move Telegram off the event loop — **S**
- **Where:** `trader/notification/notification_service.py`
  (`TelegramNotificationService.send_message` uses sync `requests.post` with
  `timeout=10`), called from `trader/bot/async_websocket_bot.py`.
- **Problem:** A slow Telegram API can freeze tick processing for up to 10
  seconds right after a trade.
- **Change:** Make it an async `send_message` using `httpx.AsyncClient`, or
  wrap it with `asyncio.to_thread`, or fire-and-forget it as a task. Also
  listed in `docs/plan.md` §3.2.
- **Scheduled:** agent-strategies Phase 4 (the trade-runner).

### 2.5 Log volume in strategies — **S**
- **Where:** `trader/trading_strategy.py` `TargetValueStrategy.on_market_refresh`
  logs an INFO line on every tick. The console filter shows
  `trader.trading_strategy` at DEBUG, and file logs capture DEBUG.
- **Problem:** The live bot writes several lines per second forever. A
  100k-tick backtest writes 100k console and file lines, which probably
  dominates its runtime.
- **Change:**
  - Log at DEBUG with lazy `%` arguments.
  - Log only when the target or state changes, or rate-limit the message.
  - Silence strategy loggers during `backtest`.
- **Why skipped:** it changes the log output people read.
- **Scheduled:** agent-strategies Phase 1, for new code and for silencing
  strategy logs in backtests. The old strategies' INFO lines stay until the
  composer strategies are deprecated.

### 2.6 Strategy class-level `clock` / `rng` defaults — **S**
- **Where:** `trader/trading_strategy.py` `TradingStrategy`. It declares
  `clock = staticmethod(datetime.now)` and a shared `rng = random.Random()` at
  class level, and `__init__` then overwrites both on the instance.
- **Problem:** The class-level `Random()` is shared hidden state. It is only
  used by subclasses that skip `super().__init__()`.
- **Change:** Make those subclasses (test fakes such as `FakeStrategy`) call
  `super().__init__()`, then keep annotation-only class declarations.
- **Scheduled:** agent-strategies Phase 1.

### 2.7 Test factories — **S**
- **Where:**
  - Three near-identical `_intent()` factories:
    `tests/trader/execution/test_gateway.py`,
    `tests/trader/ledger/test_ledger.py` and
    `tests/trader/policy/test_policy.py`.
  - Two ways to open and close an in-memory `Ledger`: the module-level
    `_OPEN_LEDGERS` list in `test_gateway.py`, and a fixture in
    `test_ledger.py`.
  - The `tests/conftest.py` `mock_jupiter_client` quote could use
    `JupiterQuoteResponse.single_route`.
- **Change:** Add `make_intent(**overrides)` and a `ledger` fixture to
  `tests/conftest.py`.
- **Done 2026-09-28** (agent-strategies Phase 0). The helpers live in
  `tests/factories.py` (`make_intent`, `open_ledger`). The conftest has an
  autouse fixture that closes them and a `ledger` fixture. The
  `mock_jupiter_client` / `single_route` point is still open.

### 2.8 Identical consecutive denials — **S**
- **Where:** `trader/execution/gateway.py` `_authorize` records every denial.
- **Context:** Since 2026-09-25 the bot pauses orders for
  `denial_cooldown = 30s` after a denial, which limits this to about 2 rows per
  minute while a limit is active. Other callers, such as future MCP agents,
  still write one row per attempt.
- **Change:** Collapse consecutive identical denials, keyed by (account, side,
  reasons), into one row with a counter. Alternatively, cache the
  `PolicyDecision` until an input to `policy_state` changes.
- **Scheduled:** agent-strategies Phase 4, where many strategy-runners share
  one gateway.

### 2.9 Ledger write batching — **S**
- **Where:** `trader/ledger/ledger.py`. Each executed trade makes three
  commits: `record_intent`, `mark_executed` and `attach_order`. `_add_event`
  re-reads the last hash for every event.
- **Change:** Commit `mark_executed` and `attach_order` in one transaction,
  and optionally move ledger calls to `asyncio.to_thread`.
- **Do not** cache the last hash in memory. The CLI (`halt`, `resume`,
  `ledger resolve`) writes to the same database while the bot runs, so a
  cached hash would break the chain.
- **Related race:** `_add_event` reads the last hash *before* the INSERT opens
  the write transaction, so two processes can fork the chain. Take the lock
  first (`BEGIN IMMEDIATE`).
- **Lock done 2026-09-28** (agent-strategies Phase 0): every write goes
  through `Ledger._write()` (`BEGIN IMMEDIATE`). The single commit is still
  scheduled for Phase 2, in `TradeGateway.record_fill`.

### 2.10 Minor
- **Unused re-exports.** `trader/paper/__init__.py` exports
  `DEFAULT_FEE_LAMPORTS` and `trader/backtest/__init__.py` exports
  `ReplayQuoteClient`. Neither is used outside its own package. Trim them, or
  keep them deliberately as public API.
- **`TickRecorder.record`** flushes on every tick. That is cheap and gives
  crash safety. Flush every N ticks or once a second if tick volume grows.
- **`load_ticks`** always sorts. Check whether the list is already sorted
  first, since the recorder writes in time order.
- **The idempotency key default** (`uuid.uuid4().hex`) is repeated in
  `TradeIntent`, `AsyncAccount._intent` and `main._execute_swap`. The
  pass-through could use `dataclasses.replace`, or a factory that omits the
  argument when it is `None`.
- **`main.py start`** is a near-duplicate of `run`. This was already flagged
  in `AGENTS.md`.
- **Scheduled:** the idempotency default and the `start` removal are in
  agent-strategies Phase 2. The other minor items are not scheduled.

### 2.11 Swap error classes live in the venue module — **S** (new 2026-09-28)
- **Where:** `SwapRejectedError` and `TransactionSubmittedError` are defined
  in `trader/providers/jupiter/async_jupiter_svc.py`. They are imported by
  `trader/execution/gateway.py` and `trader/paper/wallet.py`.
- **Change:** move them to `trader/models/errors.py` and re-export them from
  the old location.
- **Scheduled:** agent-strategies Phase 2.

### 2.12 `Interval` lives in the market client — **S** (new 2026-09-28)
- **Where:** `trader/providers/jupiter/async_jupiter_client.py`.
- **Problem:** strategy specs need the timeframe enum, but the strategy layer
  may import only core modules.
- **Change:** move it to `trader/models/public_data.py`, and have the client
  re-export it.
- **Scheduled:** agent-strategies Phase 1.

---

## Done on 2026-09-28 (for reference)

- **Ledger hash-chain race** (part of 2.9): see 2.9 above.
- **Test factories** (2.7): see 2.7 above.

- **Data paths independent of the working directory** (was 1.4). New
  `trader/paths.py`:
  - `data_dir()` reads `TRADER_DATA_DIR` and falls back to
    `<project root>/.data`.
  - `policy_file()` reads `TRADER_POLICY_FILE` and falls back to
    `<project root>/policy.toml`.
  - Relative values resolve against the project root, never the cwd.

  `ledger_path`, the `KillSwitch` default and the paper wallet derive from
  `data_dir()`, and `load_policy` uses `policy_file()`. `DATA_DIR` and
  `DEFAULT_POLICY_PATH` are gone. The test fixture now isolates state through
  the env vars. `tests/trader/test_paths.py` covers `halt` run from another
  directory reaching the bot. That test failed before the change.

## Done on 2026-09-25 (for reference)

- **Mints:**
  - `Mint` caches its pubkey and decimal scale.
  - `SolanaMints` has an O(1) symbol index, `get_pair(symbol)` and
    `symbol_of(mint)`. These replaced 4 copies of the "mint → symbol" logic and
    3 copies of the pair splitting.
- **Ledger:**
  - It is a context manager, which removed `main._open_ledger`.
  - `MOVED_FUNDS_STATUSES` is a single constant, and `_in()` builds the
    placeholders.
  - `_mark_error` is a shared helper.
  - Indexes were added on `intents(status, updated_at)`, `intents(created_at)`
    and `events(type, id)`.
  - The failure-streak count is a single `COUNT` query.
  - It uses `PRAGMA synchronous=NORMAL`.
- **Resource lifecycle:** the creator closes. `main.py` closes the ledger and
  the tick recorder; the bot closes only the provider. `TickRecorder` is a
  context manager.
- **Denials:** after a denial the bot pauses new orders for
  `denial_cooldown = 30s`, instead of writing one ledger row per tick.
- **`AsyncAccount`:**
  - `_execute_order` and `_to_order` are shared by `buy` and `sell`.
  - `_quote_is_usd` is computed once.
  - The balance cache is invalidated after each fill, which removed
    `position_last_update` and the external cache resets in the backtest and a
    test.
- **Smaller cleanups:**
  - `JupiterQuoteResponse.single_route` is the one quote factory, used by the
    replay code and the tests.
  - The `RandomStrategy` price history, which was never read, was removed.
  - The `TargetValueStrategy` history is a `deque(maxlen=...)`.
  - The unused `ReplayQuoteClient.token_mint` was removed.
  - `Tick` uses slots.
  - `resume` reuses `_build_gateway`.
  - The `paper reset` default is derived from `DEFAULT_PAPER_BALANCES`.
  - The mode check compares against `RunningMode.DRY`.
