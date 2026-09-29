# Architecture: a guided tour

This is a step-by-step walk through the project for a human reader. Read it
top to bottom:

- Steps 1–3 give you the vocabulary and the map.
- Steps 4–7 follow one trade from the command line to the blockchain and back.
- Steps 8–10 cover the other paths (modes, backtest), where the state lives, and
  the safety nets.
- Step 11 is the architecture this project is moving toward.

The code is the source of truth; this document names functions rather than line
numbers so it doesn't go stale. The roadmap, progress and open issues live in
[`plan.md`](plan.md).

---

## Step 1 — What the bot is

A long-only trading bot for Solana tokens. It swaps through the **Jupiter** DEX
aggregator. A **strategy** looks at every price tick and says "buy", "sell" or
nothing. Every order then passes through a **policy** (risk limits the owner
sets) and is written to a **ledger** (a SQLite audit log) before and after it
executes.

It holds at most one position per pair: it buys the token with a stablecoin,
then later sells all of it back.

## Step 2 — Vocabulary

| Term | Meaning |
|---|---|
| **Mint** | A token's on-chain address. Known tokens are listed in `SOLANA_MINTS` (`trader/models/mints.py`) with their symbol and decimals. |
| **Symbol / pair** | `OUTPUT-INPUT`. `SOL-USDC` means "buy SOL, paying with USDC". The input is what you hold, the output is what you buy. |
| **UI vs raw amount** | `1.5` USDC (UI) is `1500000` raw (6 decimals). `Mint.ui_to_raw` / `raw_to_ui` convert between them. On-chain and Jupiter use raw amounts. |
| **Mode** | `real` (signs and sends), `dry` (real wallet; simulates the transaction, never sends), `paper` (simulated wallet file, real Jupiter quotes, no key needed). |
| **Signal** | `OrderSignal(side, quantity)`: what a strategy returns. |
| **Intent** | `TradeIntent`: a request to spend X of one mint for another. Every swap starts as one. |
| **Order / Position** | `Order` is an executed fill. `Position` is an entry order plus, eventually, an exit order. It computes PnL. |
| **Ledger** | SQLite database per mode: an `intents` table plus an append-only, hash-chained `events` table. |
| **Policy** | The owner's limits (`policy.toml`): per-trade and daily USD caps, trades per hour, loss stop, allowed symbols, real mode on/off. |

## Step 3 — The folder map

