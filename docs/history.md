# History

Finished roadmap stages, moved out of [`plan.md`](plan.md) on 2026-09-30 so the
plan only carries open work. The text is kept as written at the time; later
stages changed some of it (stage U removed dry mode, the manual swap, the
`ledger`/`pnl`/`paper`/agent commands and the kill switch). For how the code
works today, read [`architecture.md`](architecture.md).

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

## Stage order (stage A)

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
