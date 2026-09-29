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

**Overall: about 58% of the end goal** (down from 61% after the
recheck found real accounting and evaluation gaps, see stage R). The foundations (safety, ledger,
execution seam, spec format) are largely in place. What is missing is mostly
the part that makes strategies *live*: storing them, running many at once, and
approving them for real money.

| Goal | Done | Missing | Score |
|---|---|---|---|
| 1. One gateway to create strategies that run automatically | Spec format, `strategy schema/validate/backtest` (JSON CLI), `TradeService` seam, `run ... spec` | Storing specs (`submit`), a registry, runners that start stored specs, real-mode approval | **35%** |
| 2. Manual trades | `swap` runs in the `manual` bucket through the same pipeline as strategies; the fill (real amounts) and costs are recorded and show in `pnl` / `ledger list` | Allowed to spend funds that buckets hold; no SOL fee reserve | **80%** |
| 3. Structured strategy building | Spec v1: 13 condition types, required stop, warm-up, validation, backtest | Indicators and backtests are not yet faithful (R7); spec limits not enforced live (R6); one sizer, USDC/USDT inputs only, no expression language | **60%** |
| 4. One wallet, bucket per strategy | In-process buckets with a budget cap that shrinks after losses; per-bucket ledger account; buckets on one wallet can't double-spend | One strategy per process, budgets not persisted, no wallet-level allocation view or aggregate reconcile; a bucket without a budget can spend another bucket's tokens (B2) | **40%** |
| 5. Accurate trades and costs | Real mode reads real amounts and fees from the confirmed tx; rent; failed-tx fees; net PnL; hash-chained ledger; every registry pair gets USD values (Price API V3 fills what the trade can't price) | A failure after EXECUTED can lose a fill (R1); partial sells close the whole position (R5); no mark-to-market; paper/backtest fills are the quote's | **75%** |
| (Foundations: safety, tests, layering) | Policy, kill switch, breaker, idempotency, UNCONFIRMED blocking, 432 tests, enforced layering | Key still shares a process with strategies | 85% |

The overall figure is the plain average of goals 1–5.

### 2.1 What has been built (short)

- **Hardening (2026-09-23 → 27).** Fixed the sell-decimals bug, the composer
  selling ~20% of a position, failed transactions counted as confirmed, and
  re-sends after broadcast. Added websocket reconnect, loop backoff,
  dry-by-default CLI, the price-impact cap (after confirming
  `priceImpactPct` is a fraction), the slippage ceiling, the SOL fee reserve,
  graceful shutdown, secret-free logs, and the move to `api.jup.ag`.
- **Ledger, policy and gateway.** SQLite ledger per mode with a hash-chained
  event log; pure `evaluate()` over `policy.toml` (real mode off by default,
  per-trade/daily/hourly/loss limits, allow-list, breaker); `TradeGateway` as
  the only path to a swap and to the ledger; idempotency keys; UNCONFIRMED
  intents block trading until `ledger resolve`; kill switch; positions and PnL
  restored on startup.
- **Net PnL after costs.** Network fee, rent and route SOL charges recorded
  after each swap; real mode reconciles against on-chain balance deltas.
- **Paper and backtest.** `paper` mode on a simulated wallet with real
  quotes; tick recording; deterministic `Backtester` (injectable clock and
  seeded rng).
- **Strategy specs and the agent API.** `trader/indicators.py`,
  `trader/strategy_spec/` (models, predicates, `SpecStrategy`, validation),
  read-only `trader/market/`, and the JSON commands `market ...` and
  `strategy schema|validate|backtest`.
- **The seam.** `Executor` protocol (`OnChainExecutor`, `SimulatedExecutor`);
  `TradeService` with buckets, serialized orders and classified replies;
  `TradeClient` / `LocalTradeClient`; the bot loop knows no mode, key or
  ledger; `trader/wiring.py` is the only place that turns a mode into
  components.
- **Code quality.** Data paths independent of the cwd (`trader/paths.py`),
  enforced layering (`tests/test_architecture.py`), empty package init,
  shared test factories, ledger writes under `BEGIN IMMEDIATE`, swap errors
  and `Interval` moved to core.

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
| Interface | **CLI with JSON output** over `trader/agent_api/`. MCP can wrap it later. |
| Mints | **Owner registry only** (`SOLANA_MINTS` ∩ `allowed_symbols`). |
| Activation | **Automatic in paper; the owner approves real.** |
| Processes | One strategy-runner per spec; one trade-runner per mode holding the gateway, key and ledger. Strategies are mode-agnostic. |
| Budgets | Per-strategy budget in the bucket; `policy.evaluate()` stays wallet-wide. |
| Spec storage | In the ledger (`strategies` table), so nothing an agent runs writes to the real ledger. |
| Wallet | One wallet for all strategies for now. Real mode uses a dedicated low-balance hot wallet. |

## 5. Roadmap

Refactorings that make the features cheaper come first (stage A), then the
features (stage B). Each item ships on its own with the suite green (`ruff
check`, `ruff format --check`, `pyright`, `pytest`). Size: **S** under an hour,
**M** several modules, **L** a design change.

**Why this order.** A1–A3 leave exactly one path by which money moves and gets
recorded, so the manual bucket, the trade-runner and the reports plug in once.
A4 gives every trade a USD value, which allocations, budgets and cost
accounting all need. A5–A6 split the two files that stage B will grow most
(`ledger.py` gets a `strategies` table, `main.py` gets `serve`/`approve`).
A7–A9 are cheap and remove known rough edges before more code builds on them.

### Stage A — Foundations (refactor first)

#### A0. Commit the current work — S
Phases 0–2 of the old roadmap (39 files) were uncommitted. Commit them before
anything else so each later step has a clean diff.

#### A1. One trade pipeline — M
- **Problem:** the sequence "intent → `gateway.submit` → mark executed →
  `fetch_swap_costs` → `record_fill`" lives inside `AsyncAccount`
  (`_execute`, `_execute_order`, `_fetch_costs`, `_record_order`). The manual
  `swap` (`main._execute_swap`) builds its own intent and skips the cost and
  fill steps. `AsyncAccount` also has a second path with `gateway=None`
  (backtests and tests) that executes straight on the provider.
- **Change:**
  - Extract the pipeline into one execution-layer function or class (e.g.
    `trader/execution/fills.py`), which keeps the rule that costs are fetched
    only after `EXECUTED` and never raise.
  - Give backtests a real gateway over an in-memory ledger with a permissive
    policy (`TradeGateway.in_memory()`), so `gateway=None` disappears and
    backtests exercise the same path as live.
- **Exit:** there is a single call site of `fetch_swap_costs`, and
  `AsyncAccount` has no `gateway is None` branch.
- **Design (2026-09-29):**
  - `trader/execution/fills.py` (execution layer):
    - `Fill(result, costs)`, with `amounts()` returning the raw (in, out)
      amounts: the on-chain ones when known, otherwise the quote's. This
      replaces `async_account._actual_amounts`.
    - `execute_trade(gateway, provider, intent, call) -> Fill`: runs
      `gateway.submit(intent, call)` and only then
      `provider.fetch_swap_costs(result)`. It is the only caller of
      `fetch_swap_costs`. A non-`TradeCosts` value (a mock) counts as unknown.
  - Recording stays `gateway.record_fill(...)`, because only the caller
    knows how to turn a fill into an `Order` (a bucket knows its pair and
    position; A2's manual swap knows its own).
  - `TradeGateway.in_memory(policy=Policy.unlimited())`: an in-memory
    `Ledger`, a kill switch that is never active (a backtest must not read
    the live `HALT`), and `real_mode=False`. `Policy.unlimited()` has no USD,
    rate, loss or breaker limits and allows unknown notional. The replay
    cannot use the real limits: ledger timestamps are wall-clock, so a
    1000-candle replay would trip the hourly limit in seconds. Idempotency,
    the intent lifecycle and the event chain still run.
  - `AsyncAccount(provider, input_mint, output_mint, gateway, ...)` and
    `TradeService(provider, gateway, ...)` require the gateway. The
    `gateway is None` branches in `restore_from_ledger`,
    `reconcile_position`, `_execute` and `_record_order` go away.
  - `Backtester` opens `with TradeGateway.in_memory() as gateway:` for each
    replay. Tests use `factories.memory_gateway()`, which closes itself at
    the end of the test like `open_ledger()`.
  - The manual `swap` keeps calling `gateway.submit` until A2 moves it onto
    `execute_trade`.

#### A2. Manual swaps as a bucket — S (after A1)
- `swap` goes through `TradeService` into a `manual` bucket
  (`"<mode>:manual"`) using the A1 pipeline. It records the order and costs,
  so manual trades show up in `pnl` and `ledger list`.
- A manual swap is any pair and has no position; its bucket tracks spend and
  costs only.
- `main._execute_swap` is removed. The `swap` output reports the fill (amounts,
  costs), not just the signature.
- Spending only unallocated funds is enforced later, in B2.
- **Design (2026-09-29):**
  - `protocol.py` gets `SwapRequest(spend_mint, receive_mint, amount,
    slippage_bps=50, rationale=None, idempotency_key=None)`; `amount` is in
    UI units of the token spent. The reply is the usual `OrderReply`.
  - `TradeService.swap(request, source="cli") -> OrderReply` runs under the
    same submit lock as the buckets and sorts outcomes into replies the same
    way (`_place` becomes a generic `_reply`). The account is
    `"<mode>:manual"`; it needs no `open_bucket`. Old manual swaps stay under
    `"<mode>:swap"` in existing ledgers (they had no order or costs).
  - `trader/trading_service/manual.py` builds the intent (side `SWAP`,
    notional known only when the token spent is a USD stable, as today),
    runs it through `execute_trade`, turns the `Fill` into an `Order` and
    calls `gateway.record_fill`.
  - The `Order` of a manual swap: `input_mint` is the token spent,
    `output_mint` the token received, `side=BUY` ("spend the input"),
    `quantity` the amount received and `quote_amount` the amount spent (real
    amounts when known), `fill_price = spent / received`. Its USD rates come
    from the trade when one side is a USD stable (`quote_usd`, and `sol_usd`
    when the other side is SOL), so its costs get a USD value. When neither
    side is a stable, `price` falls back to `fill_price` and the rates are
    unknown until A4.
  - `main.py swap` uses `build_trade_service(mode, ...)` and prints the fill
    (signature, amounts, costs). A denied, rejected or failed swap prints
    the reasons to stderr and exits with code 1. `main._execute_swap` is
    removed.
  - Not in A2: the SOL fee reserve for manual swaps that spend SOL (B2, with
    the allocation checks, since it needs a balance read).

#### A3. Split `AsyncAccount` — M
- **Problem:** `trader/async_account.py` (520 lines) mixes the balance cache,
  sizing, intent building, the position/PnL book (gross/net/costs totals,
  incomplete count, `pnl_summary`) and restore.
- **Change:**
  - Extract a `PositionBook` (plain data plus PnL math) that restores from
    `AccountState` and is shared by strategy and manual buckets.
  - `AsyncAccount` keeps balances, sizing and order placement.
  - Remove the dead market-data passthroughs (`AsyncAccount.get_price` /
    `get_candles`, and the provider's market-data methods kept "for
    compatibility") once nothing calls them; `trader/market/` is the only
    data path.
  - Replace `assert position` with an explicit check.
- **Design (2026-09-29):**
  - `trader/models/book.py` (core, plain data): `PositionBook(quote_symbol)`
    holds `position`, `realized_usd` and the native totals (`gross_quote`,
    `net_quote`, `costs_sol`, `incomplete`).
    - `open(entry) -> Position`;
    - `close(exit) -> ClosedPosition(position, pnl, realized_usd)`, which
      books the PnL and clears the position;
    - `restored(quote_symbol, realized_usd, gross_quote, net_quote,
      costs_sol, incomplete, entry)`, a classmethod that takes plain values,
      so the core layer never imports the ledger's `AccountState`;
    - `summary()`, the old `pnl_summary()` text.
  - `AsyncAccount` gets a `book` attribute and keeps balances, sizing,
    intents and `Fill → Order` conversion. Its PnL fields (`total_pnl`,
    `total_gross_quote`, ..., `current_position`) and the accessors
    `get_position`, `get_total_realized_pnl`, `get_unrealized_pnl` (unused)
    and `pnl_summary` go away; callers read `account.book`.
  - `can_sell()` returns the open position, so `sell` has no `assert`.
  - Dead code removed: `AsyncAccount.get_price` / `get_candles`, the
    provider's `get_price_ticker_data` / `get_candles` (market data is only
    `trader/market/`), and `ReplayQuoteClient.get_price` / `get_candles`.
  - The manual bucket keeps no book yet: a manual swap opens no position.
    Reporting manual swaps on their own is B5.

#### A4. USD value for every trade — M
- **Problem:** the price feed is USD, balances and fills are in the input
  token. Four callers work around it (`_notional_usd`, `_quote_is_usd`,
  `_execute_swap`, the backtester's stable-only check). Non-stable inputs get
  `notional_usd = None` and are denied (or escape every USD budget); costs of
  pairs without SOL have no USD value and are flagged `[!] incompleto`.
- **Change:**
  - Add a `PriceOracle` in the market layer: `usd_price(mint)` and
    `usd_value(mint, amount)`, backed by the documented Jupiter **Price API
    V3** (multi-mint). It is also the fallback when the undocumented websocket
    (`trench-stream.jup.ag`) is down (§7.2).
  - Use it for every intent's notional (bucket and manual), for converting SOL
    costs to USD on every pair, and for pricing the output in input-token
    units (`price(out) / price(in)`).
  - The trade-derived rates (`trade_rates`) stay the first choice for a
    trade's own costs; the oracle fills the gaps.
- **Exit:** no `[!] incompleto` on pairs in the registry; a `JUP-USDC` swap
  paid in SOL has a USD notional.
- Lifting the USDC/USDT-only rule for specs is a feature (B6).
- **API check (2026-09-29, live):** `GET {JUPITER_API_URL}/price/v3?ids=a,b`
  works unauthenticated on `api.jup.ag`. It returns
  `{mint: {usdPrice, blockId, decimals, liquidity, priceChange24h, ...}}`
  for all eight registry mints, and silently omits unknown ids. Stables
  come back as about 0.9999, not 1.
- **Design (2026-09-29):**
  - `AsyncJupiterClient.get_usd_prices(mints) -> dict[str, Decimal]`,
    parsed with `parse_float=Decimal`; missing mints are absent.
  - `trader/market/prices.py` (market layer):
    - a `PriceOracle` protocol with `usd_prices(mints)`;
    - `JupiterPriceOracle(client, ttl=10s)`, which caches per mint and
      treats USDC/USDT as exactly 1 USD, as the rest of the code already
      does (`quote_usd = 1`, notional = amount spent);
    - `usd_snapshot(oracle, mints)`, which **never raises**: it uses a 5s
      timeout, logs a warning and returns `{}` on failure, so a price
      problem can only make values unknown, never block or fail a trade.
  - **The prices are taken before the trade, never after it.** Anything
    after `EXECUTED` must not fail (the costs rule), so the account and the
    manual swap take one snapshot before building the intent and reuse it.
  - **Notional (policy USD value):**
    - a buy is the amount spent × `usd(input)`, which gives the same number
      as today for stable inputs;
    - a sell is the quantity × the signal's USD price, which is known on
      every pair because the feed is USD;
    - a manual swap is the amount × `usd(spend)`.
  - **Rates:** `trade_rates` stays the first source. A new
    `with_sol_usd(rates, sol_usd)` (in `models/costs.py`) fills `sol_usd`
    and `sol_in_quote` from the snapshot when the pair has no SOL, so costs
    get a USD and quote value and PnL is complete. For manual swaps with no
    stable side, the USD price of the token received comes from
    `usd(spend) × fill_price`, falling back to `usd(receive)`.
  - `TradeService(..., prices=None)` passes the oracle to its accounts and
    to `swap`; `None` keeps today's behavior. That is what backtests (no
    network) and unit tests use. `wiring.build_trade_service` passes a
    `JupiterPriceOracle` over the provider's Jupiter client.
  - **Websocket fallback:** `JupiterMarketData.get_price` waits at most
    `price_timeout` (30s) for the websocket. On a timeout or a websocket
    failure it logs a warning and returns the Price API price, so the bot
    keeps ticking (and its stops keep working) while the feed is quiet or
    down. The next call tries the websocket again.
  - **Moved to B6:** pricing the strategy's feed in input-token units
    (`price(out) / price(in)`). That changes what strategies see, and it
    only matters once non-stable inputs are allowed for strategies.

#### A5. Split the ledger module and move the order codec — M
- **Problem:** `trader/ledger/ledger.py` (663 lines) holds the connection and
  schema, the event chain, intents, PnL reports (`AccountPnL`) and
  `policy_state`. Stage B adds a `strategies` table and allocation queries.
- **Change:**
  - Split into `store.py` (connection, `_write`, `_SCHEMA`, `_migrate`, event
    chain), `intents.py`, `reports.py` and `policy_state.py`, keeping `Ledger`
    as the facade.
  - Move `order_to_json` / `order_from_json` to `trader/models/order.py`, so
    the wire codec (core) can use them.
  - **Collapse identical consecutive denials** into one row with a counter,
    keyed by (account, side, reasons). Many runners share one gateway in B3,
    and each would otherwise write a row per attempt.
- Keep: no in-memory cache of the last event hash (the CLI writes while the
  bot runs), and `mark_executed` / `record_fill` stay two commits.
- **Design (2026-09-29):**
  - `trader/ledger/` becomes one module per concern. They share one
    connection through a small base class, and `Ledger` is the facade that
    combines them, so no caller changes:
    - `store.py`: `LedgerStore` (connection, pragmas, `_SCHEMA`,
      `_ADDED_COLUMNS`, `_migrate`, `_write`), the event chain
      (`_add_event`, `add_event`, `verify_chain`, `last_event_time`,
      `_event_hash`) and the SQL helpers;
    - `intents.py`: `IntentStore`, which records intents, marks their
      status, attaches orders, resolves, and runs the queries (`get`,
      `find_by_idempotency_key`, `list_intents`, `last_executed_trade`);
    - `reports.py`: `AccountPnL`, plus `pnl_report`, `pnl_totals` and
      `total_realized_pnl`;
    - `policy_state.py`: `policy_state()` and its aggregates;
    - `ledger.py`: `class Ledger(IntentStore, Reports, PolicyStateQueries)`
      and `ledger_path`.
  - `order_to_json` / `order_from_json` move to `trader/models/order.py`,
    and the gateway, tests and CLI import them from there. The ledger keeps
    importing them, since risk may import core.
  - **Collapsing identical denials:**
    - A denial is a repeat when the account's latest intent row, in
      insertion order, is a `denied` row with the same side, mints and
      reasons.
    - A repeat increments that row's `repeat_count` (a new column added via
      `_migrate`) and its `updated_at`, and inserts nothing. The read and
      the write happen in one `BEGIN IMMEDIATE` transaction.
    - A repeat writes **no event**. The first denial is in the hash chain,
      and repeats moved no funds; `repeat_count` is informational and not
      tamper-evident.
    - Any other outcome on the account (an executed, failed or different
      denied intent) ends the streak.
    - `IntentRecord.repeat_count` is exposed, and `ledger list` shows
      `(xN)`.
    - The caller still gets its `PolicyDeniedError`. The collapsed intent's
      id is not stored, which is harmless: denied intents never block a
      retry.

#### A6. Split `main.py` into `trader/cli/` — M
- **Problem:** `main.py` is 535 lines of owner commands, and stage B adds
  `trader serve`, `strategy approve`, `wallet show` and `strategy run`.
- **Change:** move the command groups (`run`, `swap`, `backtest`, `pnl`,
  `ledger`, `paper`, `halt`/`resume`) into `trader/cli/` modules (app layer),
  as `trader/agent_api/cli.py` already does; `main.py` only mounts them. Owner
  output formatting (`_format_account_pnl`, `_record_costs`) goes with them.
- **Design (2026-09-29):** `trader/cli/` (app layer, mapped in
  `tests/test_architecture.py`):
  - `common.py`: `warn`, `parse_decimal`, `parse_kwargs` (was the
    name-mangled `__parse_kwargs`), `get_strategy_obj`,
    `get_notification_svc` and `check_symbol`;
  - `bot.py`: `run` and `backtest`;
  - `swap.py`: `swap` and its output;
  - `safety.py`: `halt` and `resume`;
  - `ledger.py`: the `ledger list / verify / resolve` group;
  - `pnl.py`: `pnl` and its formatting;
  - `paper.py`: the `paper balance / reset` group;
  - `__init__.py`: builds `app`, registers the commands, and mounts the
    groups plus the agent `market` / `strategy` apps.

  `main.py` keeps only `from trader.cli import app` and the
  `setup_logging()` entrypoint, so `uv run main.py ...` and
  `main_module.app` are unchanged. The command names, arguments and output
  stay the same; tests patch the new module paths.

#### A7. Legacy strategies clean-up — S
- Replace the undocumented hard-coded `Decimal("5")` order cap in three
  strategies (in input-token units) with an explicit `order_usd` argument.
- Log at DEBUG with lazy arguments, and only on state changes; remove the
  INFO-per-tick lines of `TargetValueStrategy` and the composer.
- Freeze them: no new features. Their future (port to specs or delete) is B6.
- **Design (2026-09-29):**
  - The cap becomes `order_usd` (default `"5"`, so behavior is unchanged)
    on `TargetValueStrategy`, `TrailingStopLossStrategy` and
    `TargetPercentStrategy`. It is shared by one base helper,
    `_capped_quantity(balance, price)`: spend `order_usd` when the balance
    covers it, otherwise `balance_percent` of the balance. It is in input
    units, which equal USD for the stable inputs these strategies need.
  - `TradingStrategy._log_on_change(key, state, msg, *args)` logs at DEBUG,
    lazily, only when `state` changes for `key`. The console shows
    `trader.trading_strategy` at DEBUG, so moving lines to DEBUG alone would
    not reduce them.
    - `TargetValueStrategy`: the per-tick "Current price" line goes away,
      and the "skip buying" reasons log once per change.
    - WMA: logs its OK/NOK once per change.
    - Trailing stop and target percent: log when they fire, not on every
      tick.
    - Composer: logs `[BUY]`, `[SELL]` or `[HOLD]` once per change.
  - Dead code removed: `TargetValueStrategy._recalculate_target_buy_price`
    (its only call was commented out) and its unused state
    (`max_position_periods`, `same_target_count`, `report_interval`,
    `max_history_size`, `price_history`, `position_periods`). With it goes
    the `pandas` dependency, which nothing else uses.

#### A8. Notifications off the event loop — S
- `TelegramNotificationService` uses sync `requests.post` (10s timeout) inside
  the loop. Make `Notifier.send_message` async over `httpx.AsyncClient` and
  schedule it as a task, so it can never stall the trade-runner's submit lock.
- **Design (2026-09-29):**
  - `Notifier` (`trader/bot/config.py`) is `send_message(message) -> None`,
    which never blocks and never raises, plus `async aclose()`.
  - `TelegramNotificationService.send_message` inside a running loop
    schedules `_send` as a task. The service keeps a reference to it so it
    isn't garbage-collected. Outside a loop (no event loop to stall), it
    runs `_send` directly.
  - `_send` posts with a short-lived `httpx.AsyncClient(timeout=10)`: there
    are only a few messages per session, and there is no client tied to one
    loop. Errors are logged with the token redacted.
  - `aclose()` waits up to 5s for pending sends. The bot calls it in
    `_shutdown`, so "Bot interrompido" still goes out on Ctrl+C.
  - `requests` is no longer used and leaves the dependencies.

#### A9. Small clean-ups — S
- `AsyncRPCClient` validates `rpc_url` with `assert`; raise `ValueError`.
- `tests/conftest.py` `mock_jupiter_client` should use
  `JupiterQuoteResponse.single_route`.
- Unused re-exports (`DEFAULT_FEE_LAMPORTS`, `ReplayQuoteClient`): trim or
  keep deliberately.
- `logging_config`: simplify `BotLoggerFileHandler` / `DictConfigurator`.
- Low value, do only if touched: `TickRecorder` flushes every tick;
  `load_ticks` always sorts.
- Delete `botconfigs.example.yaml` (never wired; specs replace it) — pending
  owner answer (§9).
- **Done (2026-09-29):**
  - `AsyncRPCClient` raises `ValueError("HELIUS_RPC_URL não definida")`.
  - The Jupiter mocks share one `factories.bonk_quote()` (built with
    `single_route`). It replaces two hand-written copies, in the conftest
    and the bot test.
  - The re-exports are trimmed from `trader.paper` and `trader.backtest`.
  - `logging_config`: the file handler that renamed its open file once the
    bot name appeared is replaced by a plain `FileHandler`
    (`.logs/trader-<ts>.log`, one per process, opened lazily) plus a
    `BotNameFilter` that tags every line with `[<bot name>]`. It uses
    `logging.config.dictConfig`. Log files are no longer named after the
    bot; the name is on every line instead, which also works with several
    bots per process.
  - **Won't do:** `load_ticks` sorting (Timsort is linear on input that is
    already sorted) and the per-tick `TickRecorder` flush (it is cheap, and
    it survives a kill).
  - **Still open:** `botconfigs.example.yaml` (§9).