```
main.py                         CLI (Typer): run, swap, backtest, pnl, halt, resume, ledger ..., paper ..., market ..., strategy ...
policy.toml / policy.example.toml   owner's risk policy (TOML)
trader/
  __init__.py                   empty on purpose: every `import trader.x` loads it
  strategies_registry.py        STRATEGIES used by the CLI (random, target_value, composer, spec)
  paths.py                      data_dir() and policy_file(): where all state lives
  wiring.py                     the only place that turns a mode into components: key, provider, gateway, trade service
  indicators.py                 pure Decimal indicators (sma/ema/wma/rsi/volatility/...) + BarSeries
  logging_config.py             console + file logging setup
  models/                       plain data types, no I/O
    mints.py                    SOLANA_MINTS registry, Mint (decimals, UI/raw conversion)
    order.py                    OrderSide, OrderSignal, SwapResult, Order
    position.py                 Position and its PnL math
    book.py                     PositionBook: a bucket's open position and realized PnL totals
    intent.py                   TradeIntent, PolicyDecision, IntentRecord, IntentStatus
    costs.py                    TradeCosts (network fee, rent), PnLResult, trade_rates
    public_data.py              TickerData (a candle), Interval (15s / 1m / 1h timeframes)
    account_data.py             MintBalance
    mode.py                     RunningMode (real / dry / paper)
    errors.py                   SwapRejectedError, TransactionSubmittedError (shared by every layer)
  trading_strategy.py           TradingStrategy base + Random, TargetValue, WMA, TrailingStop, TargetPercent, StrategyComposer
  strategy_spec/                declarative strategies written by agents (JSON specs)
    models.py                   StrategySpec + the condition catalogue (pydantic), spec_id()
    conditions.py               IndicatorBank, TickContext, PREDICATES (one pure function per condition type)
    strategy.py                 SpecStrategy: runs a spec as a TradingStrategy
    validate.py                 parse_spec, validate(spec, SpecLimits)
  market/data.py                MarketData protocol + JupiterMarketData (read-only: no key, no RPC)
  market/prices.py              PriceOracle + JupiterPriceOracle (Price API V3) + usd_snapshot (never raises)
  agent_api/                    what agents call: market.py, strategies.py, cli.py (JSON output), output.py
  bot/                          strategy side: knows no mode, key, ledger or provider
    config.py                   BotConfig(name, symbol, strategy, market, trader, notifier, on_tick)
    async_websocket_bot.py      the loop: price (MarketData) -> strategy -> order (TradeClient)
  trading_service/              the seam between strategies and execution
    protocol.py                 BucketSnapshot, OrderRequest, OrderReply (plain data)
    client.py                   TradeClient protocol (one bucket): open / bucket / submit / aclose
    service.py                  TradeService: buckets (budget caps), serialized orders, reply classification
    local.py                    LocalTradeClient: TradeClient for a TradeService in the same process
  async_account.py              AsyncAccount: one bucket's balances, sizing and intents; fill -> Order; its book is models/book.py
  execution/gateway.py          TradeGateway (the only path to a swap and to the ledger) + KillSwitch
  execution/fills.py            execute_trade: gateway, then costs -> Fill (every trade)
  policy/policy.py              Policy, pure evaluate(), TOML loader
  ledger/ledger.py              Ledger (SQLite): intents, events, PnL reports, restore
  providers/jupiter/
    async_jupiter_client.py     Jupiter HTTP (quote, swap tx, candles) + price websocket
    candles.py                  raw datapi candles -> TickerData (shared by market data and the provider)
    async_rpc_client.py         Solana RPC (Helius): balances, sign, simulate, send, confirm
    async_jupiter_svc.py        AsyncJupiterProvider: quotes, price-impact cap, retries, unit conversion, costs
    executor.py                 Executor protocol + OnChainExecutor (swap tx, sign, simulate, send, confirm)
    swap_costs.py               reads the real amounts and fees from a confirmed transaction
    jupiter_data.py             Jupiter response dataclasses
  paper/                        SimulatedWallet (.data/paper-wallet.json) + SimulatedExecutor + paper_provider()
  backtest/                     tick recording/loading + Backtester (replays ticks with synthetic quotes)
  notification/                 Null / Telegram notifications
tests/                          mirrors trader/ (tests/trader/...), plus tests/strategies and tests/test_main.py
docs/                           plan.md (roadmap), this file, examples/
```

## Step 4 — Starting the bot (`main.py run`)

Example: `uv run main.py run paper SOL-USDC random 'sell_chance=20 buy_chance=40'`

`main.run` builds two separate sides and connects them:

1. **The strategy.** `_get_strategy_obj(name, args)` looks the name up in
   `STRATEGIES` (`trader/strategies_registry.py`) and builds it from the
   `key=value` arguments. A spec must match the pair.
2. **The execution side.** `build_trade_service(mode)` (`trader/wiring.py`)
   is the only place that looks at the mode. It builds:
   - a **provider** (`build_provider`):
     - `paper`: `paper_provider(wallet)`, a `SimulatedExecutor` over the
       `SimulatedWallet`, with no key;
     - otherwise: `AsyncJupiterProvider.on_chain(keypair, is_dryrun=...)`,
       where the key comes from `SOLANA_PRIVATE_KEY`;
   - a **gateway** (`TradeGateway.for_mode(mode)`), made of the mode's
     ledger (`data_dir()/ledger-<mode>.sqlite3`), the mode's policy and the
     `KillSwitch` (`data_dir()/HALT`);
   - a **`TradeService`** over the two.
