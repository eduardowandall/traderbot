# History

Finished roadmap stages, moved out of [`plan.md`](plan.md) on 2026-09-30 so the
plan only carries open work. The text is kept as written at the time; later
stages changed some of it (stage U removed dry mode, the manual swap, the
`ledger`/`pnl`/`paper`/agent commands and the kill switch). Stages U and T
are after stage S, and stage B (moved on 2026-10-05) is last. For how the
code works today, read [`architecture.md`](architecture.md).

**Labels.** Code comments, `AGENTS.md` and the soak report (A2) cite these
stages by label (A4, R1, S7, B7, ...). On 2026-10-05 the plan was renewed and its
items renumbered from A1, so the first stage A is called **stage A
(2026-09)** here; an A label in older text or code means that one.

## What was built (short)

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

## Stage order (stage A, 2026-09)

**Why this order.** A1–A3 leave exactly one path by which money moves and gets
recorded, so the manual bucket, the trade-runner and the reports plug in once.
A4 gives every trade a USD value, which allocations, budgets and cost
accounting all need. A5–A6 split the two files that stage B will grow most
(`ledger.py` gets a `strategies` table, `main.py` gets `serve`/`approve`).
A7–A9 are cheap and remove known rough edges before more code builds on them.

### Stage A (2026-09) — Foundations (refactor first)

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

#### A11. Legacy strategies become specs — S (added 2026-09-30)
- **Owner decision:** port `RandomStrategy`, `TargetValueStrategy` and the
  composer family to specs, then delete every legacy strategy, including
  `RandomStrategy`. This replaces the old B6 plan to keep it for testing.
  Composing strategies is now the spec's job: `entry.mode` and `exit.mode`
  (`all`/`any`) over condition blocks.
- **New spec blocks.** New union members only, so existing spec ids don't change:
  - `random_chance {pct: 1..100}` is a market condition (entry or exit) that
    holds with `pct`% probability on each evaluation. It draws from the
    strategy's `rng`, which travels in `TickContext.rng`, so a backtest with
    `--seed` is deterministic. `run ... --seed N` seeds a live run.
  - `trailing_take_profit {pct, trail_pct}` is an exit condition. It arms
    once the peak since entry reaches `entry * (1 + pct/100)`, then fires
    when the price drops `trail_pct`% from that peak. Unlike the legacy
    band, it stays armed until it fires.
  - `rebound_from_low {window, pct}` is a market condition: the price is at
    least `pct`% above the lowest close of the last `window` bars
    ("stopped dropping").
- **Conversions** (examples in `docs/examples/`, all SOL-USDC, `fixed_usd` 5):
  - `spec-random.json`: `random_chance` 40 to enter, `random_chance` 20 to
    exit. It needs the required stop, so it adds `stop_loss` 50 (the widest
    allowed). Because of re-arm, the entry needs a chance below 100 to buy
    again after an exit.
  - `spec-target-value.json`: `price_below` target plus `rebound_from_low`
    to enter; exit on `trailing_take_profit {pct 5, trail_pct 1}`. The
    legacy strategy had no stop below entry (§7.1), so this adds a
    `stop_loss` of 10%. `max_spread` is dropped, since the bot never passes a spread.
  - `spec-wma-composer.json`: the composer's defaults. Entry `all` of
    WMA 50<100, 15<100, 15>30 and 5>10 on `15_SECOND` bars; stop
    `trailing_stop` 3.1%.
- **Removed:** every class in `trader/trading_strategy.py` except the
  `TradingStrategy` base (clock, rng, setup), plus the `random`,
  `target_value` and `composer` CLI entries (the follow-up below removes the registry), the
  `tests/strategies/` suite, and the `trader.trading_strategy` console/replay
  logger entries. The live suite, `/smoke` and the backtest tests use
  `spec-random.json`.
- **Follow-up (owner, 2026-09-30): the spec is the only strategy, so the CLI
  takes just its file.**
  - `run <mode> SPEC_FILE [--seed N] [--record-ticks FILE]` and
    `backtest SPEC_FILE (--ticks FILE | --candles N) [--interval ...]`. The
    strategy name, the `'key=value'` strategy arguments and the `SYMBOL`
    argument go away: the pair comes from the spec, which also sets the
    default candle `--interval` (its `timeframe`).
  - Removed: `trader/strategies_registry.py` (`STRATEGIES`,
    `NotImplementedStrategy`), `get_strategy_obj`, `check_symbol`, the
    non-spec bucket branch of `run`, and `SpecStrategy.from_file`'s `seed`
    (the CLI seeds it). A spec that fails to parse is a `BadParameter`.
    `parse_kwargs` stays for `--notification-args`.

#### A12. Re-entry relative to the last exit — S (added 2026-09-30)
- **Owner request:** a test spec that takes profit at +0.12%, stops at
  -0.05%, and buys back 0.05% below the price it last sold at (right away
  when it has never sold).
- **New entry conditions** (entry only). The "no exit yet" case is spelled
  out in the spec, never implied (owner, 2026-09-30):
  - `below_last_exit {pct}` (`pct` >= 0) holds when the price is at or below
    `last_exit * (1 - pct/100)`. It is **false** when there is no last exit
    price.
  - `no_last_exit {}` holds while the bucket has never exited (no exit time
    in memory or in the ledger).
  - "Buy now, then 0.05% below each sale" is therefore `entry.mode: any`
    over `[no_last_exit, below_last_exit 0.05]`.
  - The strategy remembers the price of its own sell signal and uses it once
    it sees the position close. That is the quote the sell was sized on;
    the fill can differ by the slippage.
  - Restart: `Ledger.last_exit_price(account)` reads `Order.price` of the
    last executed sell. It travels in `AccountState` -> `AsyncAccount` ->
    `BucketSnapshot.last_exit_price` -> `resume(..., last_exit_price=)`,
    next to `last_exit_at`. A sell without an order yields no price: the
    bucket has exited, so `no_last_exit` is false too, and it waits
    instead of buying blind (fix the row with `ledger resolve`).
  - Re-arm still applies: right after an exit the price is at the exit,
    so the condition is false and re-arms by itself.