#### A10. Automated live checks — S (added 2026-09-29)
- **Problem:** each step so far was checked by hand against the real Jupiter
  endpoints (quotes, candles, the price websocket). Those checks catch what
  mocks can't (API drift, units, the paper path end to end), but they were
  lost after each session.
- **Change:** an opt-in suite in `tests/live/`, marked `live` by its conftest
  and excluded by default (`addopts = -m "not live"`), so `pytest .` and CI
  stay offline. Run it with `uv run pytest -m live`.
- **Safety:** the live conftest removes `SOLANA_PRIVATE_KEY`,
  `SOLANA_PUBLIC_KEY`, `HELIUS_RPC_URL` and the Telegram variables, and the
  usual `isolated_workdir` fixture keeps the ledger, `HALT`, paper wallet and
  policy in `tmp_path`. Only paper mode and read-only commands run; nothing
  can sign.
- **Checks:**
  - `market price / candles / summary` return sane JSON;
  - `strategy validate` and `strategy backtest` on the example spec, and a
    random-strategy `backtest` on real candles makes trades;
  - paper swaps on real quotes: USDC→SOL records the fee with a USD value,
    SOL→JUP records rent, an over-limit swap is denied with exit code 1, and
    both show in `pnl` under `paper:manual`;
  - a paper bot (`random`) runs on the real websocket for ~30s, trades,
    keeps the event chain intact and records ticks; replaying those ticks
    twice gives identical results.