3. **The connection.** A `LocalTradeClient` binds the service to one
   **bucket** named after the pair. Its ledger account stays
   `"<mode>:<pair>"`, as it always was.
4. **The bot.** `BotConfig(name, symbol, strategy, market=JupiterMarketData(),
   trader=<the client>, notifier, on_tick)` goes into
   `AsyncWebsocketTradingBot(config).run()`. The bot gets a price source and
   a trade client, and **never** a mode, key, provider or ledger.

## Step 5 — Startup and the tick loop (`trader/bot/async_websocket_bot.py`)

**Startup (`_startup`):**
1. `trader.open()` opens the bucket on the execution side
   (`TradeService.open_bucket`).
   - It restores the open position and the PnL totals through
     `gateway.restore(account)`, so a restart does not forget a position.
   - When the venue's balances reflect our own fills (real and paper; not
     dry), it reconciles the position against the wallet. A mismatch is
     logged as an event.
2. `strategy.setup(candles)` warms the strategy up. The candles come from
   `market.get_candles(...)`, sized by `strategy.warmup()` when the strategy
   has one (specs do); otherwise 100 15-second candles.

**Each tick (`_tick` → `process_market_data`):**
1. `market.get_price(output_mint)` reads the next price from the Jupiter
   websocket (in USD). If the websocket fails or is quiet for 30s, the price
   comes from the documented Price API V3 instead, so the loop (and its
   stops) keeps running.
2. `trader.bucket()` returns a `BucketSnapshot`: what the bucket may spend
   (`available_usd`), the position, the realized PnL and the status. A
   `retiring` status stops the bot.
3. `strategy.on_market_refresh(price, spread, available_usd, position)`
   returns an `OrderSignal` or `None`.
4. If there is a signal and orders are not paused, the bot calls
   `trader.submit(OrderRequest(...))`, which returns an `OrderReply`, and
   sleeps 2s after a fill so the wallet can settle.

**Replies and errors:**
- A `denied` reply (policy, duplicate) or a `rejected` one (balance, budget,
  price impact) pauses new orders for 30s (`denial_cooldown`). The strategy
  keeps getting prices.
- An `error` reply raises `TradeServiceError`. That error, and any other
  error in the loop, retries with exponential backoff from 1s up to 60s.

## Step 5b — Buckets (`trader/trading_service/service.py`)

A bucket is one strategy's slice of the wallet: its own `AsyncAccount`,
ledger account (`"<mode>:<name>"`), position and PnL, plus an optional USD
budget.

- **What it may spend:** `available = min(spendable balance, max(0, budget +
  min(0, realized PnL)))`. A realized loss shrinks the budget; a profit does
  not grow it.
- **Shared wallet:** every capped buy re-reads the wallet balance, and orders
  run one at a time (`asyncio.Lock`). So buckets that share a wallet can
  never spend the same USDC twice.
- **Replies:** `submit_order` turns the outcome into an `OrderReply`, so no
  execution exception reaches the strategy side:

  | Outcome | Status |
  |---|---|
  | policy denial / duplicate intent | `denied` |
  | nothing executed (`ValueError`, `SwapRejectedError`) | `rejected` |
  | anything else | `error` |

- **Closing:** `close_bucket` sells the open position, and does nothing if
  there is none.

## Step 6 — From signal to swap

**`AsyncAccount` (`trader/async_account.py`)**, the bucket's account, turns
the order into an intent. Its position and PnL live in `account.book`, a
`PositionBook` (`trader/models/book.py`, plain data):
- **`buy`:**
  - refuses if a position is already open or the balance is below the minimum;
  - caps the amount at the spendable balance and at the bucket cap
    (`spend_cap`). The spendable balance keeps the venue's SOL fee reserve
    when SOL is spent (`provider.native_fee_reserve`: 0.02 SOL on-chain and
    in paper, 0 in backtests);
  - builds a `TradeIntent` that carries the order's `rationale` and, when
    given, its idempotency key.