- **Example:** `docs/examples/spec-scalp-test.json` (SOL-USDC, `15_SECOND`,
  `fixed_usd` 5, `stop_loss` 0.08 (owner's edit; first asked 0.05), `take_profit` 0.12).

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
- **Design (2026-09-29):**
  - `trader/execution/orders.py`: `order_from_fill(fill, quote, token, side,
    now, signal_price=None, usd=None, requested_quantity=None)` **never
    raises**.
    - It uses the real amounts; if one side is missing or zero, it falls back
      to the quote's; if that fails too, it logs an ERROR and returns an
      order with quantity 0 rather than raise.
    - The USD price of the token: the fill price when the quote is a stable;
      otherwise the signal's (USD feed) price; else 1 when the token is a stable; else
      `usd(quote) × fill price`, else `usd(token)`.
    - Rates come from `trade_rates` + `with_sol_usd`.
    - A bucket passes `(input, output, side, price)`; a manual swap passes
      `(spend, receive, BUY)`.
    - `priced_mints(quote, token)` gives the one snapshot rule for both.
  - **The costs rule wins over a single write.** EXECUTED is still written
    before the cost fetch, and `record_fill` after it. The second write
    becomes **non-fatal**:
    - a failure is logged at ERROR with the whole order JSON;
    - the in-memory book is updated anyway.
  - **Restore no longer needs the order JSON.** `last_executed_trade`
    accepts EXECUTED buy/sell rows without `order_json`, and `_open_entry`
    rebuilds the entry from the row: the signature, `out_amount` (or the
    intent's quantity) and the intent's price. A lost `record_fill`, or a
    trade resolved by hand, therefore restores its position: no re-buy and
    no stuck sell.
  - A failed `mark_executed` logs the `SwapResult` (signature and amounts)
    at ERROR before re-raising. The intent stays EXECUTING and trading
    stays blocked, which fails closed.
  - `ledger resolve <mode> <id> executed` attaches an order:
    - in real mode it reads the transaction (`fetch_swap_costs` on a
      `SwapResult` rebuilt from the intent) for the real amounts and fees;
    - otherwise it uses the intent's requested amounts, with costs unknown;
    - a resolved sell also books its realized PnL against the entry that
      was open before it.
  - `SOL_MINT` in `trader/models/mints.py` replaces the four local copies.

#### R2. Atomic authorization — S
- **Problem:** `_authorize` reads `find_by_idempotency_key` and
  `policy_state()` outside the write transaction, and there is no unique
  index on the key. Two processes (bot and CLI, or two runners) can both
  pass the daily limit, or both use one idempotency key.
- **Change:** check and insert in one `BEGIN IMMEDIATE`, plus a partial
  unique index on the key for moved-funds statuses. This is a prerequisite
  for B3.
- **Design (2026-09-29):**
  - `Ledger.authorize(intent, decide) -> (existing, decision)`. Inside one
    `_write()` (`BEGIN IMMEDIATE`) it:
    1. finds a moved-funds intent with the same key, and if there is one
       returns it without writing anything;
    2. reads `policy_state()`;
    3. calls `decide(state)`, the pure `evaluate` supplied by the gateway;
    4. records the intent (or the collapsed denial).
  - `TradeGateway._authorize` only turns the result into
    `DuplicateIntentError` or `PolicyDeniedError`. `record_intent` stays
    available, built on the same locked helper.
  - The partial unique index `ux_intents_moved_key` covers `idempotency_key`
    for the statuses executing, executed and unconfirmed. `_migrate` creates
    it; on an old ledger that already has duplicates it logs a warning and
    skips, so it never blocks opening a ledger.

#### R3. Balance reads fail loudly — S
- `AsyncRPCClient.get_account_balance` swallows `SolanaRpcException` and
  returns partial or empty balances, which the account caches for 3 minutes
  as zeros. That gives false reconcile mismatches and sells sized to zero.
  Change: raise, never cache a partial read, and sum several token accounts
  of one mint.
- The `get_lamports` retry waits on `httpx.ReadTimeout`, but solana-py
  wraps everything in `SolanaRpcException`, so it never fires.
- **Design (2026-09-29):**
  - `get_account_balance` no longer catches `SolanaRpcException`: a failed
    read raises. `AsyncAccount` only replaces its cache after a successful
    read, so the next `get_balance` tries again. The caller turns the error
    into an `error` reply, which goes through the backoff, instead of
    trading on a zero balance.
  - Several token accounts of one mint are summed.
  - Reads (`get_lamports`, `get_account_balance`) share one retry policy,
    `_READ_RETRY`: exponential waits from 0.5s up to 4s, 4 attempts. It
    fires on transient errors, meaning a transport error (httpx or
    httpx2) or a 429/5xx response, whether raised directly or wrapped as
    the `__cause__` of `SolanaRpcException`. Sends are never retried here.

#### R4. Rate limits and startup resilience — S
- One tenacity policy for the Jupiter HTTP calls: exponential backoff on
  429, 5xx and timeouts, honoring `Retry-After`. It applies before the
  broadcast only. Today only `get_quote` sleeps, once, for 1.5s.
- `_startup` (candles, reconcile) runs inside the loop's backoff; today one
  429 at startup exits the bot.
- A minimum tick interval, plus skipping the websocket for N seconds after
  it fails, so a dead feed does not hammer the Price API fallback.
- **Design (2026-09-29):**
  - `async_jupiter_client._HTTP_RETRY` (tenacity, 4 attempts) is used by
    `get_quote`, `get_swap_transaction`, `get_usd_prices` and `get_candles`.
    - It retries transport errors, 429 and 5xx.
    - It waits `Retry-After` (capped at 30s) when the response has one,
      otherwise exponentially from 0.5s up to 8s.
    - All four calls happen before the broadcast, so a retry can never send
      twice. The one-off 1.5s sleep on a 429 in `get_quote` and the
      ReadTimeout-only retry on candles go away.
  - `_do_swap_with_retry` waits 0.5s, then 1s, between its attempts.
  - The bot's `_loop` runs `_startup` as a step inside the same
    try/backoff. The bucket is opened only once (`_opened`), and startup
    repeats until the warm-up candles arrive; after that, the loop ticks.
  - `JupiterMarketData`: when the websocket **fails** (not merely goes
    quiet), it is skipped for `WS_COOLDOWN_SECONDS` (60s) and prices come
    from the Price API, polled at most every `REST_POLL_SECONDS` (2s).

#### R5. Sells stay within the position — S
- A sell is capped at the position's quantity, not at the wallet's balance.
  The request's quantity comes from the client, and sells skip every budget
  rule.
- A partial sell (the fee reserve, dust) keeps the remainder open instead
  of `book.close` closing the whole position and dropping its cost from
  PnL.
- **Design (2026-09-29):**
  - `AsyncAccount.sell`: quantity = min(requested, position, spendable).
  - After the fill, the remainder is `entry − sold`:
    - if the sell was **not** capped by the wallet and the remainder is more
      than `DUST_FRACTION` (1%) of the entry, `PositionBook.reduce` books
      the sold part and keeps the remainder open;
    - otherwise the position closes (as today) and a `position_leftover`
      event records the tokens left and their cost basis, so the amount is
      accounted for even though no bucket holds it.
  - `remainder_entry(entry, sold)` (`models/book.py`) scales the entry's
    quantity, quote amount and costs to what is left. `reduce` and restore
    both use it.
  - **Restore understands partial exits.** `Ledger.legs_since_last_buy`
    returns the last executed buy and the sells after it; the open entry is
    the buy minus those sells, and is empty once it is fully sold or dust.
    It replaces `last_executed_trade` in `restore`.
  - The sell's idempotency key becomes
    `"<account>:sell:<entry order_id>:<entry quantity>"`, so selling the
    remainder is a new key, while resending the same sell is still
    deduplicated.

#### R6. A spec run enforces its own limits — S
- `run ... spec` never calls `validate()`, gives the bucket no
  `budget_usd`, and ignores `max_loss_usd` (nothing reads it). The backtest
  applies the budget; the live run does not.
- **Change:** validate against the mode's policy when loading, pass
  `spec.budget_usd` to the bucket, and stop the bucket and close its
  position once realized PnL reaches `-max_loss_usd`. The bucket is named
  `strategy:<spec_id>`, not the pair, so a spec never adopts a position it
  did not open.
- **Design (2026-09-29):**
  - **Relative expiry, pulled forward from R8.** Validating at load would
    otherwise reject the example spec (2099). A spec has exactly one of
    `expires_at` or `ttl_days` (1–365); `ttl_days` counts from the moment
    the strategy first sees a tick, which is the replay's time in a
    backtest. `validate` checks either one against `max_days`. The example
    now uses `ttl_days: 30`.
  - `run ... spec` loads the spec, validates it against
    `load_policy(mode)` (as `strategy validate --mode` does), and fails with
    the errors listed. It then opens the bucket `strategy:<spec_id>` with
    `budget_usd` and `max_loss_usd`.
  - `TradeService.open_bucket(..., max_loss_usd=None)`. After every fill,
    and at open for a restored bucket, a realized PnL of `-max_loss_usd` or
    worse marks the bucket `RETIRING`:
    - it adds a `bucket_max_loss` event;
    - buys are then rejected (even from a client that ignores the status);
    - the bot stops, as it already does for `retiring`;
    - since the loss is realized by a sell, no position is left to close.
  - `LocalTradeClient` and `Backtester` gain `max_loss_usd`, and the spec
    backtest passes it, so the backtest stops where the live run would.

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
- **Design (2026-09-29):**
  - **Seeding.** Each condition declares `history()`, the bars it needs for
    a converged value: 5× the period for EMA and RSI, which are recursive,
    and the plain lookback otherwise. `spec.history()` is the maximum,
    capped at 1000 (the candle API's practical limit). The strategy warms up
    and keeps that many bars, and counts as warm only once it has them.
    `market summary` fetches at least 250 bars (5× EMA50), so it and a spec
    compute on converged series and agree.
  - **Candle timestamps (checked live, 2026-09-29):** `time` is the bar's
    **open**, and the newest candle is still forming (29s old on a 1-minute
    bar). `ticks_from_candles(candles, interval)` therefore:
    - turns each bar into four ticks, open → low → high → close. Taking the
      low before the high is the conservative order for long stops;
    - stamps them inside the bar, with the close at its end;
    - drops a candle that has not closed yet.

    Without `interval` it keeps the old one-tick-per-close behavior, which
    recorded ticks and old callers use.
  - **Live and backtest now evaluate alike:** on every tick, against the
    forming bar. Stops see the bar's low, and a fill happens at the tick
    that triggered it, like a live tick. So there is no separate
    "next-bar open" rule.
  - **Slippage:** `Backtester(slippage_bps=10)` adds to `fee_bps`
    (adverse, both sides), and `strategy backtest --slippage-bps` exposes
    it.
  - **Too little data:** `strategy backtest` returns
    `{"ok": false, ...}` when the window has fewer bars than
    `spec.history()`, and reports `bars` and `warmup_bars` otherwise.
  - **Re-arming instead of crossovers.** After an exit, an entry needs its
    conditions to be false at least once before it can fire again. That
    stops the re-buy right after `take_profit` without new condition types;
    crossovers stay a B6 option.

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
- **Design (2026-09-29):**
  - `json_errors` keeps the specific messages for `SpecParseError`,
    `ValueError` and `OSError`, and adds a catch-all: any other exception
    becomes `{"ok": false, "errors": [{"path": "", "msg": "<Type>: <msg>"}]}`
    with exit code 1.
    - **Limitation:** Typer rejects bad arguments before the command runs,
      so those errors are still plain usage text with exit code 2.
  - A failed `strategy validate` prints `spec_id`, `warmup_bars` and
    `timeframe` next to the errors (`fail(errors, **extra)`). `warmup_bars`
    is now `spec.history()`, the real warm-up.
  - **Schema:**
    - the bounded Decimal types (`Pct`, `Margin`, `RsiLevel`, `Usd`) publish
      a plain number with their bounds and units (`WithJsonSchema`); parsing
      still accepts strings;
    - every top-level spec field gets a `description`.
  - `spec_id` hashes the canonical JSON **without** `name`, `agent_id`,
    `rationale` and `supersedes`. Two specs that trade the same way now
    share one id and one bucket. There is no registry yet, so no stored id
    changes.
  - `ttl_days` shipped in R6.

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
- **Design (2026-09-29):**
  - **Logs:**
    - `paths.logs_dir()` resolves `TRADER_LOG_DIR`, else
      `<project root>/.logs`, like the other paths; tests point it at
      `tmp_path`;
    - the file handler is a `RotatingFileHandler` (10 MB × 5 per process);
    - `log_ticker` (one line per price tick) is already DEBUG on the `bot`
      logger. It stays: rotation caps the file, which is what grew.
  - `_on_error` no longer calls `traceback.print_exc()`; the logged
    `exc_info` goes through the redaction.
  - **CI:**
    - a `windows-latest` job next to ubuntu;
    - pyright's `include` covers `trader`, `tests` and `main.py`;
    - a `live.yml` workflow (`workflow_dispatch` only) runs
      `pytest -m live`.
  - **Paper wallet:** `apply_swap` runs under a lock file
    (`paper-wallet.json.lock`). It reloads from disk, computes the new
    balances on a copy, saves, and only then updates memory. `balances()`
    reloads first too.
  - **CLI default mode:** `pnl` defaults to `dry` like the other ledger
    commands and `run`/`swap`. Pass `paper` explicitly.
  - **`ledger resolve real`** checks `HELIUS_RPC_URL`, and for `executed`
    also `SOLANA_PRIVATE_KEY`, **before** writing anything.

### Stage S — Second recheck fixes (added 2026-09-29)

A second review after stage R covered the same three areas and looked
especially at the code stage R added. Each finding was checked in the code;
the spot checks included the fill at the bar low, `MAX_HISTORY` against the
candle limit, the unguarded manual `record_fill`, EXECUTING counting as
unresolved, and the resolve write order. These items come before stage B,
for the same reason as stage R.

#### S1. Backtest fidelity, round two — S
- **Fills at the bar's extremes.** Every tick fills at its own price, and
  the OHLC path is only four points. So a dip entry buys at the bar's low
  and a take-profit in the same bar sells at its high: a probe bar
  (100/90/101/100) turned about +3% into +12%.
  - **Design:** interpolate each open→low→high→close segment with
    `PATH_STEPS` (8) evenly spaced ticks, so a level-triggered condition
    fires within about 1/8 of the segment of where it was crossed.
    Stops stay conservative, because the low still comes first.
- **Unreachable warm-up.** `MAX_HISTORY` (1000) equals the candle limit, and
  the forming bar is dropped, so a 1000-bar spec (EMA200) could never be
  backtested. **Design:** `MAX_HISTORY = 900`, and a backtest needs
  `history + MIN_EVAL_BARS` (20) bars.
- **RSI converges more slowly than an EMA** (its smoothing is 1/n):
  `RSI_SEED_FACTOR = 10`.
- **Negative costs.** `--fee-bps` and `--slippage-bps` below 0 are rejected.
- **Retirement in replays.** A replay stops evaluating once the bucket is
  `RETIRING` (max loss), as the live bot does, instead of counting every
  later signal as rejected.

#### S2. Strategy state survives a restart — S
- **Problem:** the cooldown, the re-arm and the `ttl_days` clock live only
  in memory. A restart after a take-profit re-buys at once, and a spec that
  is restarted periodically never expires.
- **Design:**
  - `BucketSnapshot` gains `last_exit_at` (the last closing sell) and
    `opened_at` (the account's first intent), both read from the ledger by
    `gateway.restore`;
  - a strategy with `resume(last_exit_at, opened_at)` (the `Resumes`
    protocol) receives them once, at bot startup;
  - `SpecStrategy.resume` restores `_last_exit` with `_armed = False`, and
    derives the `ttl_days` expiry from `opened_at`.
- **Quiet periods.** `BarSeries.update` fills skipped bars with the previous
  close, capped at `maxlen`, so a quiet live feed has the same bar count as
  the candle replay.

#### S3. A running bot follows the ledger — M
- **Resolving while the bot runs.** Today the in-memory book goes stale:
  `resolve executed` leads to a double buy, and a resolved sell leaves the
  bot stuck on a duplicate key. **Design (as built):** before every order,
  inside the submit lock, `TradeService` calls
  `account.sync_with_ledger()`, which rebuilds the book from the ledger and
  warns only if the position changed. The first idea was to re-read only
  after a denial, but that misses the main case: once the intent is
  resolved, the next buy is *allowed* and would buy twice. The book is fully
  derived from the ledger, and orders are infrequent, so a sync costs a few
  reads.
- **Max loss with a remainder open.** Since R5 a partial sell can leave a
  remainder, which a retiring bucket would abandon. **Design:** when the
  bucket retires and still holds a position, the service closes it right
  away at that order's price, and the bot stops only once it is flat.
- **Restore of partial sells:**
  - a sell row without `order_json` uses the intent's quantity as the
    amount sold, instead of meaning "closed";
  - restore applies `remainder_entry` one sell at a time, as memory does,
    so the dust threshold matches.
- The manual swap's `record_fill` becomes non-fatal and logged, as in the
  account.
- The paper executor reloads the wallet before deciding whether a rent
  charge applies.

#### S4. Correct across processes — S
- **In-flight intents.** An `EXECUTING` intent in another process blocked
  every sell (a safety rule). **Design:** only `UNCONFIRMED` intents, plus
  `EXECUTING` ones older than `STALE_EXECUTING_SECONDS` (300), count as
  unresolved; inside one process the service lock already serializes
  orders.
- **Lifecycle writes.**
  - `resolve` checks the status inside the write lock;
  - `mark_executed` / `mark_failed` / `mark_unconfirmed` only move an
    intent that is still active (`WHERE status IN (...)`), so a late bot
    cannot overwrite an owner's resolution;
  - `ledger resolve --signature` records a signature that only reached the
    ERROR log.
  - **As built:** a late `mark_*` on an intent that is no longer active
    (the owner already resolved it) writes nothing and logs an ERROR with
    the payload; the owner's decision stands.

#### S5. Secrets on crashes — S
- **Unmasked tracebacks.** An exception that escapes a CLI command is
  printed by Typer's rich hook, which bypasses the redaction and can show
  the Helius URL from a chained `httpx2` error. **Design:**
  - `typer.Typer(pretty_exceptions_enable=False)`;
  - `logging_config.install_excepthook()`, set in `main.py`, prints
    tracebacks through `redact()`.
- **`/diagnose`** points at `logs_dir()/trader-*.log` and warns about the
  old files that still hold the key.

#### S6. Resolve writes last; retries have a deadline — S
- **Resolve order.** `ledger resolve` now reads the chain (fee, order)
  **before** `resolve()`, so a failed RPC leaves the intent unresolved and
  the command can be re-run. `get_confirmed_transaction` goes under
  `_READ_RETRY`.
- **Retry deadline.** Nested retries could stall an order for about 9
  minutes. **Design:** `_do_swap_with_retry` starts no new attempt after
  `SWAP_DEADLINE_SECONDS` (60), and `Retry-After` is capped at 10s. It is
  never applied during an attempt, because a timeout mid-send would be
  unsafe.

#### S7. Paper wallet lock, complete — S
- `reset` runs under the lock.
- Each writer uses a pid-unique `.tmp` file.
- The stale-lock break re-checks the lock's mtime right before unlinking,
  so it never deletes a lock another process just took.

#### S8. Ops leftovers — S
- **Log retention:** each process has its own rotation, but nothing prunes
  old files (`.logs/` is 231 MB). **Design:** at startup, delete
  `trader-*.log*` files older than `LOG_RETENTION_DAYS` (14). File names
  get the pid, so two processes started in the same second never share a
  file.
- **CI:** `addopts` gets `-W error::ResourceWarning`, which CI now enforces;
  both workflows get `permissions: contents: read`.
- **Smoke script:** `smoke.py` sets `TRADER_LOG_DIR` inside its isolated
  folder.
- **Docs:** `architecture.md` shows `logs_dir()`.
- **Startup alert:** after 5 consecutive startup failures the bot sends one
  notification (a datapi outage would otherwise go unnoticed).


## Progress of the finished stages

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
| A11 Legacy strategies become specs | **done** (2026-09-30) | 535 tests. `random_chance`, `trailing_take_profit`, `rebound_from_low`; `docs/examples/spec-{random,target-value,wma-composer}.json`; `trading_strategy.py` keeps only the base; `run`/`backtest` take just the spec file (532 tests); `tests/strategies/` removed, new `test_ported_legacy.py` |
| A12 Re-entry below the last exit | **done** (2026-09-30) | 538 tests. `below_last_exit` + explicit `no_last_exit` (entry-only `EntryCondition` union), `Ledger.last_exit_price` through the restore path to `resume(last_exit_price=)`; `spec-scalp-test.json`. Paper smoke: bought on the first tick, stop at -0.05% after 4s, then waited below the exit |
| R0 Recheck: key leak, double send | **done** (2026-09-29) | 489 tests. `httpx2` silenced + `RedactingFormatter`; send errors are `TransactionSubmittedError` (regression test fails on the old code). Owner: purge the 6 leaked log files, rotate the Helius key |
| R1 Never lose a fill | **done** (2026-09-29) | 498 tests (9 new). `execution/orders.py` (never-raise `order_from_fill`, shared by buckets and manual swaps), non-fatal `record_fill`, restore rebuilds entries without `order_json`, `ledger resolve executed` attaches an order (from the chain in real mode), `SOL_MINT` |
| R2 Atomic authorization | **done** (2026-09-29) | 503 tests (5 new). `Ledger.authorize` (key check + `policy_state` + insert in one `BEGIN IMMEDIATE`), partial unique index `ux_intents_moved_key`; a two-connection test shows the second process waits and sees the first |
| R3 Balance reads fail loudly | **done** (2026-09-29) | 509 tests (6 new). `get_account_balance` raises instead of returning partial or empty balances, sums a mint across token accounts; `_READ_RETRY` (backoff) on transient errors, including the ones solana-py wraps |
| R4 Rate limits + startup | **done** (2026-09-29) | 515 tests (6 new). `_HTTP_RETRY` (Retry-After, exponential backoff, 429/5xx/transport, pre-broadcast only) on quote/swap tx/prices/candles; pauses between swap attempts; startup inside the loop backoff; websocket cooldown and paced Price API polling |
| R5 Sells within the position | **done** (2026-09-29) | 519 tests (4 new). Sell capped at the position; `PositionBook.reduce` + `remainder_entry` keep a partial remainder open (costs scaled); `Order.closes_position`; restore = last buy minus later sells; unsellable leftovers recorded as `position_leftover`; sell key includes the entry quantity |
| R6 Spec runs enforce limits | **done** (2026-09-29) | 527 tests (8 new). `run ... spec` validates against the mode's policy, bucket `strategy:<spec_id>` with `budget_usd`/`max_loss_usd`; max loss retires the bucket (buys rejected, bot stops, event); backtests stop the same way; `ttl_days` relative expiry (example validates now) |
| R7 Faithful indicators/backtests | **done** (2026-09-29) | 534 tests (7 new). `history()` seeding (5x for EMA/RSI, capped at 1000), `market summary` >= 250 bars; candles become open-low-high-close ticks inside the bar, forming bar dropped (candle `time` is the open, checked live); `slippage_bps` (default 10); too-few-bars error; re-arm after exits |
| R8 Agent contract | **done** (2026-09-29) | 538 tests (4 new). Catch-all JSON errors (Typer argument errors stay text), failed `validate` keeps `spec_id`/`warmup_bars`, schema numbers with bounds and units, descriptions on every top-level field, `spec_id` ignores metadata |
| R9 Operations | **done** (2026-09-29) | 543 tests (5 new). Rotating logs under `logs_dir()` (`TRADER_LOG_DIR`), no raw `print_exc`, CI on ubuntu + windows, pyright covers tests/main, manual `live.yml`, paper wallet lock/reload/commit-after-save, `pnl` defaults to dry, `resolve real` checks env first. Live suite 6/6 |
| S1 Backtest fidelity 2 | **done** (2026-09-29) | 547 tests. Interpolated bar paths (8 steps per leg; the reviewer's +12% probe is now under +8%), `MAX_HISTORY` 900 + 20 evaluated bars, RSI seed x10, negative costs rejected, replay stops on retirement |
| S2 Strategy state across restarts | **done** (2026-09-29) | 552 tests. `Ledger.account_times` -> `AccountState`/`BucketSnapshot` `opened_at`/`last_exit_at`; the bot calls `resume()` (`Resumes` protocol) once at startup; `SpecStrategy.resume` restores the cooldown, re-arm and `ttl_days` clock; `BarSeries` fills quiet bars |
| S3 Running bot follows the ledger | **done** (2026-09-29) | 556 tests. `sync_with_ledger()` before every order (a test pins the resolve-then-rebuy case); a retiring bucket with a remainder sells it and the bot stops only when flat; restore applies sells one at a time (rows without `order_json` use the intent's quantity); manual `record_fill` non-fatal; paper executor reloads before the rent check |
| S4 Cross-process correctness | **done** (2026-09-29) | 558 tests. Fresh EXECUTING no longer blocks other processes (only UNCONFIRMED or EXECUTING older than 300s); `resolve` checks and writes in one lock; late `mark_*` on a resolved intent is skipped and logged; `ledger resolve --signature` |
| S5 Secrets on crashes | **done, but ineffective, see T3** (2026-09-29) | 559 tests. `typer.Typer(pretty_exceptions_enable=False)` + `logging_config.install_excepthook()` (tracebacks through `redact()`); `/diagnose` points at `trader-*.log` and warns off the old files |
| S6 Resolve last + retry deadline | **done** (2026-09-29) | 561 tests. `ledger resolve` reads the chain first (`_prepare`), writes last (`_apply`), so a failed RPC leaves it re-runnable; `_READ_RETRY` on `get_confirmed_transaction`; no new swap attempt after 60s; `Retry-After` capped at 10s |
| S7 Paper wallet lock | **done** (2026-09-29) | 562 tests. `reset` under the lock, pid-unique `.tmp`, stale-lock break re-checks the mtime before unlinking |
| S8 Ops leftovers | **done** (2026-09-29) | 564 tests. Logs pruned after 14 days and named `trader-<ts>-<pid>.log`; `-W error::ResourceWarning` in addopts; `permissions: contents: read` on both workflows; smoke isolates `TRADER_LOG_DIR`; architecture doc updated; one alert after 5 startup failures |

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

### Progress of stages U and T

| Item | Status | Notes |
|---|---|---|
| U1 Cut and delete | **done** (2026-09-30) | 456 tests (was 540). CLI is `run` + `backtest`; dry mode, kill switch, hash chain, manual swap, agent/ledger/pnl/paper commands gone; `trader/` 8,989 -> 7,722 lines, `tests/` 8,739 -> 7,564. The old ledgers in `.data/` are refused (old format) |
| T1 EXECUTING blocks own bucket (S4 regression) | **done** (2026-09-30) | An EXECUTING intent of the same account always blocks (`policy_state(account=)`); legs ordered by `created_at` |
| T2 Bounded remainder close (S3 regression) | **done** (2026-09-30) | `_Bucket.closing`: one remainder sell per order (the test hit `RecursionError` without it) |
| T3 Excepthook overridden by Typer (S5 gap) | **done** (2026-09-30) | `main.main()` routes an escaping error through `redacted_excepthook`; subprocess test with a fake api-key |
| T4 State/restore gaps | **done** (2026-09-30) | `_resumed` retried apart from `_opened`; gaps over `MAX_GAP_BARS` restart the series and cool the spec; `bucket_opened` event starts `ttl_days`; late marks are reported and `attach_order` needs EXECUTED; `closes_position` on the intent row. The resolve item is obsolete (U1) |
| T5 Same-bar entry+exit bias | **done** (2026-09-30) | Falling bars replay open -> high -> low -> close |
| T6 Resolve/CI/docs gaps | **done** (2026-09-30) | `-W error::pytest.PytestUnraisableExceptionWarning`; wallet reads under the lock, `os.replace` retried on `PermissionError` (the lock wait stays synchronous: it is held for milliseconds). Resolve and log-pruning items obsolete; docs items in U3 |
| U2 Straighten the order path | **done** (2026-09-30) | `trader/execution/account.py`; the cap is `buy(limit_usd=)` (no callback); `TradeService._place`/`_execute` classify and dispatch, errors logged once; one balance read per buy; `provider.buy(spend_amount)`; `trader/bot/decision.py` shared by the bot and the backtester (a retiring bucket with a leftover keeps being replayed); one `Strategy` protocol (no `TradingStrategy`, `WarmsUp`, `Resumes`); `pnl_totals` replaces `total_realized_pnl`. Also removed the per-order `sync_with_ledger` (it only followed manual resolutions). Paper wallet file re-reads kept (only on cache misses) |
| U3 Docs you can follow | **done** (2026-09-30) | `architecture.md` (424 -> 174 lines) follows one paper buy hop by hop with file:line; README (192 -> 94) leads with paper; AGENTS.md (120 dense lines -> 113 short ones); example rationales fixed; `.env.example` lists every variable. Stage U total: `trader/` 8,989 -> 7,788 lines, 466 tests |

## Stage B — Features (2026-09-30 → 10-05)

Moved out of `plan.md` on 2026-10-05, when the plan was renewed and its
items renumbered from A1. Text as written at the time. What stayed open
went to the new plan: B8 (perps), B7's owner approval and service
entrypoint, B10's C3 and C7, and the soak's findings.

### Order (set 2026-09-30)

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
From the soak ([A2](#a2-paper-soak--owner-done-2026-10-05) F7): a 5 USD SOL-USDC round trip
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
- `LocalTradeClient` stays: the backtest and the tests use it. (Removed
  later: the backtest calls its `TradeService` directly, and the tests run
  the bot against a local `TradeRunner`, which now requires its `hub` and
  `candles`.)

Tests: the CLI has `backtest`, `connect` and `serve`; `connect --seed` and
`--record-ticks` reach the bot; an unreadable spec fails before connecting;
`/smoke` end to end by hand.

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


### Progress of stage B

| Item | Status | Notes |
|---|---|---|
| A0–A12, R0–R9, S1–S8 | **done** (2026-09-23 → 30) | See [`history.md`](history.md) |
| U1–U3, T1–T6 | **done** (2026-09-30) | See [`history.md`](history.md) |
| B4 Real-mode guardrails for agent sessions | **done** (2026-09-30) | 546 tests (43 new). `.claude/settings.json` denies Read `.env` and Edit `.env`/`policy.toml`; `.claude/hooks/guard_commands.py` (PreToolUse, Bash/PowerShell) denies real runs, `--env-file`, `TRADER_POLICY_FILE`, `.env`, printing the key/RPC URL and the real ledger. Checked live: it blocked this session's own command. It matches command text, so docs that mention these are edited with the Edit tool |
| Owner: one-week paper soak | in progress (since 2026-10-03) | `serve paper` + 3 `connect`s. First review (2026-10-04, 12.5 h) in [A2](#a2-paper-soak--owner-done-2026-10-05): execution path clean; candle warm-up collapses on thin tokens (F1), a quiet websocket costs 30 s per tick (F2), SOL `connect` logs keep only ~2.5 h (F3). F4 fixed (2026-10-04): the trade-runner logs bucket opens, connects and disconnects |
| B3 Trade-runner / strategy-runners | **done** (2026-09-30) | `serve <mode>` / `connect spec.json`; `trader/runners/{lock,trade_runner,strategy_runner}.py`, `trading_service/{wire,remote}.py`; one execution process per mode (OS lock, `run` too); exit sweep every 30s. 12 new tests plus a live test (a separate `connect` process trades through `serve paper`). The guard also blocks `serve real` and `trader-real.json` |
| B2 Wallet allocation + reconcile | **done** (2026-09-30) | `WalletBalances` shared by the service's accounts; budgets must fit the wallet (positions at cost); startup reconcile of every bucket's positions blocks buys of a missing token; per-account `reconcile_position` removed; `ledger_dump.py` shows each bucket's position |
| Gap review after B3/B2 | **done** (2026-09-30) | 575 tests. A transaction the chain confirmed as failed is retried with the slippage escalation and ends FAILED (was UNCONFIRMED: the mode was blocked); RPC reads at Confirmed (was Finalized: stale balances right after a swap) and sells re-read the wallet; provider rejections (`recusado: ...`) don't count toward the breaker; a balance read that overlaps a fill isn't cached; a leg without a SOL price uses the other leg's; sells keep a client-given idempotency key; `connect` re-reads the connection file after a trade-runner restart and gives up after 5 reconnects so the bot loop backs off |
| B5 Accounting and performance | **done** (2026-10-03) | 603 tests (28 new) plus 2 live checks. `Position.unrealized_usd`, in fill notifications and the tick log; `trader/execution/notification/daily_report.py` (`DailyReporter`, run beside `run` via `BotConfig.background` and beside `serve`; a `daily_report` event per UTC day); paper fills `slippage_bps=10` below the quote, floored at `otherAmountThreshold`; `backtest --network-fee-usd` (0.002) taken from each replay leg; failed-on-chain attempts ride on `SwapResult.failed_signatures` / `SwapFailedError`, and `execute_trade` books their `meta.fee` (`failed_tx_fee` event, `PositionBook.charge`, subtracted by `pnl_totals`); `trader/backtest/compare.py` + `.claude/scripts/live_vs_backtest.py`. No ledger schema change. Checked live: the warm-up candles end right before the first tick; a real-quote paper fill lands below the quote |
| B6 More expressive strategies | **done** (2026-10-04) | 663 tests (60 new) plus 2 live checks. `pct_of_bucket` sizing; `expr` (`trader/strategy/spec/expr.py`: `ast` plus a whitelist, three-valued logic, normalized text); prices in the quote token everywhere on the strategy side (`Order.quote_price`, `BucketSnapshot.available` + `quote_usd`, `on_market_refresh(..., quote_usd)`); `trader/shared/market/pair.py` (`market_for`, `PairMarketData`, `ratio_candles`); `TradeService` converts USD caps with the quote's price (fail closed without it); replays with two series (`Tick.quote_usd`, a third CSV column, `ReplayPrices`, USD equity); `docs/examples/spec-jup-sol-expr.json`. No ledger schema change; USDC numbers unchanged. Checked live: a JUP-SOL backtest on two real candle series, the pair feed against the Price API, and a JUP-SOL paper smoke run |
| B7 Later | **safety core and price hub done** (2026-10-04); owner approval and a service entrypoint open | 717 tests (34 new) plus 2 live checks. `trader/execution/trade/venues/jupiter/tx_inspection.py` (program allow-list; simulated balances: only the input leaves, up to `inAmount`) in `OnChainExecutor` before sending; `AsyncJupiterProvider` quote check (2% vs the Price API, on in `wiring.py`; buys fail closed, sells warn); `trader/execution/market/hub.py` (`PriceHub`: one websocket for all mints + batched Price API for quiet ones, `StalePriceError` after 30s; `HubMarketData` paces the bot at 1 price/s) used by `run` in-process and by `serve`/`connect` through the new `price` op (fixes the soak's F2); policy `max_trades_per_hour_per_bucket` (6, paper 60); `Order.timestamp` in UTC. Checked live: one websocket subscription carries several mints; the hub keeps SOL and NOBODY fresh; real swap transactions use only allowed programs; the whole live suite (a `connect` trading through `serve paper`) passes. The B6/B7 wire changes mean a `serve` and a `connect` from before B6 can't talk to these: restart all of them together |
| B10 Review cleanup | **done** (2026-10-04) except C3 and C7 (wait for the owner) | 722 tests (4 new, 1 dropped with the mock guard it tested) plus the live suite. `PriceHub.usd_prices` (the process oracle in `run`/`serve`, `build_trade_service(prices=)`, `AsyncJupiterProvider.usd_prices`); stable = 1 in `usd_snapshot`/`price_fn` only (the backtest always uses `ReplayPrices`, identical output); `IntentStatus.REJECTED`; `SwapAttemptsError`; `swap_with_details` merged with the retry loop; priority-fee cap required below `Policy`; `TradeRunner.background`; one policy query; one `restore` per bucket in `ledger_dump.py`. No ledger schema change |
| B9 Costs across modes | **done** (2026-10-04) | `max_priority_fee_lamports` (policy, default 100,000): real's Jupiter `maxLamports` (`veryHigh`), charged by paper, in the backtest's network fee; `trader/backtest/costs.py` measures the pair fee and network fee on Jupiter unless `--fee-bps`/`--network-fee-usd` are given (`measured_costs`); `RoundTripCosts` + `Ledger.round_trip_costs` in the backtest, daily report, `live_vs_backtest.py`, `ledger_dump.py`. No ledger schema change. Checked live: Jupiter keeps a real swap's priority fee under the cap; SOL-USDC measures ~0 bps at 5 USD |

## Decision log up to 2026-10-04

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

## Renewed plan, items A (from 2026-10-05)

#### A1. Soak quick fixes — S (done 2026-10-05)
From the soak report ([A2](#a2-paper-soak--owner-done-2026-10-05)):
- F5: `random_chance` fires per tick, not per unit of time (`specs.md`, and
  live tick rates vary by token); fix the stale rationale of
  `spec-random.json`.
- F8: log sell fills at INFO like buys (`account.py`, `ORDER PLACED`); print
  prices with significant digits instead of 9 decimals (`log_ticker`).
- F3: keep `websockets.client` at INFO in the file handler and log the ticker
  line once per bar, so a long run keeps more than a few hours of logs.
- **Design.** No behaviour, spec id or schema change.
  - F5: since B7 the hub paces every bot at one tick per second whatever the
    token, so live draws are per second; a candle replay makes 4 to 25 ticks
    per bar (`PATH_STEPS`). `specs.md` says "per tick" and gives both rates;
    `spec-random.json`'s rationale names its real draws (5%x20% entry,
    5%x10% exit). The rationale is metadata, so the id stays.
  - F8: the sell `ORDER PLACED` line in `account.py` moves to INFO.
    `format_price` (in `async_websocket_bot.py`) prints 8 significant digits
    without exponent (`121.48000`, `0.00060612300`); `log_ticker` and
    `log_position` use it.
  - F3: `LOGGING["loggers"]["websockets"]` at INFO (covers
    `websockets.client`; the console already shows only WARNING up). The bot
    keeps the bar of the last ticker line (`int(now / timeframe seconds)`,
    the timeframe from `strategy.warmup()`) and writes the ticker and
    open-position lines only on the first tick of a new bar; orders and
    warnings are logged as before.
- **Done.** As designed. Seen in an isolated `/smoke` of `spec-soak-metronome.json`
  (7 min): the connect log had 24 lines (one ticker line a minute), the serve
  log both fills at INFO and no websocket frames.

#### A2. Paper soak — owner (done 2026-10-05)
The owner's paper soak ("Owner task: a one-week paper soak" in stage B
above), with one `serve paper` and one `connect` per spec. Two runs: the
first from 2026-10-03 23:30, reviewed 12.5 h in, with three specs of which
one traded (sections 1-5, F1-F8); the second on 2026-10-05 on the A1 build,
three SOL specs trading side by side, ended by the owner after about 3.5 h
(section 6, F9-F13). Where the findings went: F1 is A4; F2, F4 and F7 were
fixed in stage B (B7, 2026-10-04, B9); F3, F5 and F8 in A1; F6 is by design;
F9 is A14; F10-F13 are A13. The F8 note on the local `policy.toml` comments
is the owner's. The report as written follows ("this file" is the former
`docs/soak-test.md`; section numbers are its own).

- **Started:** 2026-10-03 23:30 local (UTC+1). **This report:** 2026-10-04,
  about 12.5 h in.
- **Times:** log lines are local time (UTC+1); ledger rows are UTC. "10:49 UTC"
  and "11:49 local" are the same moment.
- **State was not fresh.** The paper ledger goes back to 2026-09-30 and the
  paper wallet is older than that (see §4.6). The random spec's bucket already
  had about 170 round trips from 2026-10-01, and they count toward its
  `max_loss_usd`.

##### 1. Setup

| Process | PID (python) | Started (local) | Spec | Bucket |
|---|---|---|---|---|
| `serve paper` #1 | 11052 | 10-03 23:30:02 | | |
| `serve paper` #2 | 22136 | 10-04 09:33:20 | | |
| `serve paper` #3 (running) | 21440 | 10-04 11:49:01 | | |
| `connect spec-random.json` | 3676 | 10-03 23:30:21 | random, SOL-USDC, 15 s bars, 5 USD legs, budget 20, max loss 10 | `strategy:3f82df8aa389` |
| `connect spec-sol-dip.json` | 10720 | 10-03 23:30:34 | sol-dip, SOL-USDC, 1 min bars, 4 USD, budget 5, max loss 3 | `strategy:8bb94a8356d2` |
| `connect spec-wma-composer.json` | 17812 | 10-03 23:30:59 | wma-composer, NOBODY-USDC, 15 s bars, 5 USD, budget 20, max loss 5 | `strategy:5422d6605a9a` |

Log files are `.logs/trader-<epoch>-<pid>.log`. Before the soak, a
`run paper spec-sol-dip.json` (pid 4844, 23:25-23:29) opened the sol-dip bucket,
and a `serve paper` (pid 20588) lived for 11 s at 23:29:51 with no activity.

###### Timeline

| Local time | Event |
|---|---|
| 10-03 23:30 | Trade-runner #1 up; the three `connect`s say `hello` and start ticking. Paper policy: 60 buys/hour. |
| 10-03 23:56 | wma-composer is warm, 25 min after its start (see F1). |
| 10-04 01:00 | Daily report for 2026-10-03 (`daily_report` event at 00:00:02 UTC, 1 bucket). |
| 10-04 overnight | random runs into the 60 buys/hour limit every hour (§4.7). |
| 10-04 09:33 | Owner restarts the trade-runner with 120 buys/hour. All three `connect`s reconnect. |
| 10-04 11:49 | Owner edits `policy.toml` (11:48:51; now 660 buys/hour) and restarts the trade-runner. All three reconnect; random's open position is restored from the ledger and sold 10 s later. |
| 10-04 12:16 | random reaches `max_loss_usd` (-10.008 USD realized): bucket retired, its `connect` stops by itself (§4.9). sol-dip and wma-composer keep running. |

##### 2. Verdict

**The execution path behaves as designed.** Over about 1,750 fills in the soak
window: no errors in any process, no unresolved intents, every buy is followed
by exactly one sell of the same raw quantity, the paper wallet matches the
ledger to the lamport, policy limits were applied as written, both trade-runner
restarts were absorbed without losing or duplicating an order, the daily
report fired once, and random's `max_loss_usd` retired its bucket and stopped
its `connect` cleanly (§4.9).

**But only one bucket traded.** sol-dip and wma-composer never placed an order.
For sol-dip that is correct (the market never dipped 2%). For wma-composer it
is mostly a poor test subject (a near-flat, thinly traded token), but it
exposed a real warm-up problem (F1). So the multi-bucket paths (two buckets
submitting at once, the order lock, budget allocation under load) were **not**
exercised by this soak.

**Since 12:16 nothing in the soak trades.** random is retired, and the other
two are unlikely to fire. To keep the trade path under load for the rest of the
week, connect at least two specs that trade often on liquid pairs, so two
buckets compete for the order lock and the wallet. Each needs a new
`spec_id`: change a behavior field such as `budget_usd` or `max_loss_usd`.
Changing `name` or `rationale` is not enough (they are metadata, left out of
`spec_id`), so the spec would reattach to the retired bucket.

##### 3. Findings

Ordered by how much they matter. F1-F3 are worth a plan item.

###### F1. Warm-up from candles collapses for thinly traded tokens (wma-composer)

`connect` seeds the indicators from `strategy.warmup()` candles (S7 in
`architecture.md`). The Jupiter 15 s candles for NOBODY only exist for bars
with trades: the last 100 candles span **2 days** (2026-10-02 15:01 ->
10-04 10:29 UTC) with 73 gaps longer than `MAX_GAP_BARS` (5). `BarSeries.seed`
treats each gap as a feed outage and clears the series, so **seeding 100
candles leaves 1 bar** (checked by calling `BarSeries.seed` on the live
candles). The spec then warms up live: 100 bars x 15 s = 25 min, which matches
the log (`5422d6605a9a aquecida` at 23:56:42 for a 23:31:00 start). It happens
again after every `connect` restart.

The backtest has the same blind spot: `backtest --candles 1000` on this spec
replays 1,000 candles spread over 14 days (2026-09-20 -> 10-04) and resets on
almost every gap, so its "no trades" says little.

Options: for candles, fill gaps forward (a candle that doesn't exist means "no
trade", not "no data"), and keep the reset for live tick gaps only; or refuse,
at validation, specs on tokens whose candles are too sparse for the timeframe.

###### F2. A quiet websocket costs a 30 s timeout on every tick (wma-composer)

The price websocket only pushes NOBODY when it trades: 384 pushes in 12.4 h.
`JupiterMarketData.get_price` waits `PRICE_TIMEOUT_SECONDS` (30 s) for the
websocket, falls back to the Price API, and on the next tick waits 30 s again
(a timeout doesn't set `_ws_down_until`, only an exception does). Result: 1,820
ticks in 12.4 h, median gap 30.2 s, and 1,436 `sem preço no websocket`
warnings. Consequences:

- a 15 s timeframe gets at most one tick per two bars; the empty bar is filled
  with the previous close, so the WMAs run on a stair-stepped series;
- the 3.1% trailing stop would be checked only every 30 s on a meme token;
- the `connect` only talks to the trade-runner every 30 s, so it noticed each
  trade-runner restart up to 30 s late (09:33:39 and 11:49:26).

Options: after a timeout, poll the Price API at `REST_POLL_SECONDS` (2 s) for a
while before trying the websocket again (like the exception path), or give the
websocket a much shorter timeout when the Price API is the backup anyway.

###### F3. Log retention is about 2.5 hours for the SOL `connect`s

The SOL `connect`s tick about 9 times a second (one tick per websocket push)
and log at DEBUG: every websocket frame (`websockets.client`), every tick
(`bot.log_ticker`), every `get_price` call and every open-position line. That
is about 10 MB per 25 min per process. With `LOG_MAX_BYTES` 10 MB x
`LOG_BACKUPS` 5, **only the last ~2.5 h survive**: at 11:55 the random and
sol-dip logs started at 09:45 and 09:11, so the night (including the 09:33
restart for random) is gone, and they kept rotating while this report was
written. The serve logs and the wma-composer log are complete.

Options: keep `websockets.client` at INFO in the file handler (frames are 35-50%
of the lines); log the ticker line once per bar instead of once per tick; or
rotate on time with more backups for long runs.

###### F4. The trade-runner doesn't log who is connected

`TradeRunner._hello` and the disconnect in `_handle` log nothing; nor does
`open_bucket` unless a position is restored. From the serve log you can't tell
which specs said `hello`, when a `connect` went away, or which buckets are open.
A WARNING line on `hello` (spec name, bucket, budget) and on disconnect would
make a soak readable from the serve log alone.

**Fix (2026-10-04).** `TradeRunner` logs at WARNING, like its start line (the
console only shows WARNING and up for these loggers):

- `Bucket strategy:<id> aberto para a spec <name>: <symbol>, orçamento <budget>
  USD, perda máxima <max_loss> USD`: once per bucket per process, on its first
  `hello`. What the restore finds is already logged by the service
  (`Posição restaurada do ledger`, and `atingiu o limite` for a bucket that
  opens already past its max loss);
- `Spec <name> (<id>) conectada ao bucket strategy:<id> (<n> conectada(s))`:
  every accepted `hello`, reconnects included;
- `Spec <name> (<id>) desconectada (<n> conectada(s))`: when that connection
  closes, for whatever reason.

A refused `hello` was already logged (`Pedido recusado: HelloError: ...`).
Tested in `tests/trader/api/cli/test_runners.py`
(`test_the_log_shows_bucket_opens_connects_and_disconnects`). A running
trade-runner gets the new lines when it restarts; its `connect`s reconnect on
their own (§4.2).

###### F5. `random_chance` frequency depends on the feed rate

`random_chance` is drawn once per tick, and ticks arrive at the feed's rate: 9.1
per second live for SOL, 1.6 per second in a candle backtest (each bar
replayed as an interpolated path). For spec-random this means about **108 round
trips/hour live (uncapped, 10:49-11:04 UTC) vs 19/hour in the backtest**
(`backtest --candles 1000 --seed 1`: 81 round trips in 4.2 h). `docs/specs.md`
says "`pct`% of the time", which reads as a time rate. Also, the spec's
`rationale` is stale: it says entry draws of 5% and 10% ("~0.5% of ticks"),
but the entry conditions are now 5% and 20%.

Options: say "per tick" in `specs.md` and that live tick rates vary by token
(9/s for SOL, ~0.03/s for NOBODY); fix the rationale.

###### F6. random retires on history from 2026-10-01

Its bucket's realized PnL is rebuilt from every row in the ledger for
`strategy:3f82df8aa389`, including about 170 round trips from 2026-10-01. With
`max_loss_usd` 10 and about -0.011 USD per round trip, it reached the limit
at 12:16 local on 2026-10-04 (§4.9). This is the designed behavior, but it
means:

- the soak's numbers for random are not "since the soak started";
- once retired, the bucket stays retired: a new `connect spec-random.json`
  stops on its first tick (`open_bucket` re-checks the max loss). To keep it
  running, change a behavior field (a new `spec_id`; `name` and `rationale`
  don't count) or delete the paper ledger.

###### F7. Paper and backtest cost models differ by about 4x

Per round trip of 5 USD on SOL-USDC:

| | Gross (price) | Costs | Net |
|---|---|---|---|
| Paper, live (871 round trips) | -0.0097 USD (the 2 x 10 bps paper slippage) | 0.0012 USD (2 x 5,000 lamports) | **-0.0109 USD** |
| Backtest defaults | 30 bps fee + 10 bps slippage per leg | 0.002 USD per leg | **about -0.043 USD** |

Paper uses the real Jupiter quote (LP fees already inside it), so the
backtest's 30 bps fee is conservative on SOL-USDC. Paper also charges only the
base fee: `priority_fee_lamports` is 0 on every row, so paper costs are a lower
bound for real mode. Worth one line in `specs.md`/`architecture.md` so nobody
compares the two PnLs one to one.

**Fixed (2026-10-04, B9 in `plan.md`).** `max_priority_fee_lamports` in
`policy.toml` (default 100,000) is the cap real sends to Jupiter, the fee paper
charges on every leg, and part of the backtest's network fee. `backtest`
measures the pair's fee on Jupiter at the spec's size instead of assuming 30
bps. Every report shows the cost per round trip, computed the same way. With
the defaults, a 5 USD SOL-USDC round trip now reads about 71 bps in the
backtest (0 bps pool, 20 bps slippage, about 51 bps network fees). The cap is
most of that at this size; the owner may want a lower cap for small trades.
On the soak's ledger, the old paper trades read 21.9 bps.

###### F8. Small things

- `account.py` logs a buy fill at INFO and a sell fill at DEBUG (`ORDER
  PLACED`); the serve log only shows half the fills at INFO.
- `log_ticker` prints prices with 9 decimals: NOBODY (0.0006) shows 6
  significant digits.
- The local `policy.toml` (untracked) still has comments from an older example
  (`main.py swap`, `main.py resume`, `[dry.*]`, `ledger list`).
  `policy.example.toml` is already clean.
- A policy change needs a trade-runner restart. That worked well here (§4.2),
  so it is fine as long as it stays documented.

##### 4. Checks and results

###### 4.1 Processes and errors

| Process | Log span checked | ERROR | WARNING (what) |
|---|---|---|---|
| serve #1 | 23:30:02 -> 09:32:57 | 0 | 397 policy denials (60/h) |
| serve #2 | 09:33:20 -> 11:48:20 | 0 | 7 policy denials (120/h) |
| serve #3 | 11:49:01 -> now | 0 | none besides start and the restored position |
| connect random | 09:45 -> now (older lines rotated out) | 0 | 7 denials, 1 reconnect |
| connect sol-dip | 09:11 -> now (older lines rotated out) | 0 | 1-2 reconnects |
| connect wma-composer | whole run | 0 | 1,436 Price API fallbacks (F2), 2 reconnects |

No `Erro no loop principal`, no `Pedido recusado` (protocol errors), no
`Varredura ... falhou`, no websocket fallbacks for SOL in the logs that remain.
The largest gap between SOL ticks was 6.1 s (during order handling); the 3.3-3.5 s
gaps at the trade-runner restarts were the reconnects.

###### 4.2 Trade-runner restarts

At each restart the trade-runner removed `.data/trader-paper.json` on exit, so
a `connect`'s first retry said `trade-runners encontrados: nenhum`; the next
one (2 s later) re-read the file, got the new port and token, sent `hello` again
and carried on. Downtime seen by the SOL `connect`s was about 3 s.

At the 11:49 restart random had a position open (buy 10:48:20 UTC). The new
trade-runner restored it from the ledger (`Posição restaurada do ledger:
0.041157438 @ 121.48`), and the strategy, which never restarted, sold it at
10:49:11 UTC for the same raw quantity. No intent was left EXECUTING,
no idempotency key was reused, and the `reconcile` at the first `hello` found
no mismatch.

###### 4.3 Market data

| Spec | Ticks | Rate | Source |
|---|---|---|---|
| random (SOL) | 70,896 in 2.15 h | 9.15/s, median gap 0.03 s | websocket |
| sol-dip (SOL) | 89,811 in 2.73 h | 9.14/s, median gap 0.04 s | websocket |
| wma-composer (NOBODY) | 1,820 in 12.4 h | 0.04/s, median gap 30.2 s | Price API after a 30 s websocket timeout (F2) |

###### 4.4 Strategy behavior

- **random:** behaves as specified. Every buy is 5 USD; it alternates strictly
  buy -> sell; the median hold is 15 s (p95 69 s); the median wait from an exit
  to the next entry is 8.6 s; the 50% stop never came close. Win rate 0.1% (1 of
  871): a 15 s hold rarely moves SOL more than the 20 bps the paper slippage
  takes. Signal to fill over the 494 fills still in the log: median 0.20 s,
  p95 0.60 s, max 3.9 s (strategy log `_signal` -> `bot.log_placed_order`,
  minus the bot's fixed 2 s pause after a fill).
- **sol-dip:** no trades, which is **correct**. In the soak window SOL moved
  between 119.51 and 121.52; the largest dip from the 30-bar high was 0.43%
  (needs 2%), and the lowest RSI14 (21.5, 23:07 UTC) came with a 0.25% dip;
  no bar had even a 1% dip with RSI14 under 45. A
  backtest of the same window (`backtest spec-sol-dip.json --candles 1000`,
  2026-10-03 19:18 -> 10-04 11:56) also makes 0 trades.
- **wma-composer:** no trades, **consistent with its input**. Its 1,828
  logged ticks (rebuilt from the `log_ticker` lines into a ticks CSV) replayed
  with `backtest --ticks` also make 0 trades. NOBODY had only 36 distinct
  prices in 12.5 h (0.000606-0.000627), so the four WMA conditions had almost
  nothing to work with. See F1 and F2.

###### 4.5 Ledger integrity

Over all 2,068+ executed rows of `strategy:3f82df8aa389` (and the 6 of the
retired `strategy:555d793e2f18`, an older spec from 2026-09-30):

- buys and sells strictly alternate; each sell's `in_amount` equals the open
  buy's `out_amount`, and has `closes_position = 1`;
- `intent_executing` = `intent_executed` = `order_recorded` events (2,061 when
  counted), and `ledger_dump.py` shows no unresolved intents;
- no `reconcile_mismatch`, `failed_tx_fee`, `bucket_retired` or
  `bucket_max_loss` events before the max loss (§4.9);
- denied buys are folded into `repeat_count`, as designed: 113 denied rows
  stand for 486 denied requests.

###### 4.6 Paper wallet vs ledger

`wallet - (sum of executed flows - fees - rent)` should be constant. Sampled 6
times, 20 s apart, across 5 new fills: always **USDC 72.036903, SOL
0.706204384** to the raw unit. Every change to `paper-wallet.json` is in the
ledger. (That "starting" balance isn't the 100 USDC + 0.5 SOL default: the
wallet predates this ledger.)

###### 4.7 Policy

`max_trades_per_hour` counts buys only, as `policy.toml` describes:

| UTC hours | Limit | Executed buys/hour | Denied requests/hour |
|---|---|---|---|
| 23 -> 07 | 60 | 54-60 | 36-48 |
| 08 (restart at 08:33) | 60 -> 120 | 67 | 29 |
| 09 | 120 | 120 | 6 |
| 10 (uncapped from 10:49) | 120 -> 660 | 105 | 1 |

After a denial the `connect` pauses orders for 30 s (`denial_cooldown`) while
still taking prices; the longest idle gaps (about 23 min) are the hourly limit.
Sells were never denied.

###### 4.8 Daily report

`daily_report` for 2026-10-03 at 00:00:02 UTC, 1 bucket (only random had
fills), from trade-runner #1. Neither restart sent it again.

###### 4.9 Max-loss retirement (random)

Observed live on 2026-10-04, and it worked end to end:

| Local time | Where | What |
|---|---|---|
| 12:16:06.427 | ledger | sell executed, realized -0.0113 USD; bucket total -10.0079 |
| 12:16:06.557 | serve | `_check_max_loss`: `prejuízo -10.0079... atingiu o limite 10`; bucket RETIRING |
| 12:16:06.560 | ledger | `bucket_max_loss` event (`limit` 10, `realized_usd` -10.0079) |
| 12:16:08.607 | connect random | next tick (after the 2 s post-fill pause): `bucket_done` -> `encerrado: parando o bot` |
| 12:16:18.626 | connect random | websocket closed; the whole `uv` -> `python` process tree exited, no error |

The check runs after the fill, so the bucket overshoots the limit by the last
round trip's loss (0.008 USD here). No position was open, so there was nothing
for `_close_if_retiring` or the 30 s sweep to sell. The trade-runner kept
serving the other two `connect`s with no warnings. Closing the websocket took
10 s (the server didn't answer the close frame; `websockets`' default
`close_timeout`), which is harmless.

Uncapped (10:49-11:16 UTC) random made 97 fills in 27 min, about 108 round
trips/hour, costing about 1.2 USD/hour of paper slippage and fees.

###### 4.10 Resources

At 11:59 local, after 12.5 h:

| Process | Private memory | CPU time |
|---|---|---|
| connect random | 59.5 MB | 2,079 s (4.6% of a core) |
| connect sol-dip | 58.9 MB | 1,771 s (3.9%) |
| connect wma-composer | 61.3 MB | 39 s |
| serve #3 (10 min old) | 58.5 MB | 17 s (2.8%) |

At 12:16, private memory was unchanged (sol-dip 58.9, wma-composer 61.3,
serve 58.9 MB). Two samples are not a leak test; sample again at the end of
the week.

The ledger is 13.5 MB (+1 MB WAL) for about 2,200 intents and 6,300 events.

##### 5. How these checks were made

All read-only; nothing was written to `.data/`.

- `uv run --no-sync python .claude/scripts/ledger_dump.py paper`.
- SQL on `.data/ledger-paper.sqlite3` opened with `?mode=ro`: intents by
  account/side/status and hour, denial reasons, event counts, buy/sell
  pairing, and the wallet invariant (§4.6).
- Log scan per process (all rotated files, oldest first): levels, distinct
  WARNING/ERROR messages with numbers masked, tick gaps from `bot.log_ticker`.
- `backtest` on sol-dip (`--candles 1000`), random (`--candles 1000 --seed
  1`) and wma-composer (`--candles 1000` and `--ticks` rebuilt from its log).
- `JupiterMarketData.get_candles` to measure dips/RSI over the window and the
  NOBODY candle gaps, and `BarSeries.seed` on those candles.
- `Get-Process` for memory and CPU.

`connect` has no `--record-ticks` (only `run` does), so the wma-composer replay
had to rebuild ticks from the log, at the 9 decimals `log_ticker` prints.
Giving `connect` the same option would let
`.claude/scripts/live_vs_backtest.py` replay exactly what each strategy saw.
(Done in B14: `run` is gone and `connect` has `--record-ticks`.)

##### 6. Second run: three SOL buckets on the current build (2026-10-05)

The restart A2 asked for: the A1 build, a fresh paper ledger, and two liquid
specs that trade often (`spec-soak-metronome.json`, `spec-soak-revert.json`)
next to `spec-random.json`, so several buckets share one trade-runner. The
owner ended it after about 3.5 h ("good enough"), so there is no week-long
run and no end-of-week memory sample. Reviewed at 18:25 local the same day.
Times are local (UTC+1) unless marked UTC.

###### 6.1 Setup and timeline

The paper ledger was deleted at about 14:41 (its first row is a
`daily_report` at 13:41:59 UTC); the paper wallet was kept. Log files are
`.logs/trader-<epoch>-<pid>.log`.

| Local time | Event |
|---|---|
| 14:39 | On the old ledger, `connect spec-random.json` stops on its first tick (bucket retired on 10-04, F6). |
| 14:41:59 | `serve paper` (pid 20496, a build before A1's log changes) on the new ledger; `connect spec-random.json` (41632). |
| 15:50:52 | Trade-runner restarted (pid 38976, A1 build). |
| 15:51:12 | `connect spec-soak-metronome.json` (23576): bucket `strategy:733e9d719fd6`. |
| 15:52:02 | `connect spec-soak-revert.json` (31096): bucket `strategy:e104dba8efcd`. |
| 15:53:01 | random's `connect` restarted (34164) on the A1 build: three buckets, one trade-runner, for 2 h 22 min. |
| 18:15:34 | Trade-runner stopped to take the A3 build (committed 18:13). |
| 18:15:38 | A3 trade-runner (36880); the three running `connect`s (A1 code) reconnect by themselves within 8 s. 18:15:54: random buys. |
| 18:16:05 | Trade-runner restarted again (43000, still running). It restores random's open position from the ledger. |
| 18:16:18-18:16:40 | random's `connect` stopped and started four times (41620, 34516, 27268, 31452). 27268 sells at 18:16:37 and is stopped 2 s later. |
| 18:17:21 | revert's `connect` restarted (25444) on the A3 code. |
| 18:21:53-18:22:17 | The owner stops every `connect`. random had bought at 18:22:05, so its bucket holds 0.041730159 SOL; the next `connect spec-random.json` restores it. |

###### 6.2 Verdict

**Clean again, now with three buckets.** 121 intents, all EXECUTED: random
45 buys and 44 sells, metronome 8 and 8, revert 8 and 8. No denials, no
failed or rejected rows, no unresolved intents, no `reconcile_mismatch`, no
ERROR in any trade-runner log. Four trade-runner starts and six `connect`
restarts lost or duplicated no order, including a position bought under one
trade-runner and sold under the next, and a sell whose `connect` was stopped
while the fill was being reported (§6.4). Both soak specs behaved as written,
at the rate their rationale promised.

**What it could not show:** the order lock was never contended (F12), and two
things only show up when comparing with a backtest or at restarts: the replay
places stops and targets about 25 bps tighter than live at 5 USD (F9), and a
fresh trade-runner answers a burst of first price requests with
`StalePriceError` (F10). These and the two small ones (F11, F13) are plan
items A13 and A14.

###### 6.3 Findings

###### F9. Replays measure stops and targets from a fill that includes the network fee

`SpecStrategy` measures every position-relative exit from `entry_price`, the
entry order's `quote_price` (the fill), and starts the trailing stop's peak
there. The replay folds `network_fee_usd` into the fill
(`ReplayQuoteClient.get_quote` takes it from the output, since the replay
wallet holds no SOL); paper and real pay it in SOL, outside the fill. At 5 USD
legs the fee is 0.0126 USD, 25 bps, so the replay's entry sits about 35 bps
above the tick (10 bps slippage plus the fee) where live sits about 9 bps
above (tick 119.58792 at 16:16:00, BUY at 119.69521). Every exit measured from
the entry is that much tighter in the replay: revert's 0.5% trailing stop
fires 0.15% below the entry tick in the backtest and about 0.41% below live;
`stop_loss`, `take_profit`, `trailing_take_profit` and `entry_price` in an
`expr` shift the same way. The effect is the fee over the trade size (2.5 bps
at 50 USD).

Seen on revert: `backtest --candles 1000` over the same afternoon enters
where live entered (16:15/16:16, 16:31/16:31, 17:24/17:24, 17:43/17:44,
18:02/18:02 for backtest/live), but in the live window 5 of its 10 exits are
trailing stops; live had 0 of 8 (7 `max_hold`, 1 `take_profit`). Costs agree
(70.3 bps per round trip in the backtest, 64.6-69.6 bps live), so only the
exits drift. Plan item A14.

###### F10. A fresh trade-runner refuses the second first-price request for a mint

`PriceHub._watch` adds a new mint to the watch list and then polls the Price
API for its first price. A second `price` request for the same mint during
that poll sees the mint as known, finds no price and gets `StalePriceError`.
It happened at both restarts that had several `connect`s waiting (18:15:42
and 18:16:07: `Pedido recusado: StalePriceError: sem preço de So111...`).
The `connect` logs it as ERROR with a traceback (`Erro no loop principal`),
backs off one step and is fine on the next tick. Harmless, but it is the only
ERROR of the run and every multi-spec restart will show it. Plan item A13.

###### F11. A fill is reported 2 s late, and not at all if the `connect` stops

`AsyncWebsocketTradingBot._handle_reply` sleeps 2 s after a fill ("da tempo da
wallet atualizar", marked TODO) before returning the order, and only then is
it logged (`log_placed_order`) and sent to Telegram. The wallet is the
trade-runner's now, and the reply already comes after the ledger. At 18:16:37
random's `connect` signalled a sell, the trade-runner filled it 0.28 s later,
and the `connect` was stopped at +1.95 s: the ledger has the sell, the
`connect` log and Telegram never saw it. Plan item A13 (report first; whether
the pause stays goes with A5).

###### F12. The order lock was never contended

Paper intents take 0.19 s (median; max 0.46 s) from creation to the last
update, and the three buckets placed about 20 orders an hour between them:
no two intents of different buckets overlapped, and the closest pair started
0.73 s apart (16:39:29: random's buy ended at .247, revert's sell started at
.978). A soak at this rate can't exercise the lock; a test can (several
buckets submitting at once on one `TradeService`). Plan item A13.

###### F13. Backtest times are local, ledger times are UTC

Jupiter candles are parsed as naive local time (`candles_to_tickers`), so
`backtest --json` prints `start`, `end` and trade times in local time with no
offset, while the ledger is UTC. Matching F9's trades by eye needed the one-hour
shift. `live_vs_backtest.py` already normalizes. Plan item A13 (print UTC).

###### 6.4 Checks and results

**Processes and logs.** A1's F3 fix holds: the A1 trade-runner wrote 250 KB in
2 h 25 min (pid 20496, before the fix, wrote 8.8 MB in 69 min, mostly
websocket frames); the `connect` logs are 47-194 KB and nothing rotated.
Warnings are only starts, `hello`s, disconnects and reconnects (F4's lines
made the timeline readable from the trade-runner log alone). The only ERRORs
are F10's two lines in the revert and random `connect`s.

**The two soak specs** (2 h 22 min side by side):

| | Round trips | Hold | Wait from exit to entry | Exits | Rate live / backtest |
|---|---|---|---|---|---|
| metronome | 8 | 300.5-301.2 s | 736-951 s (cooldown 720 s + draw) | 8 `max_hold5m` | 3.3/h / 2.95/h (49 in 16.6 h, `--seed 1`) |
| revert | 8 | 432-481 s | 407-965 s (cooldown 360 s) | 7 `max_hold8m`, 1 `take_profit0.15%` | 3.3/h / 3.6/h (15 in 4.2 h) |

random made 44 round trips in about 3.6 h (median hold 143 s, shortest 2.2 s).
Its 1% chance per tick at one tick a second matches.

**Ledger.** Per bucket, buys and sells alternate; each sell's `in_amount` is
the open buy's `out_amount` and has `closes_position = 1`; no idempotency
key repeats; every sell has `pnl_complete = 1`. Every intent has
`intent_executing` -> `intent_executed` -> `order_recorded`, and the 7
created by the A3 build also an `intent_sent` before the execution. Other
events: three `bucket_opened` and one `daily_report` (for 10-04, with 0
buckets: the ledger was new). Costs are `simulated` on every leg: 105,000
lamports (5,000 base plus the 100,000 priority cap), no rent.

| Bucket | Net PnL | Cost per round trip |
|---|---|---|
| random | -1.5426 USD (44) | 69.3 bps |
| soak-metronome | -0.2915 USD (8) | 69.6 bps |
| soak-revert | -0.2383 USD (8) | 64.6 bps |

At about 0.035 USD a round trip and ~3.3 round trips an hour, each soak
spec's 15 USD max loss would have lasted about 5.4 days, as planned.

**Paper wallet.** The 7 swaps in A3's applied log match their ledger rows to
the raw unit (in, out, fee, rent). Balances at the end: 57.306262 USDC,
0.722330263 SOL; with this ledger's flows taken out, the wallet started it
with 62.870936 USDC and 0.693305104 SOL (the wallet predates the ledger, as
in §4.6). No fills happened while it was sampled, so the invariant wasn't
re-checked over time.

**Restarts.** At 18:15 the three A1 `connect`s reconnected to the A3
trade-runner with no changes (the protocol didn't change). The position random
bought under pid 36880 was restored by pid 43000 (`Posição restaurada do
ledger: 0.041750099 @ 119.76`), shown by the next `connect`s (`LONG
0.04175010 @ 119.76020`) and sold once at 18:16:37. The `connect` started
after that sell began flat, with the sell in its PnL (-1.4410 -> -1.4736).

**Wallet allocation.** Open budgets 20 + 15 + 15 = 50 USD fit the wallet at
every `hello`; no refusal.

**Resources.** The A3 trade-runner used 62.5 MB private memory and 9.6 s of
CPU in its first 7 minutes (58.5 MB for the 10-04 one at 10 minutes). The
long-run sample was not taken.

###### 6.5 How these checks were made

As in §5, read-only: `ledger_dump.py paper`; SQL on the ledger with
`?mode=ro` (pairing, event sequences, overlaps between buckets, costs,
the wallet's applied log against the rows); a scan of each log by level and
distinct message; `backtest --candles 1000 --json` of both soak specs
(metronome with `--seed 1 --fee-bps 0 --network-fee-usd 0.012579` after a
Jupiter 429), and a replay of revert with the strategy logger on to get each
exit's label; `Get-Process` for memory.

#### A3. Resolve UNCONFIRMED intents — M (done 2026-10-05)
Today a process killed mid-swap leaves an UNCONFIRMED (or EXECUTING) intent
that blocks the mode until the ledger file is moved or deleted. At `serve`
start, for each one: read its signature's status at Confirmed; success ->
EXECUTED with its fill and costs; failed on-chain -> FAILED with the fee
booked; unknown with the blockhash expired -> FAILED (nothing moved);
otherwise it keeps blocking and the log says what the owner must check. Every
resolution is a ledger event. First check whether the signature is stored
before the send (`ledger/intents.py` records it only on the error path); an
intent without one stays blocking. Decide the paper behaviour in the design.
- **Design.** No schema bump: everything new is an event.
  - **Every send is logged before it happens.** The signature is known once
    the transaction is signed. `TradeGateway._execute` sets a send hook (a
    `ContextVar`, like `botname`) for the duration of `execute()`; the
    executor calls it with a `SentTx` (signature, `last_valid_block_height`
    of the blockhash, mints, quoted in/out) right before `send_transaction`.
    The hook writes an `intent_sent` event and the `signature` column in one
    transaction; if that write fails the send doesn't happen (a pre-broadcast
    failure). Retries log one `intent_sent` per attempt. The
    `intent_executing` event gets `"send_log": true`, so an intent created
    by this build with no `intent_sent` was never sent.
  - **Outcome of one send** (`Executor.outcome(sent) -> TxOutcome`): on
    chain, `getSignatureStatuses` with the history search at Confirmed:
    `err` -> FAILED; confirmed/finalized -> LANDED; not found and the
    finalized block height past `last_valid_block_height` -> EXPIRED (it can
    never land); anything else, or an RPC error -> PENDING. Paper: the
    simulated wallet keeps a log of applied swaps (signature, amounts, fee,
    rent; the last 200, in `paper-wallet.json`, an old file reads as empty);
    applied -> LANDED, otherwise EXPIRED (a paper swap is applied in the same
    step, so one that isn't applied never will be).
  - **Resolving an intent** (`trader/execution/trade/gateway/resolve.py`),
    for every EXECUTING or UNCONFIRMED intent:
    - one send LANDED -> `mark_executed` with that signature, costs from
      `fetch_swap_costs`, the order from `order_from_fill` (USD from the
      oracle at resolve time; the trade's own snapshot is gone), and for a
      sell the PnL against the open entry (computed before the sell is
      marked, with the same keep-the-rest rule as `AsyncAccount.sell`, now
      one function in `book.py`); `record_fill` as usual;
    - all sends FAILED or EXPIRED -> FAILED, and the fee of each FAILED
      send not yet in a `failed_tx_fee` event is booked;
    - no send and `send_log` -> FAILED (never sent: interrupted before the
      broadcast, e.g. Ctrl+C during the quote);
    - otherwise (a send PENDING, or an intent from an older build with no
      send log) it keeps blocking, and the log says which signature to check
      (once per intent per process).
    Each resolution writes an `intent_resolved` event (outcome, signature,
    reason).
  - **When.** `TradeService.resolve_intents()` runs under the order lock (so
    nothing of this process is in flight, and the OS lock keeps other
    `serve`s of the mode out): once when `serve` starts, before it accepts
    connections, and on every sweep while anything is active. Open buckets of
    the touched accounts are restored from the ledger and the balance cache
    is dropped, so a PENDING send that lands later doesn't need a restart.
  - The policy's unresolved message says the trade-runner retries by itself
    and what to check if it can't.
- **Done.** As designed. The signature is now stored before the send (it used
  to be only on the error path). `sign_transaction` returns a `SignedTx` with
  the blockhash's `last_valid_block_height`; `AsyncAccount.sell` and the
  resolver share `PositionBook.settle_sell` and, in `fills.py`, `settle`
  (costs and failed fees after EXECUTED), `record_fill_safely` and
  `record_leftover`; `TradeIntent.pair()` gives the (quote, token) mints. The
  on-chain executor refuses to send when no send hook is set
  (`announce_send(required=True)`). Paper keeps the last 200 applied swaps
  and the time of the last one trimmed (`trimmed_at`): a send missing from
  the log but sent before that time is PENDING, never EXPIRED. A sweep that
  resolves a losing sell re-checks the bucket's `max_loss_usd`.
  A resolution counts toward the circuit breaker like any FAILED row (it
  re-arms at the next restart). Checked in paper by forging the last buy of a
  `/smoke` run back to UNCONFIRMED without its order: the next `serve paper`
  resolved it at start, with the wallet's actual fill. The on-chain path is
  covered by unit tests only (no live RPC in agent sessions); its first real
  use is the owner's.

#### A13. Soak II small fixes — S (done 2026-10-05)
From the second soak run (A2 in `history.md`, F10-F13). No schema or spec id
change.
- F10: `PriceHub` keeps the first poll of a new mint in flight and every
  `price` request for that mint awaits it, so `connect`s that say `hello`
  together after a restart don't get `StalePriceError`. The bot logs a
  stale-price reply as a WARNING without a traceback (it is not a bug in the
  loop).
- F11: the bot logs the fill and sends the Telegram message before the 2 s
  pause, so a `connect` stopped right after a fill still reports it.
- F12: a test where several buckets submit at once (`asyncio.gather`) on one
  `TradeService`: orders run one at a time, every bucket stays within its
  budget, and the wallet never goes negative.
- F13: `backtest` prints `start`, `end` and trade times in UTC, like the
  ledger.
- **Design.**
  - F10, hub: `PriceHub._watch` keeps one `asyncio.Task` per mint whose first
    poll is in flight (`_first_polls`); a request for a mint that is watched
    but still has no price awaits that task before reading. A failed first
    poll still ends in `StalePriceError` (no price, no decision). The
    `usd_prices` path goes through `_watch` too, so it gets the same fix.
  - F10, bot: a refused answer carries the exception's name (`"kind"`,
    added next to `"error"` in `TradeRunner._answer`; old clients ignore
    it). `RemoteTradeClient._call` raises `PriceUnavailableError` (a
    `TradeServiceError` in `shared/trading_service/protocol.py`) for kind
    `StalePriceError`, and the bot's `_on_error` logs that one as a WARNING
    without a traceback; the backoff stays.
  - F11: the 2 s pause moves out of `_handle_reply` to `_tick`, after
    `_report_order`: same pause, but the fill is logged and notified first.
  - F12: `test_trade_service.py` gets a test where three buckets submit
    buys and sells at once with `asyncio.gather`; the quote client yields
    to the loop and counts calls in flight, which must never pass one, and
    no bucket spends past its budget or the wallet.
  - F13: `Backtester.run` stores `start`, `end` and trade times through
    `to_utc` (candle ticks are naive local time, tick files are already
    UTC), so `--json` prints `+00:00`; the readable summary says
    `período (UTC)`.
- **Done.** As designed. The hub test holds the first poll open and checks
  that a second `price` and a `usd_prices` wait for it (one API call), and
  that a waiter that gives up doesn't cancel it. The concurrency test fails
  without the order lock (three quotes in flight at once). An isolated
  `/smoke` of `spec-random.json` logged its buy in the same second as the
  signal (2 s later before), with no ERROR.

#### A14. Replay fills like live fills — M (done 2026-10-05)
Soak F9: the replay takes `network_fee_usd` out of the swap output, so a
replay fill is fee/size worse than a live one (25 bps at 5 USD), and every
exit measured from `entry_price` (stops, targets, trailing peaks, `expr`)
fires that much earlier than live. In paper and real the fee is paid in SOL,
outside the fill. Charge the replay's network fee outside the fill as well
(the leg's cost, off the bucket's PnL and equity), so a replay entry is the
tick plus slippage and pool fee, as live. Done when `round_trip_costs` and
the realized PnL of a spec without position-relative exits are unchanged,
the revert soak spec's exits on the soak afternoon look like live (mostly
`max_hold`), and the headline change of each `docs/examples/` spec is
recorded here. Before A6: a first real run uses a tiny budget, where the gap
is largest.
- **Design.** Only `trader/backtest/` changes; live paths, the ledger schema
  and spec ids stay.
  - `ReplayQuoteClient` prices a leg at the tick minus `fee_bps +
    slippage_bps` only: no network fee in the output, so the fill (and
    `entry_price`) is the tick plus slippage and pool fee, as in paper.
  - A `ReplayExecutor` (a `SimulatedExecutor` with no SOL fees, rent,
    slippage or SOL reserve) reports each leg's network fee as
    `fee_lamports`: `network_fee_usd` at the replay's SOL price. The
    ledger then turns it into the leg's cost exactly as live (`costs_sol`,
    `costs_usd`, net PnL, the bucket's realized PnL and max loss, round-trip
    costs). The `Backtester` builds the provider with it directly instead of
    `paper_provider`.
  - The replay's SOL price comes from `ReplayPrices`, which now also answers
    for SOL: the token's USD price when the token is SOL, the quote's when the
    quote is SOL, and otherwise a fixed reference (`REPLAY_SOL_USD`); the fee
    is a USD amount and the lamports only carry it, so the reference only
    changes the SOL figure shown, never a USD value. On a SOL leg the ledger
    converts at the fill price, so the USD cost differs from
    `network_fee_usd` by the slippage share (0.1% of the fee).
  - The replay wallet holds no SOL, so the fee isn't taken from it: the
    executor adds each fee to `fees_usd`, and `_equity` subtracts it. What
    the bucket may spend still shrinks with the fee, through the realized PnL
    under the budget cap.
  - Acceptance as above; "unchanged" is within the fee's share of the buy:
    a buy no longer gives up the fee's worth of tokens, so the position is
    that much bigger, as live.
- **Done.** As designed, in `trader/backtest/replay.py` (no new module).
  Tests: a 1% stop no longer fires on a 0.6% dip when the fee is 5% of the
  leg (it did before), and the fee tests check the fill at the tick, the
  equity and realized PnL net of both fees, and the round-trip cost.
  Headlines of `docs/examples/` on the same frozen ticks (1000 candles taken
  2026-10-05 ~19:20 UTC, `--fee-bps 0 --network-fee-usd 0.012579 --seed 1`),
  before -> after:

  | Spec | Closed | Return | Realized PnL | Exits |
  |---|---|---|---|---|
  | random | 83 -> 83 | -14.713% -> -14.718% | -2.9425 -> -2.9436 | chance 83 -> 83 |
  | scalp-test | 3 -> 3 | -0.526% -> -0.526% | -0.1052 -> -0.1053 | stop_loss 3 -> 3 |
  | soak-metronome | 49 -> 49 | -11.124% -> -11.127% | -1.6686 -> -1.6691 | max_hold 49 -> 49 |
  | soak-revert | 13 -> 13 | -2.903% -> -2.902% | -0.4355 -> -0.4353 | trailing 2 -> 1, take_profit 0 -> 1, max_hold 11 -> 11 |
  | jup-sol-expr, sol-dip, target-value, wma-composer | 0 -> 0 | unchanged | 0 | none |

  Round-trip cost stays 70.3 bps everywhere. The specs without exits measured
  from the entry move by the fee's share of the buy (the position is that
  much bigger now, as live). On the part of the soak afternoon still in the
  candles (16:14-17:10 UTC), revert's replay now exits like live
  (`max_hold`, `take_profit`, `max_hold`, `max_hold`; before: a trailing
  stop at 16:14 and 17:02), and over the whole 4 h window 11 `max_hold` and
  1 `take_profit`, no trailing stop (the soak's pre-A14 run: 7 trailing stops
  of 15).

#### A5. One rule for when orders re-read the wallet — S (done 2026-10-05)
Old B10 C3. It touches real sells, so it is settled before A6. Decided with
the owner on 2026-10-05:
- **Every order reads the wallet fresh** (`WalletBalances.fresh`), inside the
  service's order lock: buys with or without a bucket cap, and sells (one read
  per sell, where `can_sell` and the cap used to read twice). The cache serves
  only reads that are not orders (allocation, reconcile, snapshots), and
  anything that moves the wallet (a fill, the A3 resolver) still invalidates
  it. Replaces the four special cases (cap-only re-read on buys, sell
  re-read, ...).
- **The bot's 2 s pause after a fill goes** (`POST_FILL_PAUSE_SECONDS`,
  "da tempo da wallet atualizar"): the wallet is the trade-runner's, the reply
  comes after the ledger, and the next order reads the wallet fresh anyway.
- **A sell the wallet can't cover is refused and alerted**, no longer capped
  at the balance. If the spendable balance of the token (for SOL, after the fee
  reserve) is below the quantity to sell, `AsyncAccount.sell` raises
  `WalletShortfallError` (a `ValueError`: the reply is a rejection, nothing is
  sent). `TradeService` blocks buys of that token (as `reconcile_mismatch`),
  and once per open position writes a `sell_shortfall` event, logs an ERROR
  and sends a Telegram message from `serve` (`TradeService(notifier=)`,
  the notifier `serve` already builds for the daily report). The owner checks
  the wallet; the strategy keeps asking and the sell goes through once the
  tokens are back. `settle_sell(may_keep_rest=False)` stays for intents
  resolved from older ledgers.

Tests: a buy without a cap reads fresh; a shortfall sell is refused without a
swap, blocks buys, writes one event and one message for repeated attempts;
the old capped-sell test becomes the refusal.
- **Done.** As designed. `WalletBalances.fresh`; `AsyncAccount` reads once per
  order (`_sellable` takes the balance; `can_sell` is gone, and so is the
  live `record_leftover` call, which only a wallet-capped sell could reach);
  `WalletShortfallError` in `gateway/account.py`; `TradeService._sell` /
  `_shortfall`; `build_trade_service(notifier=)`, and `serve` shares one
  notifier between the service and the daily report. 781 tests (3 new, 3
  rewritten: the capped sell, the fee-reserve rest that used to close as a
  `position_leftover`, and the pause test). Consequence for SOL-output pairs
  (`SOL-USDC`): the 0.02 SOL fee reserve is not sellable, so the wallet needs
  SOL beyond the reserve plus the position, or the sell is refused.

#### A4. Warm-up across candle gaps — M (done 2026-10-05)
Soak F1: Jupiter candles only exist for bars with trades, and
`BarSeries.seed` treats every gap over `MAX_GAP_BARS` as an outage and clears
the series, so a thin token warms up live (25 min for wma-composer) and its
backtests reset all the time. A missing candle means "no trade": fill it
forward when seeding and in replays; the reset stays for gaps in live ticks.
The backtest headline of the liquid examples must not change.
- **Design.**
  - `BarSeries.seed` fills every gap between candles with the previous close
    (at most `maxlen` bars: a longer gap is a flat window anyway) and never
    clears. The **first `update` after a seed** fills the same way: the gap
    from the last candle (the last trade) to the first live tick is also "no
    trade", and for a thin token it is often hours. Every later `update` keeps
    the `MAX_GAP_BARS` reset (a live tick gap is an outage).
  - `ticks_from_candles` (the `--candles` replay) emits one flat tick at the
    previous close at the start of every bar with no candle, carrying the
    previous `quote_usd`, so the replay's `BarSeries` never sees a gap and the
    strategy is asked once per quiet bar, as live (where the hub keeps
    sending the unchanged price). `bars_in` then counts the quiet bars too, so
    the warm-up + 20 bars check measures time, not trades. `--ticks` replays
    are recorded live ticks: unchanged, their gaps still reset.
  - Not changed: `ratio_candles` still keeps only the bars where both series
    have a candle (a missing bar there is then filled forward like any other);
    the live feed's reset.
  - Checked by: `BarSeries` and `ticks_from_candles` unit tests (gap filled,
    cap at `maxlen`, first live tick after a seed fills, the next gap resets,
    `quote_usd` carried); a before/after replay of every `docs/examples/` spec
    on one saved set of candles (liquid headlines identical; wma-composer warms
    up and trades).
- **Done.** In `trader/shared/indicators.py` (`BarSeries`, `bar_index`, now
  also used by `bars_in` and `compare.py`) and `trader/backtest/ticks.py`
  (`ticks_from_candles`, `_quiet_bars`). One change from the design (after
  `/simplify`): instead of a one-shot "first `update` after a seed" flag,
  `seed(candles, until=)` fills explicitly up to `until`, whose bar starts at
  the last close; `SpecStrategy.setup` passes its clock (now live, the first
  tick in a replay). Tests: `TestSeedAcrossQuietBars` (fill, cap at
  `maxlen`, `until`, the later live reset, no `until` keeps the reset), quiet
  bars and their `quote_usd`
  in `TestCandlePaths`, and soak F1 itself (sparse candles plus a first tick
  an hour later leave the spec warm); all fail on the old code. Old and new
  code back to back on one saved set of 1,000 candles per example
  (2026-10-05 ~22:30 UTC, `fee_bps` 30, slippage 10, network fee 0.002):

  | Spec | Bars | Closed | Return |
  |---|---|---|---|
  | random, scalp-test, soak-metronome, soak-revert, sol-dip, target-value | unchanged | unchanged | unchanged |
  | jup-sol-expr | 995 -> 1000 (5 quiet JUP bars) | 0 -> 0 | unchanged |
  | wma-composer (NOBODY, 15 s) | 1000 -> 88,746 (14 days) | 0 -> 32 | 0% -> -0.63% |

  The wma-composer replay takes ~15 s (99k ticks). `/smoke --spec
  spec-wma-composer.json`: `aquecida` on the first tick (the soak: 25 min).
  Left as is: `ratio_candles` keeps only bars with a candle in both series.

#### A15. First real run fixes — M (done 2026-10-06)
From A6 run 1. No schema change: the refund is an event, like
`failed_tx_fee`.

- **Sell re-read (F1).** When the fresh read has less of the token than the
  sell needs, `AsyncAccount.sell` asks the provider for a direct read
  (`token_balance(mint)`): real reads the wallet's associated token accounts
  of the mint by address (`getMultipleAccounts`, Token and Token-2022: an
  account read, not the owner index), paper the simulated wallet. The larger
  of the two is used, and a disagreement is logged.
- **Log noise (F3, F4).** `hpack` and `h2` at WARNING (`httpcore` is already
  ERROR). The hub and `usd_snapshot` warnings name the error type when
  `str(ex)` is empty.
- **Close the token account when a bucket retires (F2).** Owner decision
  (2026-10-06): close it and record the refund.
  - *When:* the `serve` sweep, for a bucket that is RETIRING and flat (after
    expiry or max loss; the sweep already sells what is left first).
  - *Only if* the account is the bot's and nothing needs it: the mint is not
    SOL; no other open bucket of the `serve` is ACTIVE on the mint (either
    side of its pair); no open position on the mint in any bucket of the
    mode's ledger; a fill of the mode paid rent for the mint after its last
    close (an account the owner made is never closed); the wallet's associated
    account of the mint exists and holds exactly 0 (dust or the owner's own
    tokens: left alone; `CloseAccount` would refuse it anyway); no close of the
    mint pending.
  - *Who gets the refund:* the bucket of the latest fill that paid that rent,
    which may not be the retiring one.
  - *How (real):* one `CloseAccount` instruction (the account's token program,
    destination and authority the wallet), base fee only (it is not urgent),
    the program check and a simulation before signing off, then send and
    confirm. A `rent_refund_sent` event (signature, block height, lamports)
    is written before the broadcast; if that write fails, nothing is sent.
    After confirmation, `rent_refund` (account, mint, signature,
    `refund_lamports`, `fee_lamports`, `sol_usd`, `net_usd`). A send with no
    `rent_refund` is resolved on the next sweeps with the A3 outcome check:
    landed -> `rent_refund`; failed -> `rent_refund` with refund 0 and the fee;
    expired -> `rent_refund` with both 0; pending -> wait.
  - *How (paper):* the simulated wallet drops the account from
    `open_accounts` and credits the rent it charged minus the base fee, under
    its lock.
  - *Accounting:* `AccountPnL.add_rent_refund` (`rent_refund_lamports`, the
    fee into `fee_lamports`, `net_usd += net_usd`, `costs_usd -= net_usd`; no
    SOL price: counted in `unknown_costs`); the payer's open bucket re-reads
    its book from the ledger;
    `round_trip_costs` subtracts the refunds in its window (the rent sits in
    the first trip's cost). `ledger_dump.py` and the daily report show it
    through those totals.
  - *Limits:* only buckets opened in this `serve` (a spec whose `connect`
    never comes back is not swept); the account of a bucket that retired
    while another was ACTIVE on the mint closes when that one retires.
- **Done.** As designed, except that `rent_lamports` stays what was paid and
  the refund is its own field (`rent_refund_lamports`), so the daily report
  shows both. Code: `TokenAccount` and `associated_token_accounts`,
  `sign_instructions` (`async_rpc_client.py`); `token_balance` and
  `close_token_account` on both executors and the provider;
  `SimulatedWallet.close_account` (the paper `fetch_fee` now reads the fee of
  an applied entry, so a resolved paper close books its fee);
  `trader/execution/models/rent.py` (`RentRefund`, the two event names);
  `Reports.rent_payer` / `pending_rent_refunds` / `_account_events`,
  `RoundTripCosts.refund`; `TradeService.close_token_account` /
  `resolve_rent_refunds`, called by the `serve` sweep;
  `PositionBook.rent_refund_sol` in the bucket summary; `error_text` in
  `logging_config.py`. After `/simplify`: the direct read moved from the sell
  into `WalletBalances.fresh` (every order and the startup reconcile, not SOL,
  both reads at once); the close passes the swaps' send gate first
  (`policy.send_refusals`: `real_trading_enabled`, unresolved intents,
  breaker; refused -> retried next sweep); the provider reads the close's fee
  (`fetch_failed_fees`, one fallback path); the sent event is `asdict(SentTx)`;
  the paper wallet's applied-log append is one helper (`_logged`), so a close
  also moves `trimmed_at`. Not done: folding the close into the A3 intent
  resolver (it would need a non-swap intent and a schema change). 801 tests (18 new): the sell re-read (missing token,
  covered read, failed direct read), the direct RPC read, the logger levels,
  and `test_rent_refund.py` (refund and its accounting, once per bucket, every
  rule that leaves the account alone, the payer, a failed close, a send
  resolved on the next sweep, the on-chain instruction and its order) plus
  the sweep calling it; the "another bucket active" and "a bucket holds the
  token" rules were each disabled once to see their test fail. Not checked
  live: no real close has been sent yet. The A6 run 1 JUP account (1,488,440
  lamports, paid by `real:strategy:ad3606394fc0`) closes the next time that
  bucket is swept as retired: once its one-day TTL is over, when the owner
  runs `serve real` and connects the spec again. (Done 2026-10-07: the
  whole 1,488,440 lamports came back, see A6 below.)

#### A6. First real run — S (owner, done 2026-10-07)
- The owner's answers to §7 recorded: pairs, `max_trade_usd`, daily notional,
  one hot wallet.
- The Helius key rotated; `real_trading_enabled = true`; the priority-fee cap
  reviewed for small trades (soak F7: at 5 USD the cap is most of the ~71 bps
  round trip).
- A dedicated low-balance wallet; one liquid spec with a tiny budget,
  backtested and soaked in paper.
- `serve real` + `connect` started by the owner in a terminal (agent sessions
  can't).
- Afterwards: `ledger_dump.py`, the daily report and `live_vs_backtest.py`;
  write down the exit criteria for raising the budget.
- **Constraint (owner, 2026-10-06): the wallet holds about 5 USD, all SOL.**
  At ~120 USD/SOL that is ~0.042 SOL; the 0.02 SOL fee reserve
  (`DEFAULT_SOL_FEE_RESERVE`) leaves ~0.022 SOL (~2.6 USD) spendable, and
  `_check_allocation` measures budgets against that. So: a SOL-input spec
  (no USDC to spend), budget ~2 USD with room for a SOL drop between
  restarts; the first buy of a new token opens its account (~0.002 SOL,
  ~0.25 USD rent, refundable only by closing it); the 100,000-lamport
  priority-fee cap is ~0.012 USD a leg, ~60 bps of a 2 USD trade, so lower
  it; tight `[real.limits]` (trade, daily notional, daily loss, trades per
  hour). The goal is a few round trips that exercise the real path, not PnL.
- **Chosen (2026-10-06):** `docs/examples/spec-real-first-run.json`, JUP-SOL
  (`USDC-SOL` is refused: the bought token can't be a stablecoin), a 1% draw
  per tick, out after 10 min or a 1% stop, 60 min cooldown, 1.5 USD a buy,
  budget 2, max loss 0.5, one day. Backtests at a 20,000-lamport cap: 14
  round trips a day, -0.10 to -0.15 USD. Policy for the owner to add:
  `[real.trading]` `real_trading_enabled = true`, `allowed_symbols = ["SOL",
  "JUP"]`, `max_priority_fee_lamports = 20000`; `[real.limits]`
  `max_trade_usd = 2`, `max_daily_notional_usd = 40`, `max_trades_per_hour =
  2`, `max_trades_per_hour_per_bucket = 2`, `max_daily_loss_usd = 1`,
  `max_consecutive_failures = 2`.
- **Run 1 (2026-10-06, 06:00-08:41 UTC):** 3 JUP-SOL round trips, every leg
  through the quote check, inspection and simulation, confirmed at the first
  send; nothing UNCONFIRMED, no failed tx, flat at the end. Ledger: net
  -0.001595 SOL (-0.19 USD) = swaps +0.000043, fees -0.000150 (6 x 24,999
  lamports), rent -0.001488 (the JUP account, first buy only). Without the
  rent a round trip cost ~0.007 USD (~46 bps); the backtest on the recorded
  ticks says 0.009 USD (59.8 bps), so it errs on the safe side. Fill vs quote
  under 3 bps except the first buy (19.7 bps). Findings, fixed in A15:
  - **F1.** Two of three exits were refused once ("Sem valor minimo") and
    went through 30-45 s later: the fresh wallet read (`getTokenAccountsByOwner`)
    came back without the JUP account, one minute after a read that had it.
  - **F2.** The rent is booked as a cost and never comes back: the account
    stays open after the sell. It is 93% of the bucket's loss and 36% of its
    max loss, and makes the round-trip cost read 441 bps.
  - **F3.** Prices stale for ~4 min (08:04-08:08 UTC): websocket and Price
    API failed together (a local network blip); the warnings end in an empty
    message (`str(ex)` of a timeout is empty).
  - **F4.** 12,306 of ~12,700 lines of the `serve` log are `hpack` DEBUG
    (with Cloudflare cookies).
- **Run 2 (2026-10-06, 14:09-16:38 UTC, A15 build, same spec and bucket):**
  2 JUP-SOL round trips (-0.0046 and -0.0063 USD), every leg executed at the
  first send; nothing UNCONFIRMED, no failed tx, flat at the end. The `serve`
  log is 10 lines (F4 fixed); the websocket dropped 5 times and the Price API
  covered each time, with no stale price (F3); no exit was refused (F1 not
  seen again). Fees 24,999 lamports a leg (the 20,000 cap holds). Bucket after
  5 round trips: net -0.201 USD, of which rent 0.00148844 SOL (~0.178 USD);
  without it ~0.006 USD (~41 bps) a round trip against the backtest's 60 bps.
  **Not yet exercised: the rent refund (F2).** It happens only when the
  bucket retires flat: the one-day TTL counts from `bucket_opened` (~06:00 UTC
  on 2026-10-06), so `serve real` + `connect` started after ~06:01 UTC on
  2026-10-07 retires it in the first sweep and closes the JUP account
  (`rent_refund_sent`, then `rent_refund`; the backtest and
  `round_trip_costs` then drop the rent). Live vs backtest over both runs
  (`--mode real`; without it the script reads the paper bucket): 10 live
  legs, 11 backtest (one more buy after live stopped). With a
  `random_chance` entry the legs fall at different moments (run 1 up to
  ~15 min apart, run 2 ~2 min), so per-leg price gaps (-56 to +94 bps) are
  timing, not fills. The four round trips that ended on `max_hold` in both
  match: backtest -0.0228 USD, live -0.0210 (rent left out). Costs: live
  ~0.006 USD a round trip, almost all network fee (2 x 24,999 lamports,
  ~40 bps at 1.5 USD), so fill slippage is about 1 bps; the backtest's
  10 bps a leg is what puts it at 60 bps.
- **Exit criteria for a bigger budget** (all must hold; A6 closes when they
  do):
  1. At least 5 clean round trips: nothing UNCONFIRMED or EXECUTING, no
     failed tx, no exit refused (met by runs 1 and 2 except run 1's F1
     refusals, fixed in A15).
  2. The rent refund received: after expiry the sweep closes the JUP
     account, `rent_refund` is booked to the bucket and `ledger_dump.py`
     shows `rent_refund_lamports` ~1,488,440 (met 2026-10-07, below).
  3. Live cost per round trip, rent left out, at or below the backtest's
     (met: ~41 vs 60 bps).
- **Then (a proposal for the owner):** a wallet of 10-15 USD; a new spec
  with 5 USD a buy, so the fixed ~0.006 USD of network fees a round trip is
  ~12 bps instead of ~40; `max_trade_usd = 5` and `max_daily_loss_usd = 3`
  in `[real.limits]`, with the other limits scaled to match.
- **Rent refund (2026-10-07, 06:19-06:20 UTC):** `serve real` + `connect`
  after the TTL; the first sweep (30 s after start) retired the bucket
  ("spec vencida"), `connect` stopped by itself, and the same sweep closed
  the JUP account: 1,488,440 lamports back to
  `real:strategy:ad3606394fc0` for a 5,000-lamport fee (tx
  `SxA7KpeE...gc2Dwd`), the whole rent the first buy paid. F2 is closed on
  the real path; the bucket's net is ~-0.000226 SOL (~-0.03 USD) after 5
  round trips. A first attempt stopped 3 s after `connect`, before any
  sweep: the owner has to leave both running for one sweep (~30 s).
  All three exit criteria hold: A6 is done.

#### A7. Perps: decoupling — M (done 2026-10-07)
 `Venue` protocol (execution stops importing the
concrete Jupiter provider; `SpotVenue` wraps it); `ExecutionResult` replaces
`SwapResult` in the gateway and `mark_executed`; `BucketAccount` protocol
(`AsyncAccount` becomes `SpotAccount`; allocation and reconcile through
`committed_usd()`/`held()`); `direction` on `TickContext` and `Position`
(default long) used by the five position blocks and `_track`, tested both
ways; `canonical_json` leaves out default-valued new fields, with a test
pinning the ids of `docs/examples/`. Done when the example specs' backtests
are identical and `tests/test_architecture.py` maps the new modules.
- **Design (2026-10-07).** No behaviour, schema, wire or spec-id change.
  - `ExecutionResult` (`trader/execution/models/execution.py`, core) is
    `SwapResult` renamed and moved out of `shared` (only the trade-runner
    and the backtest use it). The fields stay, so the `intent_executed`
    payload and the `signature`/`in_amount`/`out_amount` columns are
    unchanged; A8 adds the perp part as an optional field.
  - `Venue` (`trader/execution/models/venue.py`, core, a `Protocol`):
    `native_fee_reserve`, `balances()`, `token_balance(mint)`,
    `open(input, output, spend)` and `close(input, output, quantity)`
    returning an `ExecutionResult`, `fetch_costs(result)` (never raises),
    `fetch_failed_fees(signatures)`, `send_outcome(sent)`,
    `close_token_account(mint, announce)`, `aclose()`. `positions()` waits
    for A10. `SpotVenue` (`trader/execution/trade/venues/spot.py`) wraps an
    `AsyncJupiterProvider` (kept as `.provider`; `wiring` reads its
    `jupiter_client` and sets `usd_prices` there). The account,
    `WalletBalances`, `fills`, the resolver and `TradeService` take a
    `Venue` (attribute `venue`); a test in `test_architecture.py` keeps
    `trade/gateway` and `trade/trading_service` from importing
    `trade/venues`. `wiring` and the backtest wrap their provider.
  - `BucketAccount` (`trader/execution/models/bucket.py`, core,
    `Protocol`): what `TradeService` uses of an account (`account_id`, the
    two mints, `book`, the restored times, `restore_from_ledger()`,
    `buy()`/`sell()`, `get_spendable_balance()`, `committed()`).
    `AsyncAccount` becomes `SpotAccount`; `committed()` (the quote spent on
    the open position, was `_position_cost`) feeds the allocation check.
    BUY/SELL keep their names (they mean enter/exit, D2). The reconcile
    stays on the ledger's open positions, which covers buckets no
    `connect` has opened yet; `held()` comes with A8, when perp positions
    must leave the wallet reconcile. (Superseded in A8: `open_positions`
    skips entries with a `perp`, which also covers unopened buckets.)
  - `Direction` (`trader/shared/models/direction.py`: `LONG`, `SHORT`, a
    `sign`). `Position.direction` (default `LONG`) signs
    `unrealized_pnl`; the wire sends it only when it isn't long, so old
    peers still talk. `TickContext.direction` comes from the position in
    `_track`; `peak` is the best price since entry (the max long, the min
    short); `take_profit`, `trailing_take_profit`, `stop_loss` and
    `trailing_stop` compare in the favourable direction (`max_hold` has
    none). Tests run them both ways; long results are unchanged.
  - Spec ids: a test pins the ids of every file in `docs/examples/`
    (`ad3606394fc0` has a real bucket). Leaving a default-valued new field
    out of `canonical_json` lands with the first such field (`market`,
    A8); A7 adds no spec field.
  - Checked by replaying every example spec with a tick file
    (`.data/ticks`, fixed costs and seed) before and after: identical JSON.
- **Result.** Every example spec with a tick recording (8 of 9; the
  `NOBODY-USDC` one has none) replays to identical `--json` output before and
  after (fixed fee, network fee and seed); `serve paper` + `connect` smoke runs
  bought and sold through `SpotVenue` with no error lines. New tests:
  `test_direction.py` (the four position blocks and the PnL both ways, the
  wire), `test_venue_seams.py`, `test_example_ids.py`, and the
  architecture rule that `trade/gateway` and `trade/trading_service` don't
  import `trade/venues` (the layer map: `execution` may no longer import
  `venue`). 829 tests. Left for A8 (from `/simplify`): `TradeService` still
  builds `SpotAccount`; gateway tests still mock the Jupiter provider through
  `SpotVenue` (`spot_provider`) instead of a `Venue` mock; `Position.direction`
  isn't persisted.

#### A8. Perps: model, paper and spec — L (done 2026-10-07)
 The `market` block (+ `specs.md`)
checked against the policy at `hello`; `PerpTerms`, `PerpPosition`,
`PerpAccount` (a `BucketAccount`, A7; the wallet reconcile leaves perp
positions out, through a `held()` on the protocol; `TradeService` gets
the account kind from the venue instead of building a `SpotAccount`, and
the direction is stored with the entry, not only on `Position`) [as built:
the reconcile skips perp entries in `open_positions`, and the account kind
comes from the spec's market (`open_bucket(perp=...)`)]; ledger schema +1 (D6) and restore of an open perp position;
`SimulatedPerpsVenue` and the liquidation check in `serve`'s sweep; policy
`perps_enabled`, `max_leverage` (default 3), `allowed_perp_markets`,
exposure as notional; the daily report and notifications show direction,
leverage, liquidation price and accrued borrow. Done when a short spec in
paper opens, holds, stops out and is liquidated in a forced test, with the
ledger and PnL right after a restart.
- **Design (2026-10-07).** The owner archives `.data/ledger-*.sqlite3`
  before running this build (schema 2 -> 3, no migration; decided
  2026-10-07). A perp leg rides the existing swap-shaped path (intent ->
  gateway -> ledger -> `Order` -> `PositionBook`) with the perp parts
  added, so idempotency, policy, breaker, restore and PnL stay one code
  path. Spot is unchanged and spot spec ids too (pinned).
  - **Spec and terms.** `market` (`PerpMarket` in
    `trader/shared/spec/terms.py`: `kind: "perp"`, `venue: "jupiter"`,
    `direction`, `leverage` 1.1-250) on `StrategySpec` and `SpecTerms`;
    `canonical_json` leaves it out when it's `None`. `symbol` stays
    `BASE-QUOTE` (`SOL-USDC`): the base is the market, the quote a USD
    stablecoin, the collateral (D5). The spec itself requires a `max_hold`
    exit and a stop (`stop_loss`/`trailing_stop`) of at most half the
    liquidation distance after fees (`pct <= 50 / leverage - 0.12`); the
    terms carry `stop_pct` and `max_hold_minutes` so the trade-runner
    re-checks them at `hello` (`validate.py`), with `perps_enabled`,
    `max_leverage` and `allowed_perp_markets`. Sizing and `budget_usd`
    count collateral; the exposure (collateral x leverage) is what
    `max_trade_usd` and the daily notional see.
  - **Policy.** `perps_enabled` (default off; on in `PAPER_DEFAULTS`),
    `max_leverage` (3), `allowed_perp_markets` (`["SOL"]`, the only one in
    the registry). `evaluate` refuses a perp entry that breaks them; exits
    are never blocked.
  - **Models.** `Direction` stays in `shared`; `PerpTerms` (market mint,
    direction, leverage) on `TradeIntent.perp`; `PerpFill`
    (`trader/shared/models/perp.py`: price, size USD, collateral, fees,
    borrow, liquidation price, liquidated) on `ExecutionResult.perp` and
    `Order.perp`, so the entry order carries the direction and the
    `Position` takes it from there (restore included). Order convention for
    a perp leg: `quantity` is the size in the base token, `fill_price` the
    oracle fill price, `quote_amount` the collateral posted (entry) or
    returned (exit); the existing PnL then is collateral back - collateral
    posted (D9). `Position.unrealized_usd` is direction-aware and, for a
    perp, takes off the accrued borrow and the close fee.
  - **Ledger (schema 3).** `intents.instrument` (`spot`/`perp`) and
    `intents.perp_json` (the `PerpTerms`). `open_positions` (the wallet
    reconcile) counts spot only. A liquidation is recorded as an EXECUTED
    exit leg outside the policy (`Ledger.record_external`, rationale
    `liquidation`): the whole collateral is a realized loss and counts for
    `max_loss_usd`.
  - **Venue and account.** `PerpVenue` (protocol, beside `Venue`):
    `open_perp(collateral, market, amount, terms)`,
    `close_perp(collateral, market, terms)`, `position(market, direction)`
    and `liquidations(now)`, plus the common post-trade calls.
    `SimulatedPerpsVenue` (`trader/execution/trade/venues/paper/perps.py`)
    keeps positions under `perps` in `paper-wallet.json` (one per market and
    side, like Jupiter), prices from the process oracle (the hub; spot
    stands in for the venue oracle), and the Jupiter fee model from §8.2:
    0.06% of size each way, a linear impact fee, an hourly borrow fee
    (default 1 bps of size an hour; A10 brings the live rate), liquidation
    at 0.2% of size; each leg also pays the base fee + priority cap in SOL
    like spot paper. `PerpAccount` (`trade/gateway/perp_account.py`, a
    `BucketAccount`) spends collateral from the wallet and always closes
    the whole position (partial closes are A12). `TradeService` takes the
    perp venue beside the spot one (`None` in real: perp buckets are
    refused until A11), picks the account kind from the spec's market, and
    refuses a second open perp bucket on the same market and side.
  - **Sweep.** `serve`'s sweep asks the perp venue for liquidations at the
    current price and books each one on its bucket (`perp_liquidated`
    event, Telegram). The A3 resolver leaves a perp intent blocking (the
    owner checks); a perp send log comes with A11.
  - **Reports.** `connect`'s fill messages and the daily report show
    direction, leverage, liquidation price and accrued borrow. `backtest`
    refuses a perp spec until A9.
  - **Done when** a short `SOL-USDC` spec in paper opens, holds, stops out,
    and is liquidated in a forced test (prices moved past the liquidation
    price), with the ledger and PnL right after a restart; plus
    `docs/examples/spec-sol-short.json` and a `/smoke` of it.
- **Result.** As designed, with these details: `Position.direction` is
  derived from the entry order's `perp` (no separate field, nothing extra on
  the wire); `PerpAccount` is a `SpotAccount` subclass (restore, intents and
  the USD snapshot shared; `post_trade` points at the perp venue);
  `PerpVenue.liquidations(open_)` checks only the positions of buckets this
  `serve` has open (another bucket's is checked when it connects); the
  `serve` sweep is a list of steps. `ledger_dump.py` shows an open perp's
  `PerpFill`. Tests: `test_perp_buckets.py` (the "done when": a 3x short opens,
  stops out, reopens, is liquidated past its liquidation price, and a fresh
  service on the same ledger and wallet has the same PnL; an open short is
  restored and closed after a restart; one bucket per market and side; no perp
  venue in real; the policy's switch and exposure limit),
  `test_paper_perps.py` (fees, borrow, liquidation math, the wallet),
  `test_perp_spec.py` (spec, terms, `hello` rules), and the backtest refusal.
  `docs/examples/spec-sol-short.json` (id pinned). A `/smoke` of a fast copy
  of it (20% draw, 2 min `max_hold`) opened, closed and reopened in 260 s: the
  round trips cost ~0.05 USD each at 30 USD of exposure (0.036 in perp fees +
  2 x 105,000 lamports). 857 tests. `/code-review` and `/simplify`: one
  position per market and side is checked at the buy, against the venue
  (`PerpVenue.has_position`), so a retired v1 never blocks v2 and a clash is
  a clean refusal with no intent; equity has one formula (`PerpFill.equity`,
  with the venue's close fee), so the stored liquidation price is where the
  paper venue liquidates; a
  liquidation is booked before the venue forgets it (`acknowledge`), so a
  failed ledger write is retried; `spec_backtester` refuses perps, so
  `live_vs_backtest.py` does too.

#### A9. Perps: backtest — M (done 2026-10-07)
 The replay uses the perp engine;
`--borrow-bps-hour`; liquidation on the candle path; the summary shows
liquidations and fees; two example specs.
- **Design (2026-10-07).** The replay is the paper path already
  (`TradeService` -> gateway -> in-memory wallet), so a perp spec replays
  through the same `PerpAccount` and the A8 engine, not a second model.
  - `ReplayPerpsVenue` (`trader/backtest/replay.py`, a
    `SimulatedPerpsVenue`): the in-memory replay wallet, `ReplayPrices`
    (the tick is the oracle), the tick clock (borrow accrues in replay
    time), and the network fee as a per-leg cost exactly like
    `ReplayExecutor` (one shared helper; the replay wallet holds no SOL).
    `Backtester` takes `perp: PerpTerms | None` and `borrow_bps_hour`,
    builds the service with it, and opens the bucket with the terms.
  - **Liquidation on the candle path:** before the strategy's step on each
    tick, `service.check_liquidations()`; ticks already walk each candle
    open -> low -> high -> close, so the adverse extreme of every bar is
    seen. A liquidation becomes a trade marked `liquidated`.
  - **Equity** adds the open perp's value at the tick (what a close would
    return: `SimulatedPerpsVenue.equity_at`, 0 once liquidated) and takes
    off the network fees, like spot.
  - **Result and summary:** `BacktestResult.perp` (direction, leverage,
    borrow bps/h, liquidations, perp fees and borrow paid in USD), `None`
    for spot; trades get `liquidated`. The text summary prints the perp
    costs instead of the pair's bps line and flags liquidated trades.
    `fee_bps`/`slippage_bps` don't apply to a perp (the engine prices it);
    the network fee is still measured or given.
  - **CLI:** `backtest --borrow-bps-hour N` (default 1, the paper value);
    `refuse_perps` goes away, so `live_vs_backtest.py` replays perps too
    (`ReplayCosts.borrow_bps_hour`).
  - **Examples:** `spec-sol-perp-long.json` (2x long on a dip) and
    `spec-sol-perp-short.json` (3x short on a spike), both backtested.
  - **Check:** the spot backtests of `docs/examples/` are identical to
    before (the JSON with the new `perp`/`liquidated` keys left out).
- **Result.** As designed, plus: `Policy.unlimited()` (the replay's) allows
  perps with no leverage cap, and an empty `allowed_perp_markets` now means
  every market (like `allowed_symbols`); `Ledger.round_trip_costs` measures a
  perp leg on its exposure (spend x leverage) and in its direction, and a
  liquidation leg carries the liquidation price. The spot check ran on
  synthetic ticks (a seeded 6-day random walk; `.data/ticks` had been
  cleared): the seven spot examples with ticks replay to identical JSON. On
  those ticks `spec-sol-perp-long` made 61 round trips (+1.15 USD, fees
  1.46, borrow 0.11, ~16 bps a trip) and `spec-sol-perp-short` 75 (-2.98
  USD, ~15 bps a trip); no liquidations (their stops are far inside).
  `test_perp_backtest.py`: a gap past the liquidation price liquidates before
  the stop (all collateral lost, equity 20 of 30), borrow accrues on the
  replay clock, equity marks the open perp, the round-trip cost is ~12 bps on
  the exposure for a short. `docs/examples/spec-real-first-run.json` was
  removed by the owner (its bucket is archived) and left the pinned ids. 863
  tests. `/simplify`: `check_liquidations` returns the booked exit orders, so a
  liquidation is recorded like any exit (its borrow counts in the summary, no
  close fee); one `perp_terms_for` builds the terms for `serve` and the
  backtest; the replay prices perps straight from the tick. Skipped: a shared
  network-fee holder for both replay venues and caching the parsed fill per
  tick (~7% of a perp replay). 864 tests.

#### A10. Perps: Jupiter read-only — M (done 2026-10-07)
 IDL decoding (`anchorpy` or
hand-written Borsh, after a spike); `positions()`, oracle price, borrow rate;
paper uses the live borrow rate; live tests read the pool and custody and an
empty position. No schema change: can run any time after A7.
- **Spike (2026-10-07).** Hand-written Borsh, no `anchorpy`: a ~80-line
  reader driven by the program's Anchor IDL (the community copy,
  `julianfssen/jupiter-perps-anchor-idl-parsing`) decoded the live JLP pool
  and SOL custody through the public RPC, discriminators included
  (`sha256("account:<Name>")[:8]`; accounts are 2000 bytes, zero-padded).
  The SOL custody reads 6 bps to open and 6 to close (our 0.06%). The price
  that moves is the Doves aggregated feed (`custody.dovesAgOracle`,
  `AgPriceFeed`: price x 10^expo, a few seconds old); the plain
  `dovesOracle` was months stale. Borrow is the jump-rate model on
  `custody.jumpRateState` (annual bps; `targetUtilizationRate` x 1e9),
  utilization `assets.locked / assets.owned`, hourly = annual / 8760: SOL
  was ~14% used, ~1450 bps a year, ~0.17 bps an hour (paper's 1 bps/h is
  6x too high). Sources: the IDL repo and Jupiter's governance posts on the
  jump-rate model (discuss.jup.ag, Gauntlet and Chaos Labs).
- **Design.** All read-only, in the market layer (never moves funds):
  `trader/execution/market/perps/`: `idl.py` (the IDL-driven Borsh reader
  and the discriminator check), `perpetuals_idl.json` (only the accounts
  and types of the vendored IDL, plus Doves' `AgPriceFeed`) and `reader.py`
  (`JupiterPerpsReader`: `pool()`, `custody(mint)`, `borrow_bps_hour(mint)`,
  `oracle_price(mint)` with its timestamp, `position(owner, terms)` by PDA
  `["position", owner, pool, custody, collateral custody, side]`, a long's
  collateral custody being its own and a short's USDC). It speaks JSON-RPC
  `getMultipleAccounts` at Confirmed over `httpx`, to `HELIUS_RPC_URL` when
  set, else the public mainnet RPC. Program, pool and custody addresses are
  constants; a custody's decoded mint is checked against the market's.
  - **Paper:** `SimulatedPerpsVenue` takes a borrow-rate source; `wiring`
    gives paper the reader's live rate, read when a position opens and kept
    in its `PerpFill.borrow_bps_hour`; a failed read falls back to the
    default with a warning. The fee stays 0.06% (matches the custody), and
    prices stay the hub's (the Doves feed is exposed, not used yet: A12
    routes perp state through the hub). The backtest keeps
    `--borrow-bps-hour`.
  - **Tests:** unit tests decode recorded account bytes (fixtures captured
    from mainnet, so offline); `tests/live/test_live_perps.py` reads the
    pool (its custodies include SOL's), the SOL custody (mint, 9 decimals,
    6/6 bps), a borrow rate in a sane band, a fresh oracle price within 5%
    of the Price API, and an empty position for a fresh keypair.
- **Result.** As designed. The reader worked live: SOL custody utilization
  14.4%, borrow 0.166 bps an hour, fees 6/6 bps, Doves price 116.04 (2 s
  old), no position for a fresh wallet. The position PDA formula matched 354
  of 354 real SOL positions listed from the program. A `/smoke` of a fast
  short opened and closed at the live rate (0.1663 bps/h kept in the fill).
  Unit tests decode real account bytes captured that day
  (`tests/trader/execution/market/perps_accounts.json`, a real open short
  with its owner zeroed); `tests/live/test_live_perps.py` (3 tests) passes
  against mainnet. Not used yet: the Doves price (paper keeps the hub's spot
  price; A12) and the custody's live fee bps (it matches 0.06%; the live
  test fails if it changes). 879 tests. `/simplify`: the reader retries 429/5xx with the
  market layer's `HTTP_RETRY` and reports the RPC's own error; paper reads the
  borrow rate at most every 5 minutes, waits at most 5 s (the open runs under
  the order lock) and falls back to the last good rate before the default;
  the decoder refuses enum variants with fields (they would misalign every
  later field); the unused USDT custody and a duplicate `BPS` are gone.
  Deferred to A11: one RPC-URL helper shared with the venue (A11 must never
  fall back to the public RPC), reading custodies from `pool.custodies`
  instead of the constant table (a second market), and the full upstream IDL
  pinned to a commit (A11 needs the instructions and `PositionRequest`).

#### A11a. Perps: requests built and simulated — M (done 2026-10-07)

- **Design (2026-10-07).** Nothing here signs or sends; real `serve`
  keeps no perp venue until A11b.
  - The vendored IDL gains the three request instructions, their params
    types and the `PositionRequest` account; `idl.py` gains the Borsh
    encoder (the decoder's mirror) and `encode_instruction(name, params)`
    (discriminator + args).
  - `trader/execution/trade/venues/jupiter_perps/requests.py`: the account
    metas and PDAs, and `open_request` (side, collateral in USDC raw, size
    USD, the price limit: long pays at most price x (1 + slippage), short
    at least price x (1 - slippage); a long's `jupiterMinimumOut` from a
    USDC->SOL quote), `stop_request` (a trigger at the spec's stop price,
    above for a short, below for a long, entire position, desired mint
    USDC) and `close_request` (market, entire position, USDC back). The
    request counter is `sha256(idempotency key)[:8]` as a u64 (D8): the
    same intent always targets the same request account, so a resend
    after a crash fails instead of opening twice. `build_transaction`
    wraps them with the compute budget (limit 200k; price from the
    policy's `max_priority_fee_lamports`) as an unsigned v0 message.
  - `simulate(rpc, tx)` returns the units and logs or raises
    an error with the program's last log line (as built: `PerpsReadError`,
    from `JupiterPerpsReader.simulate`).
  - Reconcile (D10): `reconcile_perps(ledger entries, venue positions)`
    lists open ledger perps with no position on the venue and venue
    positions the ledger doesn't know; the service will turn each into a
    `perp_mismatch` event and stop entries in that market (wired in A11b,
    unit-tested here).
  - Tests: offline, the encoder against known bytes (a Borsh round trip
    through the decoder, and the discriminators); `tests/live/` simulates
    an open short and an open long (payer: a public exchange wallet that
    holds SOL and USDC) and, when the program has one, a market close and
    a trigger on an open short found on chain (its owner as payer, never
    logged).
- **Result.** As designed. All four requests built by the module simulate
  on mainnet: open short 98.6k units, open long 89.6k, close 87.9k, stop
  89.7k. The encoder reproduces the spike's bytes; the reader gained
  `simulate` (read-only) and `program_accounts` (memcmp filters, for the
  live test now and for finding a wallet's positions in A11b). Live: 5 tests
  in `test_live_perps.py` (the close/stop one skips if the program has no
  open SOL short). 887 tests. `/simplify`: the reader's three RPC calls share
  one `_rpc` helper; `memcmp` filters go in base64 (no hand-made base58);
  `requests.py` uses `spl.token`'s program ids and ATA helper and the
  reader's `USD_SCALE` and `collateral_mint`; a perp's quote must be USDC
  (the requests fund from and pay to the USDC account; SOL-USDT was accepted
  before); the tests read the arguments back through the IDL by name. A
  review asked whether two buckets could share one Jupiter position: no, a
  perp buy is refused while that market and side has an open position
  (`has_position`), so a whole-position close never closes another bucket's.
  890 tests.

#### A16. Execution cleanup — M (done 2026-10-08)

- **Design (2026-10-08).** What the review of `trader/execution/`
  (about 10k lines) found, and the step that fixes each:
  1. *The gateway rebuilt positions.* `gateway.py` held ~90 lines that turn
     an account's legs into its open entry (`_open_entry` and helpers): that
     is reading the ledger. They move to `trade/ledger/positions.py`, behind
     `Ledger.open_entry(account)` and `Ledger.open_positions(prefix)`; the
     gateway keeps the submit path and delegates. Every ledger event name
     goes into one module, `trade/ledger/events.py` (they were constants in
     three modules plus a dozen inline strings).
  2. *The `gateway/` package held the accounts.* `SpotAccount`,
     `PerpAccount` and `WalletBalances` move to `trade/accounts/` (`spot.py`,
     `perp.py`, `wallet.py`); `trade/gateway/` keeps the one path to a swap
     and its settlement (`gateway`, `fills`, `orders`, `resolve`). Its own
     layer in `tests/test_architecture.py`.
  3. *`TradeService` was the largest module (670 lines).* The rent-refund
     lifecycle (A15: close, record, resolve) moves to
     `trading_service/rent.py` (`RentRefunds`), which the service calls.
  4. *Jupiter file names.* `market/jupiter/async_jupiter_client.py` ->
     `client.py`, `jupiter_data.py` -> `quote.py`;
     `venues/jupiter/async_jupiter_svc.py` -> `provider.py`,
     `async_rpc_client.py` -> `rpc.py` (test files follow). `trade/venues`
     stops re-exporting the quote classes from `market`.
  5. *Small seams.* `MintBalance` joins the venue contract
     (`models/venue.py`; `account_data.py` goes); the SOL fee reserve is a
     venue fact there too, so paper no longer imports the on-chain executor;
     `Venue` stops redeclaring the `PostTrade` methods; stale docstrings
     fixed.
  6. `AGENTS.md` and `architecture.md` follow the new paths.

  Kept on purpose: `SpotVenue` (the A7 seam; the provider keeps its
  swap-shaped API for paper, replay and tests), the name `trading_service`
  (the same on the shared, strategy and execution sides), and
  `jupiter_perps/reconcile.py` (unused, but A12 wires it).
- **Result.** As designed; no behaviour, schema, wire or spec-id change.
  `TradeService` went from 670 to 600 lines (`rent.py` 110), `gateway.py`
  from 365 to 270 (`ledger/positions.py` 100). Old import paths are gone,
  not aliased: the tests, `.claude/scripts`, the live suite and the docs use
  the new ones. 907 tests, the same as before.

#### A16b. Execution cleanup, second pass — M (done 2026-10-08)

- **Design (2026-10-08).** A second read of the
  modules A16 only skimmed (market, venues, ledger internals, policy):
  1. *A dead price path.* Since the hub (B7) the bots get prices through
     `HubMarketData`; `JupiterMarketData.get_price` (a websocket per mint,
     the Price API as fallback) and the websocket methods of
     `AsyncJupiterClient` (`get_price`, `_read_price`, `_get_price`,
     `_connect_price_ws`) are only called by tests. They go;
     `JupiterMarketData` becomes `JupiterCandles`, the `CandleSource` that
     `serve` and `backtest` use. Its tests go or move to a fake feed.
  2. *Event names A16 missed:* `intent_executed`, the `intent_<status>`
     f-string and `order_recorded` in `ledger/intents.py`.
  3. *Two copies of the guarded send.* `OnChainExecutor.execute` (swaps) and
     `send_instructions` (rent close, perps) each simulate, check balances,
     log the send and send; one helper does it for both.
  4. *RPC client.* `sign_transaction` signs twice (`VersionedTransaction`
     with the keypair already signs, then it signs the message again by
     hand); `is_connected` runs before every call and its answer is never
     read. One signature, no probe.
  5. *`SimulatedWallet`* repeats lock -> read -> compute -> write -> assign
     five fields in three methods; one `_commit` does the last two.
  6. *Small:* paper's own `DEFAULT_FEE_LAMPORTS` is `BASE_FEE_LAMPORTS`; the
     client re-exports `Interval` "for compatibility" (no one uses it) and
     inlines its error notes twice next to `_add_response_notes`;
     `logger_wrapper` has two identical `except` branches; orphan comments.

  Seen and left: `check_signature_is_confirmed` (RPC) and `_status_outcome`
  (executor) both read a signature status, but it is the real-money
  confirmation path with 20 tests on its current shape; and the ledger's
  mode-prefix filter (`substr(account, 1, ?)`) is repeated in three
  queries, which reads fine.
- **Result.** As designed; no behaviour, schema, wire or spec-id change.
  `trader/execution/` lost about 330 lines net. The live suite now reads
  prices the way production does: a running `PriceHub` behind a
  `HubMarketData` (`live_helpers.running_hub`/`hub_feed`), and the websocket
  check reads `websocket_prices` directly. `live_vs_backtest.py` needed only
  the pair's candles, so `trader/shared/market/pair.py` gained `PairCandles`
  and `candles_for` (`PairMarketData` delegates its candles to them); the
  backtest CLI's test seam is `CANDLES` (was `MARKET_DATA`). The double
  signature was checked to give the same bytes before it went. 900 tests
  (9 covered only the removed websocket path; 2 new for `candles_for`).

#### A17. Strategy cleanup — M (done 2026-10-08)

- **Design (2026-10-08).** The review of `trader/strategy/` (about
  2k lines) and the indicators it uses (`trader/shared/indicators.py`):
  1. *Each indicator is described in three places.* The functions are in
     `conditions._INDICATORS`, their names again in `expr.FUNCTIONS`, and
     the bars each needs twice: `expr._lookback`/`_history` and the
     `lookback()`/`history()` of every typed condition in `models.py`. One
     registry in `shared/indicators.py` (`INDICATORS`, `lookback(name,
     window)`, `history(name, window)`) that all three read; a new
     indicator is one entry there. `moving_average` and `pct_change` (only
     tests call them) go, with the stale "`market summary`" docstring.
  2. *The bot's name.* `AsyncWebsocketTradingBot` in `async_websocket_bot.py`
     reads a `MarketData` feed (the hub, through `connect`); it has opened
     no websocket since B7. It becomes `TradingBot` in `bot/loop.py` (the
     `bot` logger name stays).
  3. *Small:* a Rich markup tag left open in `log_placed_order`
     (`[gray]...[gray]`); an unused `self.logger` alias in `SpecStrategy`;
     the `models.py` docstring still calls `expr` a future condition.

  Seen and left: `decision.py`, `config.py`, `runner.py` and the remote
  client read well; `SpecStrategy` keeps its default `datetime.now` clock
  (`to_utc` normalizes it, and `connect` never injects one).
- **Result.** As designed; no behaviour, schema, wire or spec-id change
  (the pinned example ids hold: every `lookback()`/`history()` gives the
  numbers it gave before). A new test checks each registry entry against its
  own function: no value one bar before its `lookback`, a value at it. The
  tests that patch the bot use `trader.strategy.runner.TradingBot`. 900
  tests (2 for the removed helpers, 2 new for the registry).

#### A17b. Strategy cleanup, second pass — S (done 2026-10-08)

- **Design (2026-10-08).** A second read of
  `trader/strategy/` and the `shared/` modules it uses (feed, pair, spec
  terms and validation, the wire, `Order`/`Position`):
  1. *Orders cross the wire as JSON inside JSON.* `wire.py` turns an `Order`
     into a JSON string and parses it back into a dict (and the reverse on
     the way in). `order_to_dict`/`order_from_dict` in `models/order.py`;
     the JSON codec (ledger) wraps them, the wire uses them directly.
  2. *A dead branch in the bot's position line.* `log_position` handles a
     closed position, but it only ever gets the snapshot's open one; the
     branch goes, and with it `Position.realized_pnl_percent` (only its
     tests used it).

  Seen and left: the bot loop, `decision.py`, the remote client, the spec
  terms and their validation read well after A17.
- **Result.** As designed. `order_to_json` now dumps `order_to_dict`, and
  the JSON it writes is byte for byte the old one (checked on a plain, a
  costed and a perp order), so ledger rows and the wire don't change. 902
  tests (2 new in `tests/trader/shared/models/test_order_codec.py`).

#### A11c. Perp run 1 fixes (F1-F5) — M (done 2026-10-08)

- **Plan (owner, 2026-10-08):** before the long run.
  - **Design.**
    - *F1, position rent.* Before sending an open, `JupiterPerpsVenue` reads
      whether the position account exists (pre-send: a failed read stops
      the open). If it doesn't, the open creates it, and after the keeper
      fills, the account's lamports (a new `JupiterPerpsReader.lamports`,
      never raising: a failed read books the rent as unknown, 0, with a
      warning) go into the leg's `TradeCosts.rent_lamports`, carried on the
      `ExecutionResult` and kept by `fetch_costs`. It is a cost of the
      first round trip, as a token account's rent is for spot (A15). The
      account is never closed (Jupiter keeps the PDA and reuses it), so
      there is no refund to track.
    - *F2, the stop's fee.* `PerpAccount` keeps the signature its stop send
      announced, reads its fee (`PerpVenue.fetch_failed_fees`, which only
      reads fees and never raises) and books a `perp_stop_fee` event
      (`account`, `signature`, `fee_lamports`, `fee_usd`). `pnl_totals`
      adds it to `fee_lamports`/`costs_usd` and takes it from `net_usd`;
      the book takes it from `realized_usd` (`PositionBook.charge_cost`),
      so the budget sees it. Paper places no stop and books nothing. A12's
      "venue stop as its own intent" replaces this event.
    - *F3, `stop_left`.* After a close the venue reads the stop request, waits
      2 s once if it is still there (the keeper cleared it in 1 s in run 1),
      and reports it only if it is there on the second read.
    - *F4, the fee split.* A perp leg's fee is `meta.fee`; its priority part
      is what passes the one signature's base fee (as `parse_swap_costs`).
    - *F5, a refused `hello`.* `HelloRefusedError` moves to the shared
      protocol (the bot only knows that); the bot loop stops on it with an
      ERROR and a Telegram message, instead of retrying. The one refusal that
      passes by itself, the spec's old session still alive on `serve`
      (`SpecConnectedError`, listed in `HELLO_RETRY_KINDS`), reaches the
      client as a dropped connection and is retried.
- **Result.** As designed, then two reviews.
  - **`/code-review`** found:
    - a refused `hello` on a mid-run reconnect would have stopped the bot
      and its stop-loss for good; now only a permanent refusal stops it,
      and the old-session case is retried (F5's split);
    - post-fill reads in `open_perp`/`close_perp` (the borrow rate, the
      USDC that came back) could raise after the keeper filled and leave a
      real position FAILED; one never-raising `_after_fill` now covers them,
      with an estimate at the oracle price for the close;
    - the D7 close now comes before the stop fee is booked.

    The rent of a first open that the keeper rejects is left to A12.
  - **`/simplify`:**
    - `lamports_usd` and `priority_fee_lamports` live in
      `shared/models/costs.py` (the failed-fee path, the stop fee, spot swap
      costs and the perp venue use them);
    - the stop fee is booked from one place with the open's SOL price;
    - the reader's two `getMultipleAccounts` share `_multiple`;
    - `RoundTripCosts` has one signed `charge` (the rent refund is a
      negative charge);
    - `AccountPnL._known` holds the "no SOL price" rule;
    - the stop check is read, wait 2 s, read;
    - moving the check into the sweep and the stop as its own intent stay
      in A12.
  - **Tests:** 910, the CI gate and the live perps suite green (a live
    check for `reader.lamports`).
  - **Before the long run:** restart `serve real` and `connect` (the wire
    gained the `SpecConnectedError` kind).

#### A12. Perps: hardening — L (done 2026-10-09)

Oracle staleness (entries denied on a stale venue
oracle); partial closes; adding collateral; shared perp state reads through
the price hub. From the A11b `/simplify` review (left out of A11b, which
waits for the owner's run):
- the venue stop as its own gateway intent (key `{key}:stop`): idempotency,
  `intent_sent` and the resolver for free, instead of the `announce`
  callback on `PerpVenue.place_stop` and the `perp_stop_sent` event;
- `stop_left` reads the stop address from the ledger's `perp_stop_placed`
  (the venue's in-memory `_stops` is lost on a restart), and cancelling it
  from the bot;
- the A11a `reconcile_perps` (unused) run once at the first open in one
  batched read, replacing the per-bucket `_check_venue_position`;
- a typed perp payload on `SentTx` instead of addresses in the mint fields
  (needed by the perp resolver);
- compute budgeting inside `OnChainExecutor.send_instructions` (one
  simulation per send; the rent close would get it too);
- one mode branch in `wiring` that builds the spot provider and the perp
  venue together;
- paper venue stops that fire in the sweep, so paper runs the D7 path;
- one reading of a signature's status (owner, 2026-10-08, from A16b):
  `AsyncRPCClient.check_signature_is_confirmed` (the wait after a send)
  and `OnChainExecutor._status_outcome` (the resolver) each turn a status
  into confirmed/failed/pending; one `TxOutcome` mapping for both, with
  the confirmation tests (`test_swap_confirmation.py`) moved onto it;
- the position account's rent when the first open is rejected (from the
  A11c review): our request transaction creates the account even when the
  keeper then drops the request, so the rent is paid on a REJECTED intent
  and later opens book 0. Book it from the request transaction itself
  (its `meta` shows the new account), not from the open that fills.
- **Scope (owner, 2026-10-09):** everything above in one item, the A3
  resolver for perp intents included; partial closes and adding
  collateral come in, so §8.1's "changing collateral of an open
  position" leaves the out-of-scope list. Size: **L**.
- **Design (2026-10-09).** Ledger schema, wire ops and the ids of
  `docs/examples/` don't change (new spec fields are left out of
  `canonical_json` when unset; new intent sides are values in the
  existing `side` column; new payload fields are optional).
  - **Two new intent sides.** `STOP` (a venue order placed or cancelled,
    no money moves) and `ADD` (collateral added to an open perp).
    `legs_since_last_buy` reads `BUY`, `SELL` and `ADD`: an `ADD` grows
    the entry's `quote_amount` and collateral (`positions.py`), so PnL
    stays "back - posted". The policy gives `STOP` the send rules only
    (real mode, unresolved, breaker) and `ADD` the budget rules like a
    buy; trade counts and the daily notional count `BUY` and `ADD`.
  - **Venue stop as an intent (`{key}:stop`, side `STOP`).** `PerpAccount`
    places it through `execute_trade` (idempotency, policy, `intent_sent`
    written first, the resolver), so the `announce` callback and
    `perp_stop_sent` go; the stop fee is booked like any leg's
    (`fetch_costs`, the leg's `fee_lamports`; `perp_stop_fee` stays
    readable in old ledgers). `PerpVenue.place_stop(terms, fill, key)`
    returns the result with the request address. Failing still closes
    the position (D7). `perp_stop_placed` keeps the address.
  - **Leftover stop.** `stop_left(terms, address)` takes the address of
    the bucket's last `perp_stop_placed` from the ledger
    (`Ledger.last_stop_request`), so a restart doesn't lose it; one still
    there after the wait is cancelled at once by a `{key}:cancel` intent
    (side `STOP`, `closePositionRequest2`, added to the vendored IDL; the
    live suite simulates it). `perp_stop_left` is written only if the
    cancel fails (the owner cancels in the UI).
  - **Typed `SentTx.perp`** (`PerpSend`: `kind` open/close/stop/cancel/add,
    `request`, `position`, and for paper the `fill`): the mint fields go
    back to mints; `sends_of` rebuilds it (old payloads have none).
  - **Perp resolver.** `IntentResolver` gets the mode's `PerpVenue`. For a
    perp intent the sends are checked as for swaps (`Venue.send_outcome`:
    the signature on chain, or the paper wallet's `applied` log); a LANDED
    send asks `PerpVenue.resolve_send(sent, terms)`: real reads the
    request and position accounts (request still there: pending; gone and
    the position changed as asked: executed, the fill from the position or
    the keeper's payout; gone and unchanged: rejected, the request's fee
    and rent booked); paper rebuilds the result from the logged fill.
    `STOP` intents resolve to EXECUTED/FAILED with no fill.
  - **Compute budget inside `send_instructions`.** Sign at the 200k limit,
    run the one balance simulation (it returns `unitsConsumed`), then
    re-sign locally with `units x 1.3` (floor 100k) and the unit price
    from the caller's `ComputeBudget` (recent fee accounts and a floor; the
    rent close passes none: no priority). The perps venue stops simulating
    on its own reader.
  - **Rent and fee of a request from its transaction.** `fetch_costs` (fill)
    and `fetch_failed_fees` (rejected: the `SwapRejectedError` carries the
    landed request's signature) read the request transaction once: its fee
    plus the rent of a position account it created (`meta` pre balance 0,
    post > 0, at one of the wallet's position PDAs). Replaces the lamports
    read after the fill.
  - **One mode branch in `wiring`**: `_venues(mode)` builds the spot
    provider and the perp venue together.
  - **Shared perp reads (`PerpsFeed`, `market/perps/feed.py`).** One per
    `serve`, the perps counterpart of the hub: the custody (borrow rate)
    cached 60 s and the Doves price cached 1 s, shared by the paper borrow
    rates and the real venue. **Stale oracle:** a real open whose Doves
    price is older than 30 s (`MAX_ORACLE_AGE`) is refused before
    anything is sent (`StalePriceError`; paper already refuses a stale hub
    price).
  - **Paper venue stops.** `SimulatedPerpsVenue.place_stop` keeps the level
    in the position's record; `liquidations()` (the sweep, and every tick
    in a backtest) reports a crossed stop as a venue exit at that price,
    paid back on `acknowledge` (the pending exit is held until then).
  - **Batched reconcile.** At the first open, `PerpVenue.open_markets()`
    (real: every market and side PDA in one `getMultipleAccounts`; paper:
    the wallet's `perps`) against every ledger bucket's open perp, through
    `reconcile_perps`: each mismatch is a `perp_mismatch` event; one the
    venue has and the ledger doesn't blocks perp buys in that market and
    side for the process (one the ledger has and the venue doesn't is
    booked by the sweep as a venue exit when its bucket opens). Replaces
    `_check_venue_position`.
  - **Partial closes.** Spec: `exit.partial = {"pct": 50, "mode": "any",
    "conditions": [...]}` (spot and perp), fired at most once per
    position, after the stop and the full exit had their turn; it sells
    `pct`% of the entry. `PerpAccount.sell` honours a smaller quantity:
    real sends `createDecreasePositionMarketRequest` with `sizeUsdDelta`
    and `collateralUsdDelta` at that fraction (`entirePosition` false);
    paper scales the position record. The book's partial settle already
    handles the rest. The venue stop (whole position) stays.
  - **Adding collateral.** Spec: `market.add_collateral = {"within_pct": 5,
    "usd": 2, "max_times": 1}`: when the price is within `within_pct`% of
    the liquidation price, `serve`'s sweep (and the backtest each tick)
    adds `usd` of collateral, at most `max_times` per position, inside the
    bucket's budget (`ADD` intent, policy budget rules). Real: an increase
    request with `sizeUsdDelta` 0; paper: the record's collateral grows
    and its liquidation price moves. `TradeService.check_liquidations`
    becomes `check_perps` (exits, then top-ups).
  - **Tests:** each step's unit tests (fakes for the RPC/reader), the perp
    resolver for open/close/stop in real (fake reader) and paper, partial
    and top-up through `PerpAccount` and the backtest, the policy for the
    new sides, `canonical_json` unchanged for every example; live: the
    cancel, the partial close and the top-up requests simulated on
    mainnet.
- **Built (2026-10-09).** As designed, with these differences:
  - the community IDL's `closePositionRequest` no longer exists on chain
    (Anchor error 101 in the simulation): the program's own on-chain IDL
    (at its `anchor:idl` address) has `closePositionRequest2` (no
    arguments; also the mint, system and ATA programs), now vendored and
    simulated in the live suite;
  - `reconcile.py` moved to `trader/execution/models/` (the service may
    not import the venue layer), keyed by (market, side);
  - `stop_level` lives in `execution/models/perp.py` (both venues use it)
    and falls back to half the distance to liquidation without a spec
    stop; a paper stop is placed and pays one network fee like a real
    one (the backtest charges it too);
  - `Order.reduced` (default false, out of old JSON) marks an entry that
    had a partial sell: the strategy fires `exit.partial` once per
    position, restarts included;
  - a perp sell's `quantity` now matters: less than the position (beyond
    the 1% dust rule) is a partial close;
  - `add_collateral` fires only before the venue stop: the stop sits at
    most half way to liquidation, so `within_pct` must be wider than
    that distance (the docs say so); borrow drift is what it is for.
  Tests: 948 (`test_perps_venue.py` rewritten; new tests in
  `test_perp_buckets.py`, `test_paper_perps.py`, `test_perps_feed.py`,
  `test_send_instructions.py`, `test_resolve.py`, `test_policy.py`,
  `test_perp_spec.py`, `test_spec_strategy.py`, `test_perp_backtest.py`);
  live perps 8 (3 new), all passing on 2026-10-09.
- **Reviews (2026-10-09).**
  - **`/code-review`** found, all fixed:
    - a resolved real close was booked with size 0 (the whole payout as
      profit): the close's send now logs the size it takes;
    - a resolved top-up or partial close could book a keeper rejection as
      filled: `PerpSend.before` holds the size or collateral before the send,
      and one `_done` check serves the keeper wait and the resolver;
    - the real `held()` ignored the borrow the position owes, so a top-up
      could never fire on borrow drift: it is size x (the collateral
      custody's cumulative rate - the position's snapshot) / 1e9 (USDC for a
      short; a live check reads it);
    - the resolver wrote `perp_stop_placed` for a cancel and never booked a
      resolved stop's fee;
    - `top_up` read the venue before the cheap checks.
    Skipped: paper `acknowledge` pays back a pending exit even if the bucket
    booked nothing (needs an inconsistent restore).
  - **`/simplify`:**
    - `with_collateral` (with the venue's close fee) covers paper's top-up and
      `held`;
    - `PerpTerms.top_up` is the spec's `AddCollateral`;
    - one `record_venue_order` (`gateway/fills.py`) for the account and the
      resolver;
    - one `_payout` for venue exits and resolved closes;
    - `PerpSend.fraction` is gone;
    - open and top-up gather their independent reads;
    - the cost read retries while the RPC indexes the transaction (12 s);
    - the top-up cap is read only when collateral goes in;
    - also: `TRADE_SIDES`, `Ledger.open_perp_markets`, the feed's `_cached`
      and the hub's `MAX_AGE_SECONDS`, one compile for `resign` and
      `sign_instructions`, and the reader's unused `borrow_bps_hour` removed.
    Left as debt (backlog): the altitude review's deeper changes.
  - **Tests:** 957, the CI gate green; live perps 9, green (open, close,
    stop, partial close, top-up and cancel simulated on mainnet, and the
    owed borrow); a paper `/smoke` of a spot spec and of a perp spec with a
    partial exit and `add_collateral` clean.
  - **Before the next real run:** restart `serve real` and `connect`
    together (the wire's `Order` gained `reduced`, the terms' `market`
    gained `add_collateral`).

#### A18. Borrow rate of a short — S (done 2026-10-09)

Paper (`SimulatedPerpsVenue`'s
borrow rate) and the real entry fill (`JupiterPerpsVenue.open_perp`) read
the market custody's rate (SOL) for every perp, but a short borrows from
the collateral custody (USDC); `collateral_mint(terms)` already picks the
right one and `held()` uses it. Both read `collateral_mint(terms)`'s
custody through `PerpsFeed`. Tests: a paper short and long pick different
custodies; live: the USDC custody's `borrow_bps_hour` is sane. Paper and
backtest borrow costs of shorts change (the backtest's `--borrow-bps-hour`
stays one number for either side).
- **Design (2026-10-09).** `collateral_mint(terms)`
  (`market/perps/reader.py`) is the custody a position borrows from: the
  market's own token for a long, USDC for a short (Jupiter's long swaps
  its USDC into SOL collateral). `SimulatedPerpsVenue.open_perp` asks
  `_borrow_rate(collateral_mint(terms))` (the 5-minute cache is per
  custody, so a long and a short each keep their own) and
  `JupiterPerpsVenue.open_perp` reads `feed.borrow_bps_hour` of the same
  mint for the entry fill's `borrow_bps_hour`. The replay is unchanged
  (no live rates: `--borrow-bps-hour`). No schema, wire or spec change.
- **Built (2026-10-09).** As designed. In both venues the helper is
  imported as `borrowed_from` (`open_perp`'s `collateral_mint` parameter
  would shadow it). Tests: 959 (a paper short and long ask different
  custodies; the real entry records the USDC custody's rate); live: the USDC
  custody decodes with a sane borrow rate. A paper `/smoke` of a 3x short
  was clean and opened at 0.020 bps/h (the USDC custody's live rate that
  day).

#### A19. Token account rent in replays — S (done 2026-10-09)

Live, the first buy of a token
pays its account's rent (2,039,280 lamports) and the sweep refunds it when
the bucket retires flat (A15); `ReplayExecutor` passes
`account_rent_lamports=0`. The replay charges it like paper does (the first
buy of a token the replay wallet has no account for) and, when the replay
ends with no open position, books the refund as the sweep would at expiry.
The summary and `--json` show both (`rent_usd`, `rent_refund_usd`), and
`round_trip_costs` nets them as live (`live_vs_backtest.py` gets the same
view). Tests: a spot round trip pays the rent once and gets it back when
flat at the end; a perp pays none (no token account).
- **Design (2026-10-09).** The replay wallet holds no SOL, so the rent is
  a leg cost like the network fee (A14), not a SOL debit:
  - `ReplayExecutor.execute` asks the wallet whether the output needs an
    account (`needs_account`, before the swap opens it) and, if so, puts
    `DEFAULT_ACCOUNT_RENT_LAMPORTS` in the leg's `rent_lamports`; the
    ledger and the book carry it into the round trip's net PnL as live,
    and `fees_usd` (which the replay equity subtracts) adds its USD at the
    tick's SOL price. SOL and the quote token never pay (native, funded).
  - At the end of the replay, a flat bucket is retired and closed through
    the live path: `TradeService.retire` then `close_token_account`
    (`RentRefunds`: the payer from the ledger, `rent_refund_sent` and
    `rent_refund` events, the book restored). `ReplayExecutor` overrides
    `close_token_account` to refund that rent less the base fee (5,000
    lamports) and takes the net off `fees_usd`. A bucket still holding the
    token (or dust) keeps it, as live.
  - `BacktestResult` gains `rent_usd` (paid) and `rent_refund_usd`; the
    summary prints them when non-zero, `--json` has them, and
    `round_trip_costs` nets them through the same events as live.
  - Also: `RentRefunds.resolve` rebuilds the logged send with
    `SentTx.from_payload` (A12 added `SentTx.perp`, so a pending refund
    logged before A12 would have raised `KeyError`).
  No schema, wire or spec change.
- **Built (2026-10-09).** As designed, with two details:
  - the close doesn't touch the replay wallet (a SOL-quote pair would have
    counted the refund twice: the wallet's SOL and `fees_usd`), and
    `ReplayExecutor.fetch_fee` gives the close the base fee
    (`replay-close-` signatures);
  - the end-of-replay retire and close log at ERROR only (the service's and
    the rent's loggers), so a backtest's output stays quiet.

  The two JUP-SOL replay tests now include the rent (paid at 200 USD/SOL,
  back at 210 less the fee). Tests: 962 (a replay that ends holding the
  token keeps its account; a perp pays no rent; the summary line; a
  `rent_refund_sent` logged before A12 resolves). `backtest` of
  `spec-jup-sol-expr.json`: 0.2264 USD paid, 0.2229 USD back. Spot and
  perp paper `/smoke`s clean.

#### A20. Perps cleanup from the A12 reviews — M (done 2026-10-09)

No behaviour change, no
schema or wire change:
- the live keeper wait polls `resolve_send` until it isn't None, so "did
  the keeper execute" and the fill have one path (the resolver's), and the
  venue's `_wait`/`_done` pair goes;
- what each intent side means (spends, moves the position, counts as a
  trade, its policy rules) is declared once on `IntentSide`, and
  `POSITION_SIDES`/`SPENDING_SIDES`/`TRADE_SIDES`, `_rules_for` and the SQL
  `IN` lists derive from it;
- a stop placement and its cancel are told apart by the intent, not the
  send log or the key suffix: a `TradeIntent.venue_action`
  (`place_stop`/`cancel_stop`, stored in `perp_json`, absent on old rows);
- "a partial exit already fired" and "top-ups so far" come from one leg
  history of the open position (`Position.legs`, rebuilt by
  `open_entry`), replacing `Order.reduced` (the wire field goes: restart
  `serve` and `connect` together) and the ledger count in `top_up`;
- the stale-oracle refusal moves above the intent
  (`PerpVenue.fresh_price`, checked by `PerpAccount.buy` like the hub's
  `StalePriceError`), so a stale tick leaves no REJECTED row;
- one position read per sweep: `liquidations()` returns what it read and
  `top_up_perps` uses it, instead of `held()` per bucket.
- **Design (2026-10-09).**
  - **One keeper check.** `JupiterPerpsVenue` gets `_outcome(sent, terms,
    before)`: a venue order (stop, cancel) is done once it landed;
    otherwise one read of the request and the position decides: changed as
    the send asked (`_done`, checked first): the fill; request still there:
    None (wait); request gone and unchanged: rejected, with the send's
    signature (its fee and rent are the bucket's). The live send polls it
    (`_wait`, 60 s, then UNCONFIRMED) and `resolve_send` calls it once. The
    fill has one builder; only a close's payout has two sources: live,
    the wallet's USDC before and after (`before` carries that balance and
    the position read before the send, for the fee split); resolved, the
    keeper's payout on the position (`last_payout`), fees unknown.
  - **Intent sides declared once.** `IntentSide` gets properties (`spends`,
    `moves_position`, `is_trade`); `POSITION_SIDES`, `SPENDING_SIDES`,
    `TRADE_SIDES` and the policy's `_rules_for` derive from them (an order
    on the venue: the send rules only).
  - **Placing and cancelling a stop are two sides**: `STOP` and a new
    `CANCEL` (instead of the planned `venue_action` field: a side needs no
    storage and the policy, SQL and resolver already branch on it). The
    account picks the side; the resolver writes `perp_stop_placed` for
    `STOP` only, without reading the send log; the key suffix decides
    nothing.
  - **Leg counts on the position.** `Position` gains `partial_sells` and
    `top_ups` (since the entry): `PositionBook.reduce` counts a sell that
    asked for less than the position (its `requested_quantity` under the
    1% dust rule, so a full exit that fills short is not one),
    `PositionBook.add` counts a top-up, and the restore rebuilds both from
    the legs (`positions.open_position`; a sell intent with
    `closes_position` false counts). `AccountState.open_position` carries
    them (`open_entry` stays, derived). The wire's position gains both
    (missing: 0). `Order.reduced` goes; `exit.partial` reads
    `partial_sells`, `add_collateral` reads `top_ups`.
  - **Stale oracle above the intent.** `PerpVenue.check_fresh(terms)`:
    real reads `PerpsFeed.fresh_price` (`StaleOracleError`, a
    `SwapRejectedError`: a rejection reply, no ledger row); paper has
    nothing to do (the hub's `StalePriceError` already stops the buy at its
    USD snapshot, before the intent). `PerpAccount.buy` calls it first;
    `open_perp` keeps its own check for a price gone stale in between.
  - **One venue read per sweep.** `PerpVenue.sweep(open_) -> PerpSweep`
    (`exits`: the positions the venue closed; `held`: the others as
    `PerpFill`s with the liquidation of now) replaces `liquidations` and
    `held`. `TradeService.check_perps` takes the lock once, books the exits,
    then tops up from `held` (`PerpAccount.top_up(price, held, limit)`).
  - Restart `serve` and `connect` together (the wire's `Order.reduced`
    goes, the position's counts come). No schema change.
- **Built (2026-10-09).** As designed, in five steps with the suite green
  after each:
  - `IntentSide.spends`/`moves_position`/`is_trade`, the tuples and
    `_rules_for` from them;
  - the `CANCEL` side (the account passes the side; the resolver drops the
    send it no longer needs);
  - `Position.partial_sells`/`top_ups` (`book.rest_of` and
    `asked_for_part` hold the rule, `positions.open_position` rebuilds
    them; `PositionBook.restored` takes the position);
  - `PerpVenue.sweep`/`PerpSweep` and `check_fresh` (`check_liquidations`,
    `top_up_perps`, `liquidations` and `held` are gone);
  - `JupiterPerpsVenue._outcome` (with `_fill`, `_close_fill` and
    `_Before`), polled by `_wait` and called once by `resolve_send`; `_send`
    returns the logged `SentTx`.

  Tests: 966 (a full exit that fills short isn't a partial; the counts over
  the wire and from an old `serve`; a stale oracle leaves no intent; the
  sweep's owed borrow). Paper `/smoke`s of a spot spec and of a perp spec
  with a partial exit: clean.