- **Rule from now on:** a live check done by hand is added to this suite.

### Stage R — Recheck fixes (added 2026-09-29)

A full review after stage A covered the money path, the strategy side, and
operations and tests. Every finding below was traced in the code. The two
worst were fixed right away (R0). The rest come **before stage B**, because
B1 (the backtest gate), B2 (allocation) and B3 (many processes on one
ledger) build directly on them.

#### R0. Fixed during the recheck — done
- **The Helius API key leaked into `.logs/`.** solana-py uses the `httpx2`
  fork, which logs every request URL at INFO, and only `httpx` was
  silenced. 81 lines in 6 files, from 2026-09-24/25, contain `?api-key=`.
  - Fix: `httpx2`/`httpcore2` are set to WARNING/ERROR, and a
    `RedactingFormatter` on both handlers masks `api-key=` and Telegram
    `bot<token>` in every line and traceback. A test covers both.
  - **Owner:** delete those log files and rotate the Helius key (§9).
- **A send could be retried after it went out.** `simulate` and
  `send_raw_transaction` ran outside the `try` that raises
  `TransactionSubmittedError`. A send that timed out after the node had
  accepted it was re-quoted and **sent again**.
  - Fix: simulation stays retryable; any error from the send onward is
    `TransactionSubmittedError` with the signature, so the intent becomes
    UNCONFIRMED and fails closed.
  - A regression test fails on the old code: it sent the swap more than
    once.
