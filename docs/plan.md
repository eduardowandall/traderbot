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

**Overall: about 61% of the end goal.** The foundations (safety, ledger,
execution seam, spec format) are largely in place. What is missing is mostly
the part that makes strategies *live*: storing them, running many at once, and
approving them for real money.

| Goal | Done | Missing | Score |
|---|---|---|---|
| 1. One gateway to create strategies that run automatically | Spec format, `strategy schema/validate/backtest` (JSON CLI), `TradeService` seam, `run ... spec` | Storing specs (`submit`), a registry, runners that start stored specs, real-mode approval | **35%** |
| 2. Manual trades | `swap` runs in the `manual` bucket through the same pipeline as strategies; the fill (real amounts) and costs are recorded and show in `pnl` / `ledger list` | Allowed to spend funds that buckets hold; no SOL fee reserve | **80%** |
| 3. Structured strategy building | Spec v1: 13 condition types, required stop, warm-up, validation, backtest | One sizer only, USDC/USDT inputs only, no expression language; the legacy strategies live outside the spec | **65%** |
| 4. One wallet, bucket per strategy | In-process buckets with a budget cap that shrinks after losses; per-bucket ledger account; buckets on one wallet can't double-spend | One strategy per process, budgets not persisted, no wallet-level allocation view or aggregate reconcile | **45%** |
| 5. Accurate trades and costs | Real mode reads real amounts and fees from the confirmed tx; rent; failed-tx fees; net PnL; hash-chained ledger; every registry pair gets USD values (Price API V3 fills what the trade can't price) | No mark-to-market; paper/backtest fills are the quote's; USD values depend on the Price API being up | **80%** |
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

#### A6. Split `main.py` into `trader/cli/` — M
- **Problem:** `main.py` is 535 lines of owner commands, and stage B adds
  `trader serve`, `strategy approve`, `wallet show` and `strategy run`.
- **Change:** move the command groups (`run`, `swap`, `backtest`, `pnl`,
  `ledger`, `paper`, `halt`/`resume`) into `trader/cli/` modules (app layer),
  as `trader/agent_api/cli.py` already does; `main.py` only mounts them. Owner
  output formatting (`_format_account_pnl`, `_record_costs`) goes with them.

#### A7. Legacy strategies clean-up — S
- Replace the undocumented hard-coded `Decimal("5")` order cap in three
  strategies (in input-token units) with an explicit `order_usd` argument.
- Log at DEBUG with lazy arguments, and only on state changes; remove the
  INFO-per-tick lines of `TargetValueStrategy` and the composer.
- Freeze them: no new features. Their future (port to specs or delete) is B6.

#### A8. Notifications off the event loop — S
- `TelegramNotificationService` uses sync `requests.post` (10s timeout) inside
  the loop. Make `Notifier.send_message` async over `httpx.AsyncClient` and
  schedule it as a task, so it can never stall the trade-runner's submit lock.

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
- Fix: a trade resolved by hand as `executed` has no order JSON, so its
  position is not restored. `ledger resolve` should take the fill amounts (or
  read them from the signature) and write the order.

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
| A5 Split the ledger | not started | |
| A6 Split `main.py` | not started | |
| A7 Legacy strategies clean-up | not started | |
| A8 Async notifications | not started | |
| A9 Small clean-ups | not started | |
| A10 Automated live checks | **done** (2026-09-29) | `tests/live/` (5 tests, ~45s, `uv run pytest -m live`): market data, spec and random backtests on real candles, paper swaps, a 30s paper bot on the websocket plus a deterministic replay of its ticks. First run caught that the example spec can never pass `validate` (fixed 2099 expiry); the test uses a fresh copy |
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
