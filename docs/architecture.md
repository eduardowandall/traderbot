# Architecture: a guided tour

This is a step-by-step walk through the project for a human reader. Read it
top to bottom:

- Steps 1–3 give you the vocabulary and the map.
- Steps 4–7 follow one trade from the command line to the blockchain and back.
- Steps 8–10 cover the other paths (modes, backtest), where the state lives, and
  the safety nets.
- Step 11 is the architecture this project is moving toward.

The code is the source of truth; this document names functions rather than line
numbers so it doesn't go stale. The roadmaps live in [`plan.md`](plan.md)
(hardening, done), [`agent-strategies.md`](agent-strategies.md) (current work)
and [`refactoring-backlog.md`](refactoring-backlog.md).

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
main.py                         CLI (Typer): run, start, swap, backtest, pnl, halt, resume, ledger ..., paper ...
policy.toml / policy.example.toml   owner's risk policy (TOML)
trader/
  __init__.py                   STRATEGIES registry used by the CLI (random, target_value, composer)
  paths.py                      data_dir() and policy_file(): where all state lives
  logging_config.py             console + file logging setup
  models/                       plain data types, no I/O
    mints.py                    SOLANA_MINTS registry, Mint (decimals, UI/raw conversion)
    order.py                    OrderSide, OrderSignal, SwapResult, Order
    position.py                 Position and its PnL math
    intent.py                   TradeIntent, PolicyDecision, IntentRecord, IntentStatus
    costs.py                    TradeCosts (network fee, rent), PnLResult, trade_rates
    public_data.py              TickerData (a candle)
    account_data.py             MintBalance
    bot_config.py               RunningMode + BotConfig (wiring; see step 11)
  trading_strategy.py           TradingStrategy base + Random, TargetValue, WMA, TrailingStop, TargetPercent, StrategyComposer
  bot/async_websocket_bot.py    the main loop: price -> strategy -> order
  async_account.py              AsyncAccount: balances, position, turns signals into intents and orders
  execution/gateway.py          TradeGateway (the only path to a swap) + KillSwitch
  policy/policy.py              Policy, pure evaluate(), TOML loader
  ledger/ledger.py              Ledger (SQLite): intents, events, PnL reports, restore
  providers/jupiter/
    async_jupiter_client.py     Jupiter HTTP (quote, swap tx, candles) + price websocket
    async_rpc_client.py         Solana RPC (Helius): balances, sign, simulate, send, confirm
    async_jupiter_svc.py        AsyncJupiterProvider: buy/sell/swap facade, retries, price-impact cap, costs
    swap_costs.py               reads the real amounts and fees from a confirmed transaction
    jupiter_data.py             Jupiter response dataclasses
  paper/                        SimulatedWallet (.data/paper-wallet.json) + PaperJupiterProvider
  backtest/                     tick recording/loading + Backtester (replays ticks with synthetic quotes)
  notification/                 Null / Telegram notifications