- Stale `.claude/commands/phase.md` and `sync-docs.md` (they pointed at the
  deleted docs) and README (`start`, the registry location) were fixed.

#### R1. Never lose a fill after EXECUTED — M
- **Problem:** after `mark_executed`, several steps can still raise, and
  the fill is then lost or misreported:
  - `_to_order` raises when the on-chain delta is 0;
  - `record_fill` can hit `database is locked`;
  - `mark_executed` itself can fail, with the signature only in memory.

  A lost buy leaves tokens orphaned, and the strategy buys again. A lost
  sell leaves the book closed in memory but open in the ledger; after a
  restart the fixed sell key makes every later sell a duplicate, so the
  bucket is stuck. `ledger resolve ... executed` writes no order, which has
  the same effect.
- **Change:**
  - one never-raise `order_from_fill(...)` that falls back to the quote's
    values, shared by `AsyncAccount` and the manual swap (the two
    Fill→Order copies and `_priced_mints` merge);
  - one ledger transaction that writes EXECUTED, the order, costs and PnL.
    Costs are still fetched after the swap: the intent is marked EXECUTED
    first and is completed, never re-executed. The signature is logged at
    ERROR if even that write fails;
  - `ledger resolve ... executed` rebuilds the order from `getTransaction`
    (moved here from B2);
  - one `SOL` constant (it is defined in four modules).