- **`sell`:**
  - caps the quantity at what the wallet actually holds;
  - uses a fixed idempotency key per position, so one entry can never be sold
    twice.
- **Notional:** the intent's USD value (`notional_usd`). A sell is its
  quantity times the signal's USD price. A buy is the amount spent, times the
  USD price of the token spent (1 for USDC/USDT; otherwise from a Price API
  snapshot, `trader/market/prices.py`). The snapshot is taken **before** the
  trade and never raises; if it has no price, the value is `None` and the
  policy denies the buy by default. The same snapshot gives the SOL price for
  pairs without SOL, so their costs get a USD value.

**`TradeGateway.submit(intent, execute)` (`trader/execution/gateway.py`)** is
the **only** path to a swap:
1. **Idempotency:** if this key already moved funds, it raises
   `DuplicateIntentError`.
2. **Policy:** it calls `evaluate(intent, policy, ledger.policy_state(),
   halted=..., real_mode=...)`, a pure function with no I/O (see the checks
   below).
3. **Ledger:** it records the intent, as denied or as executing. A denial
   raises `PolicyDeniedError`.
4. **Execute:** it runs the swap.
   - **Success:** the intent is marked `EXECUTED`.
   - **Failure before broadcast:** it is marked `FAILED`, which is safe to
     retry.
   - **Failure after broadcast, or Ctrl+C mid-swap:** it is marked
     `UNCONFIRMED`. That blocks **all** trading until the owner runs
     `ledger resolve`.

`evaluate` applies two kinds of checks:
- **Safety checks, for every intent:** kill switch, real mode enabled,
  unresolved intents, circuit breaker, known and allowed mints, positive
  amount.
- **Budget checks, for buys and swaps only:** unknown USD value, max per
  trade, daily notional, trades per hour, daily loss. Sells skip these, so an
  exit is never blocked.

**The provider swap (`AsyncJupiterProvider._do_swap`):**
1. Get a quote from Jupiter. Reject it if the price impact is above
   `max_price_impact_pct` (`SwapRejectedError`, never retried).
2. Hand the quote to the **executor** (`executor.execute(...)`).
   - **`OnChainExecutor`** gets the swap transaction from Jupiter, signs it
     with the keypair, simulates it, sends it and waits for confirmation. A
     failed transaction raises right away.
   - **`SimulatedExecutor`** (paper and backtest) applies the quote to the
     `SimulatedWallet`. It charges the base network fee and the rent of a
     new token account, as on-chain would.

`_do_swap_with_retry` retries only failures **before** the send, with slippage
escalating up to a ceiling. Anything that fails after the send raises
`TransactionSubmittedError` and is never retried, to avoid double execution.
In **dry** mode the RPC client simulates the transaction and never sends it.

## Step 7 — After the swap

1. The gateway marks the intent `EXECUTED`.
2. **Only then** does `execute_trade` (`trader/execution/fills.py`, the one
   pipeline every trade uses) call `provider.fetch_swap_costs(result)`.
   It returns the real network fee, rent, and the real in/out amounts, read
   from the confirmed transaction. It never raises: if it fails, the costs are
   just unknown.
3. The result becomes an `Order`. Real amounts are used when known, otherwise
   the quote's.
4. A buy opens a `Position` in the bucket's `PositionBook`. A sell closes it
   (`book.close`), which computes the realized PnL (gross, costs, and net in
   the quote token, plus a USD estimate) and adds it to the book's totals.
5. `gateway.record_fill(...)` stores the order JSON and the PnL on the
   intent row. That is what `gateway.restore`, `main.py pnl` and `ledger list`
   read later.

   The account never touches the ledger directly. `mark_executed` and
   `record_fill` are deliberately two commits: the costs are fetched between
   them, and the intent must already be `EXECUTED` by then.