tests/                          mirrors trader/ (tests/trader/...), plus tests/strategies and tests/test_main.py
docs/                           plan.md, agent-strategies.md, refactoring-backlog.md, this file
```

## Step 4 — Starting the bot (`main.py run`)

Example: `uv run main.py run paper SOL-USDC random 'sell_chance=20 buy_chance=40'`

`main.run` builds the pieces and hands them to the bot:

1. **`_build_provider(mode)`** returns the object that talks to Jupiter and the
   chain:
   - in `paper`, a `PaperJupiterProvider` over a `SimulatedWallet`;
   - otherwise, an `AsyncJupiterProvider` with the keypair from
     `SOLANA_PRIVATE_KEY`.
2. **`_build_gateway(mode)`** returns a `TradeGateway`, made of:
   - the ledger for this mode (`data_dir()/ledger-<mode>.sqlite3`);
   - the policy loaded for this mode (`load_policy(mode=...)`);
   - the `KillSwitch` (`data_dir()/HALT`).
3. **`_get_strategy_obj(name, args)`** looks the name up in `STRATEGIES` and
   builds the strategy from the `key=value` arguments.
4. **`create_bot_config(...)`** resolves the symbol into mints and returns a
   `BotConfig`.
5. It then builds `AsyncWebsocketTradingBot(config).run()`, which opens an
   asyncio loop.

## Step 5 — Startup and the tick loop (`trader/bot/async_websocket_bot.py`)

**Startup (`_startup`):**
1. `account.restore_from_ledger()` rebuilds the open position and the PnL
   totals from the ledger, so a restart does not forget a position.
2. Outside dry mode, `account.reconcile_position()` checks that the wallet
   really holds the position. A mismatch is logged as an event.
3. `strategy.setup(candles)` warms the strategy up with recent candles.

**Each tick (`_tick` → `process_market_data`):**
1. `account.get_price(output_mint)` reads the next price from the Jupiter
   websocket (in USD).
2. `strategy.on_market_refresh(price, spread, balance, position)` returns an
   `OrderSignal` or `None`.
3. If there is a signal and orders are not paused, `account.place_order(...)`
   runs, and the bot sleeps 2s so the wallet can settle.

**Error handling (`_on_error`):**
- A policy denial or a duplicate intent pauses new orders for 30s
  (`denial_cooldown`). The loop keeps running and the strategy keeps getting
  prices.
- Any other error retries with exponential backoff, from 1s up to 60s.

## Step 6 — From signal to swap

**`AsyncAccount` (`trader/async_account.py`)** turns a signal into an intent:
- **`buy`:**
  - refuses if a position is already open or the balance is below the minimum;
  - caps the amount at the spendable balance, keeping 0.02 SOL for fees when
    SOL is spent;
  - builds a `TradeIntent`.
- **`sell`:**
  - caps the quantity at what the wallet actually holds;
  - uses a fixed idempotency key per position, so one entry can never be sold
    twice.
- **Notional:** the intent's USD value (`notional_usd`) is known only when the
  input is USDC/USDT. For other pairs it is `None`, and the policy denies it by
  default.

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
2. Get the swap transaction from Jupiter.
3. Sign it with the keypair.
4. Simulate it, then send it.
5. Wait for confirmation. A failed transaction raises right away.

`_do_swap_with_retry` retries only failures **before** the send, with slippage
escalating up to a ceiling. Anything that fails after the send raises
`TransactionSubmittedError` and is never retried, to avoid double execution.

In **paper** mode, `PaperJupiterProvider` replaces only `_do_swap`: it takes a
real quote and applies it to the simulated wallet. In **dry** mode, the RPC
client simulates the transaction and never sends it.

## Step 7 — After the swap

1. The gateway marks the intent `EXECUTED`.
2. **Only then** does `AsyncAccount` call `provider.fetch_swap_costs(result)`.
   It returns the real network fee, rent, and the real in/out amounts, read
   from the confirmed transaction. It never raises: if it fails, the costs are
   just unknown.
3. The result becomes an `Order`. Real amounts are used when known, otherwise
   the quote's.
4. A buy opens a `Position`. A sell closes it and computes the realized PnL:
   gross, costs, and net in the quote token, plus a USD estimate.
5. `ledger.attach_order(...)` stores the order JSON and the PnL on the intent
   row. That is what `restore_from_ledger`, `main.py pnl` and `ledger list`
   read later.

## Step 8 — Modes and the other entry points

| | real | dry | paper | backtest |
|---|---|---|---|---|
| Needs key + RPC | yes | yes | no | no |
| Prices | live websocket | live websocket | live websocket | recorded ticks or candles |
| Quotes | Jupiter | Jupiter | Jupiter | synthetic (`ReplayQuoteClient`: tick price minus `fee_bps`) |
| Swap | signed and sent | simulated, never sent | applied to `SimulatedWallet` | applied to an in-memory `SimulatedWallet` |
| Ledger | `ledger-real` | `ledger-dry` | `ledger-paper` | none |

- **`main.py swap`** is a one-off manual swap. It builds a `TradeIntent` and
  submits it through the same gateway.
- **`main.py backtest`** runs a strategy over ticks with `Backtester`
  (`trader/backtest/replay.py`), using the same `AsyncAccount` and
  `PaperJupiterProvider` code with a synthetic quote client. The strategy gets
  the replay clock and a fixed seed, so results are reproducible. That is why
  strategies must use `self.clock()` / `self.rng`.
- **`run --record-ticks FILE`** saves live prices so they can be replayed.

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

Details and progress are in [`agent-strategies.md`](agent-strategies.md). In
short:

- **AI agents author strategies, they never trade.** An agent writes a
  declarative JSON **strategy spec**. It gets validated and backtested, and
  then it runs automatically in paper mode. Real mode needs the owner's
  approval.
- **Strategy and execution become separate processes:**

  ```
  strategy-runner (one per strategy)  --TradeClient-->  trade-runner (one per mode)
    reads prices, runs the strategy                     owns the key, wallet, ledger, gateway
    knows nothing about the mode or the key             one budget "bucket" per strategy
  ```

  The same `TradeClient` interface has an in-process implementation (tests,
  backtests, `main.py run`) and a socket implementation (separate processes).

- **Layers with enforced import rules.** Each module belongs to one layer and
  may import only from the layers listed for it. `tests/test_architecture.py`
  checks this.

  | Layer | Modules (today, plus planned) | May import |
  |---|---|---|
  | **core** | `trader.models.*` (except `bot_config`), `trader.paths`, `trader.logging_config`; planned: `trader.indicators`, `trader.trading_service.protocol` | core |
  | **strategy** | `trader.trading_strategy`; planned: `trader.strategy_spec.*` | core |
  | **market** (read-only data) | Jupiter HTTP/websocket client and its dataclasses; planned: `trader.market.*` | core, market |
  | **venue** (moves funds) | `async_jupiter_svc`, `async_rpc_client`, `swap_costs`, `trader.paper.*` | core, market, venue |
  | **risk** | `trader.policy.*`, `trader.ledger.*` | core, risk |
  | **execution** | `trader.execution.*`, `trader.async_account`; planned: `trader.trading_service.*` | core, market, venue, risk, execution |
  | **strategy-side** | planned: `TradeClient`, `RemoteTradeClient`, the strategy-runner | core, strategy, market, strategy-side |
  | **app** (wiring) | `main.py`, `trader` (package init), `trader.bot.*`, `trader.backtest.*`, `trader.notification.*`, `trader.models.bot_config`; planned: `trader.agent_api.*`, the trade-runner | anything |

- **The rule that matters most:** strategy code and the strategy-runner can
  **never** import execution, venue or risk modules. A strategy therefore
  cannot reach the key, the ledger or the policy, even by accident.