#### R2. Atomic authorization — S
- **Problem:** `_authorize` reads `find_by_idempotency_key` and
  `policy_state()` outside the write transaction, and there is no unique
  index on the key. Two processes (bot and CLI, or two runners) can both
  pass the daily limit, or both use one idempotency key.
- **Change:** check and insert in one `BEGIN IMMEDIATE`, plus a partial
  unique index on the key for moved-funds statuses. This is a prerequisite
  for B3.

#### R3. Balance reads fail loudly — S
- `AsyncRPCClient.get_account_balance` swallows `SolanaRpcException` and
  returns partial or empty balances, which the account caches for 3 minutes
  as zeros. That gives false reconcile mismatches and sells sized to zero.
  Change: raise, never cache a partial read, and sum several token accounts
  of one mint.
- The `get_lamports` retry waits on `httpx.ReadTimeout`, but solana-py
  wraps everything in `SolanaRpcException`, so it never fires.

#### R4. Rate limits and startup resilience — S
- One tenacity policy for the Jupiter HTTP calls: exponential backoff on
  429, 5xx and timeouts, honoring `Retry-After`. It applies before the
  broadcast only. Today only `get_quote` sleeps, once, for 1.5s.
- `_startup` (candles, reconcile) runs inside the loop's backoff; today one
  429 at startup exits the bot.