## Step 8 — Modes and the other entry points

| | real | dry | paper | backtest |
|---|---|---|---|---|
| Needs key + RPC | yes | yes | no | no |
| Prices | live websocket | live websocket | live websocket | recorded ticks or candles |
| Quotes | Jupiter | Jupiter | Jupiter | synthetic (`ReplayQuoteClient`: tick price minus `fee_bps`) |
| Swap | signed and sent | simulated, never sent | applied to `SimulatedWallet` | applied to an in-memory `SimulatedWallet` |
| Ledger | `ledger-real` | `ledger-dry` | `ledger-paper` | none |

- **`main.py swap`** is a one-off manual swap on any pair. It calls
  `TradeService.swap`, which runs it in the `manual` bucket (ledger account
  `"<mode>:manual"`, no position) under the same lock and through the same
  `execute_trade` pipeline as the strategy buckets
  (`trader/trading_service/manual.py`). The fill, with real amounts and
  costs, is recorded, so manual swaps show up in `pnl` and `ledger list`.
- **`main.py backtest`** runs a strategy over ticks with `Backtester`
  (`trader/backtest/replay.py`).
  - It uses the same `TradeService` → `LocalTradeClient` path as `run`, over
    a paper provider with a synthetic quote client.
  - Its gateway is `TradeGateway.in_memory()`: an in-memory ledger, a policy
    with no limits (ledger timestamps are wall-clock, so real limits would
    trip in seconds), and a kill switch that ignores the live `HALT`.
    Idempotency and the intent lifecycle still run, as live.
  - An optional `budget_usd` applies the same bucket cap as live.
  - The strategy gets the replay clock and a fixed seed, so results are
    reproducible. That is why strategies must use `self.clock()` /
    `self.rng`.
- **`run --record-ticks FILE`** saves live prices so they can be replayed.
- **Agent commands** (`main.py market ...`, `main.py strategy ...`,
  `trader/agent_api/cli.py`) are read-only.
  - They always print one JSON object and never need the key.
  - `market symbols | price | candles | summary` return registry and market
    data. `summary` uses the same `trader/indicators.py` code as the
    strategies.
  - `strategy schema | validate FILE | backtest FILE` check a spec without
    storing it. The backtest uses the spec's timeframe and starts with
    `budget_usd`.
  - Run a spec yourself with `run paper SOL-USDC spec 'file=spec.json'`
    (`docs/examples/spec-sol-dip.json` is an example).

## Step 8b — How a strategy spec works

A spec (`trader/strategy_spec/`) is JSON with the following parts:

- a pair and a timeframe;
- **entry** conditions, combined with `all` or `any`;
- an **exit** with a required **stop** (`stop_loss` or `trailing_stop`) plus
  optional exit conditions;
- **sizing** (`fixed_usd`);
- a **budget**, a max loss, a cooldown and an expiry.

`SpecStrategy` runs it as follows:

1. **Every tick updates the bars.** It updates the bars of the spec's
   timeframe once (`IndicatorBank`). The bar still forming uses the latest
   price, and indicators are cached per tick.
2. **It tracks the position.**
   - The entry price and entry time come from the entry order, which uses the
     account's clock, so this works after a restart and in backtests.
   - The peak for the trailing stop starts at the entry price.
   - The exit time starts the cooldown.
3. **With a position, it checks the exit.** The stop is checked first and is
   **always** OR'd, so `mode: all` can never disable it. Then the exit
   conditions are checked. Exits **never wait for the warm-up**.
4. **Without a position, it checks the entry.** It may enter only when all
   of these hold:
   - the indicators are warm (the largest `lookback()`);
   - the spec has not expired;
   - the cooldown is over;
   - the entry conditions hold.

   The buy spends `min(sizing.usd, balance)`.