- A minimum tick interval, plus skipping the websocket for N seconds after
  it fails, so a dead feed does not hammer the Price API fallback.

#### R5. Sells stay within the position — S
- A sell is capped at the position's quantity, not at the wallet's balance.
  The request's quantity comes from the client, and sells skip every budget
  rule.
- A partial sell (the fee reserve, dust) keeps the remainder open instead
  of `book.close` closing the whole position and dropping its cost from
  PnL.

#### R6. A spec run enforces its own limits — S
- `run ... spec` never calls `validate()`, gives the bucket no
  `budget_usd`, and ignores `max_loss_usd` (nothing reads it). The backtest
  applies the budget; the live run does not.
- **Change:** validate against the mode's policy when loading, pass
  `spec.budget_usd` to the bucket, and stop the bucket and close its
  position once realized PnL reaches `-max_loss_usd`. The bucket is named
  `strategy:<spec_id>`, not the pair, so a spec never adopts a position it
  did not open.

#### R7. Faithful indicators and backtests — M
The B1 gate trusts backtests, so they must not be optimistic.
- **Indicators:** they are computed over a short sliding window, seeded
  with only `lookback` bars. EMA equals the SMA at start, and RSI14 read 78
  live against 67 in `market summary` on the same data. Change: incremental
  Wilder/EMA state updated on bar close, seeded with 3–5× the period, and
  shared with `market summary`.
- **Fills:** a backtest fills at the same close that triggered the signal,
  checks stops on closes only, and has no slippage. Change: fill at the
  next bar's open plus slippage, and check stops against the bar's low and
  high.
- **Timestamps:** candle timestamps are probably the bar's open. Check this
  live and stamp ticks at the close.
- **Warnings:** a backtest with fewer bars than the lookback, or zero
  evaluated bars, reports `ok: true` silently. It must error or warn.
- **Entries:** entry conditions are states, so a strategy re-buys right
  after `take_profit`. Add `crosses_above`/`crosses_below` conditions (or
  re-arming), and default the cooldown to one bar.

#### R8. Agent-facing contract — S
- `json_errors` catches only `ValueError`/`OSError`: `httpx` errors,
  `KeyError`, `LookupError` and Typer's parameter errors print a traceback
  instead of `{"ok": false}`. Add a catch-all, and keep `spec_id` in failed
  `validate` output.
- Schema: add descriptions and units per field, bounds on the
  Decimal-as-string branch, and a relative `ttl_days` expiry. The example
  spec can then validate.
- `spec_id` hashes `name`/`rationale`/`agent_id`; hash only the behavioral
  fields, so `supersedes` survives a reworded rationale.

#### R9. Operations — S
- Logs: use `RotatingFileHandler`, put the directory under the project root
  (the `paths.py` rule), and lower the per-tick DEBUG volume. `.logs/` is
  155 MB today.
- Remove `traceback.print_exc()` from the bot's `_on_error`: it bypasses the
  console filter and the redaction.
- CI:
  - add a `windows-latest` job (the target platform);
  - bring `main.py` and `tests/` into the pyright scope;
  - add a manual or scheduled `pytest -m live` workflow.