5. **Every signal carries a rationale**, for example
   `"spec 1c21270544fc buy: rsi14<30, price<wma50-0%"`.

Each condition type is one small pure function in
`conditions.PREDICATES`. A test checks that every type in the schema has
one.

## Step 9 — Where the state lives

Everything is resolved by `trader/paths.py` and **never depends on the current
directory**:

| File | What |
|---|---|
| `data_dir()/ledger-<mode>.sqlite3` | intents, events, orders, PnL (one file per mode) |
| `data_dir()/HALT` | kill switch; if it exists, nothing trades |
| `data_dir()/paper-wallet.json` | paper balances |
| `policy_file()` (`policy.toml`) | the owner's risk policy |
| `.logs/` | file logs |

`data_dir()` is `TRADER_DATA_DIR` if set, otherwise `<project root>/.data`.

## Step 10 — Safety nets (summary)

- **Kill switch:** `main.py halt` / `resume`. It fails closed: if the flag
  can't be read, the bot assumes it is halted.
- **Circuit breaker:** after N consecutive failures, trading stops until
  `resume`.
- **UNCONFIRMED intents** block trading until someone resolves them by hand.
- **Idempotency keys**, backed by the ledger, prevent double execution across
  restarts.
- **Price-impact cap and slippage ceiling** in the provider.
- **SOL fee reserve** in the account.
- **Real mode is off** until `policy.toml` sets `real_trading_enabled = true`.
- **Hash-chained events:** `ledger verify` detects tampering.

## Step 11 — Where the architecture is going

Details and progress are in [`plan.md`](plan.md). In short:

- **AI agents author strategies, they never trade.** An agent writes a
  declarative JSON **strategy spec**. It gets validated and backtested, and
  then it runs automatically in paper mode. Real mode needs the owner's
  approval.
- **Strategy and execution are already separated in one process**:

  ```
  strategy side (bot: MarketData + strategy)  --TradeClient-->  TradeService (buckets)
    knows nothing about the mode or the key                     owns provider, gateway, ledger
  ```

  Today `LocalTradeClient` connects the two in the same process (`run`,
  backtests, tests). Plan item B3 adds a socket `TradeClient`. Then each strategy
  runs as its own **strategy-runner** process, and one **trade-runner** per
  mode holds the key and the `TradeService`. The bot code does not change.

- **Layers with enforced import rules.** Each module belongs to one layer and
  may import only from the layers listed for it. `tests/test_architecture.py`
  checks this.

  | Layer | Modules (today, plus planned) | May import |
  |---|---|---|
  | **core** | `trader` (the empty package init), `trader.models.*`, `trader.paths`, `trader.logging_config`, `trader.indicators`, `trader.trading_service.protocol` | core |
  | **strategy** | `trader.trading_strategy`, `trader.strategy_spec.*`, `trader.strategies_registry` | core |
  | **market** (read-only data) | `trader.market.*`, the Jupiter HTTP/websocket client, `candles.py` and its dataclasses | core, market |
  | **venue** (moves funds) | `async_jupiter_svc`, `executor`, `async_rpc_client`, `swap_costs`, `trader.paper.*` | core, market, venue |
  | **risk** | `trader.policy.*`, `trader.ledger.*` | core, risk |
  | **execution** | `trader.execution.*`, `trader.async_account`, `trader.trading_service.service` / `.local` | core, market, venue, risk, execution |
  | **strategy-side** | `trader.bot.*`, `trader.trading_service.client` (`TradeClient`); planned: `RemoteTradeClient`, the strategy-runner | core, strategy, market, strategy-side |
  | **app** (wiring) | `main.py`, `trader.wiring`, `trader.backtest.*`, `trader.notification.*`, `trader.agent_api.*`; planned: the trade-runner | anything |

- **The rule that matters most:** strategy code and the strategy-runner can
  **never** import execution, venue or risk modules. A strategy therefore
  cannot reach the key, the ledger or the policy, even by accident.