- Paper wallet: save before changing the in-memory balances (a failed save
  is retried and applies the swap twice), and reload before each swap (the
  CLI and the bot overwrite each other's file).
- One default mode across the CLI (`pnl` uses paper, the others use dry).
- `ledger resolve real ... failed` needs `HELIUS_RPC_URL` for the fee
  backfill, but it only checks after writing. Check first.

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
  status change is also a hash-chained event (`strategy_submitted`,
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
- **Tests:** idempotent resubmit, every gate threshold, the caps,
  `verify_chain()` clean, policy parsing with mode overrides and unknown keys.

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
- Port the useful legacy strategies to specs, then remove
  `TargetValueStrategy` and the composer family (`RandomStrategy` stays for
  testing). Adding a condition type stays a three-place change (model, union,
  `PREDICATES`), checked by a test.

#### B7. Later
- An MCP server over `trader/agent_api`.
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
| Hardening, ledger/policy/gateway, paper/backtest, specs, `TradeService` | **done** (2026-09-23 → 29) | 432 tests; ruff, format and pyright clean |
| A0 Commit | **done** (2026-09-29) | `f434d2e` |
| A1 One trade pipeline | **done** (2026-09-29) | 443 tests (11 new). `execute_trade` is the only cost fetch; backtests use `TradeGateway.in_memory()`. Checked live: a 300-candle random backtest made 161 legs through it |
| A2 Manual swaps as a bucket | **done** (2026-09-29) | 451 tests (8 new). `TradeService.swap` + `trading_service/manual.py`; `main._execute_swap` removed. Checked live in paper (isolated data dir): USDC->SOL and SOL->JUP recorded with costs, an over-limit swap denied |
| A3 Split `AsyncAccount` | **done** (2026-09-29) | 454 tests (4 new); live suite green. `PositionBook` in `trader/models/book.py`; `AsyncAccount` 520 -> 427 lines; dead market-data passthroughs removed from the account, the provider and `ReplayQuoteClient` |
| A4 USD value for every trade | **done** (2026-09-29) | 472 tests (18 new); live suite 6/6 (new: Price API vs websocket, fallback). `trader/market/prices.py` (`JupiterPriceOracle`, `usd_snapshot`), `with_sol_usd`, notional for every pair, websocket fallback. Strategy-side price units moved to B6 |
| A5 Split the ledger | **done** (2026-09-29) | 476 tests (4 new); live suite 6/6. `trader/ledger/{store,intents,reports,policy_state}.py` behind the `Ledger` facade (the old 663-line module is now 34 lines); order codec in `models/order.py`; identical consecutive denials collapse (`repeat_count`, `ledger list` shows `(xN)`) |
| A6 Split `main.py` | **done** (2026-09-29) | 476 tests; live suite 6/6. `trader/cli/` (7 modules, app layer); `main.py` is 10 lines; same commands, arguments and output |
| A7 Legacy strategies clean-up | **done** (2026-09-29) | `order_usd` replaces the hard-coded cap (default 5, unchanged); `_log_on_change` logs state changes only; dead `_recalculate_target_buy_price` and the `pandas` dependency removed |
| A8 Async notifications | **done** (2026-09-29) | Telegram over `httpx` as a task, `Notifier.aclose()` drains at shutdown (5s cap); `requests` dependency removed |
| A9 Small clean-ups | **done** (2026-09-29) | 486 tests. RPC `ValueError`, shared `bonk_quote()`, trimmed re-exports, simpler logging (`trader-<ts>.log` + `[bot]` per line). `botconfigs.example.yaml` waits for the owner |
| A10 Automated live checks | **done** (2026-09-29) | `tests/live/` (5 tests, ~45s, `uv run pytest -m live`): market data, spec and random backtests on real candles, paper swaps, a 30s paper bot on the websocket plus a deterministic replay of its ticks. First run caught that the example spec can never pass `validate` (fixed 2099 expiry); the test uses a fresh copy |
| R0 Recheck: key leak, double send | **done** (2026-09-29) | 489 tests. `httpx2` silenced + `RedactingFormatter`; send errors are `TransactionSubmittedError` (regression test fails on the old code). Owner: purge the 6 leaked log files, rotate the Helius key |
| R1 Never lose a fill | not started | |
| R2 Atomic authorization | not started | |
| R3 Balance reads fail loudly | not started | |
| R4 Rate limits + startup | not started | |
| R5 Sells within the position | not started | |
| R6 Spec runs enforce limits | not started | |
| R7 Faithful indicators/backtests | not started | |
| R8 Agent contract | not started | |
| R9 Operations | not started | |
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
    amounts from the token-balance deltas. Dry mode asks Solana
    (`getFeeForMessage`). Paper simulates the base fee and first-time rent.
    Dry and paper use the quote's amounts; that is inherent to those modes.
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
  - See it with `main.py pnl <mode>` and `ledger list`.
- **Prices are USD, balances are in the input token.** `Order.price` is the
  real fill only for USDC/USDT inputs; `Order.fill_price` keeps the raw
  input-per-token price. Strategy sizing is only meaningful for stable
  inputs, which is why specs and backtests require them (B6 lifts this).
- **Manual swaps may spend any wallet funds**, including what a bucket
  holds, and keep no SOL fee reserve (B2). Their `pnl` block shows the
  costs paid; its gross/net lines are always zero, since a manual swap
  opens no position (B5 can report manual swaps on their own).
- **Dry mode is not paper trading.** It reads the real wallet and never
  changes balances; use `paper` to test strategies end to end.
- **Paper fills use the quoted `outAmount`**, with no slippage model; replays
  ignore the network fee (B5).
- **`TargetValueStrategy`'s trailing stop only works inside a narrow band**,
  with no stop-loss below entry (A7/B6).
- **A bot trade resolved by hand as `executed` has no order JSON**, so its
  position is not restored (B2).
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
- CLI `--notification-args` still accepts the Telegram token; prefer the
  `TELEGRAM_*` env vars.

## 8. Top risks

1. **A runaway or prompt-injected agent.** Mitigated: agents only author
   specs; policy, bucket budgets and the kill switch bound every trade; real
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

- **Urgent:** delete the six `.logs/` files from 2026-09-24/25 that contain
  the Helius `api-key`, and rotate the key (R0). Nothing new leaks now.
- `policy.toml` (loosened paper limits) is tracked in git next to
  `policy.example.toml`. Should it be gitignored?
- Which pairs, and what maximum notional per trade and per day, are you
  comfortable with in real mode?
- May a manual swap spend funds allocated to a bucket, or only unallocated
  funds (the B2 default)?
- Legacy strategies: port `TargetValueStrategy` / the composer to specs, or
  delete them after B6?
- Delete `botconfigs.example.yaml`?
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
