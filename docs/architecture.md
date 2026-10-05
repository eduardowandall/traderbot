# Architecture: follow one trade

This document explains the code by following **one paper buy** from the command
line to the wallet and back, hop by hop. Read §3 first; the rest fills in what
changes for sells, real mode and backtests, and where the state lives.

Line numbers are as of 2026-09-30 and will drift; the function names are the
stable handle. The roadmap and open issues are in [`plan.md`](plan.md); the
finished stages are in [`history.md`](history.md).

---

## 1. The bot in one paragraph

A long-only bot for Solana tokens that swaps through the **Jupiter** DEX
aggregator. A **strategy** (always a JSON spec, `trader/strategy/spec/`) looks at
each price tick and says buy, sell or nothing. Each order becomes a
**TradeIntent**, is checked by the owner's **policy** (`policy.toml`) and is
written to a **ledger** (SQLite, one file per mode) before and after it
executes. One position per bucket at a time; each spec trades in its own
**bucket** (`strategy:<spec_id>`) with its own budget.

| Term | Meaning |
|---|---|
| Pair `OUTPUT-INPUT` | `SOL-USDC` buys SOL paying USDC. The input is the quote token. |
| UI vs raw | `1.5` USDC is `1500000` raw. `Mint.ui_to_raw` / `raw_to_ui` (`trader/shared/models/mints.py`). |
| Mode | `paper` (simulated wallet file, real quotes, no key) or `real` (signs and sends). |
| Signal → request → intent → order | `OrderSignal` (strategy) → `OrderRequest` (bot to service) → `TradeIntent` (ledger row) → `Order` (the fill). |

## 2. The map, by folder and by layer

The top folders of `trader/` follow the two processes:

| Folder | Process | What |
|---|---|---|
| `trader/execution/` | the trade-runner (`serve`, or the execution half of `run`) | `market/`: everything that reads Jupiter (the client, candles, the `PriceHub`, the USD oracle), never moves funds; `trade/`: the key, wallet, ledger, policy, venues and the one path to a swap; `runner.py` is the `serve` server (it also answers `price` and `candles`), `wiring.py` builds it per mode |
| `trader/strategy/` | the strategy-runner (`connect`, or the bot half of `run`) | the bot loop (`bot/`), the spec engine (`spec/`), the client of the trade-runner (`trading_service/`); `runner.py` builds one bot |
| `trader/shared/` | both | models, the strategy's feed without network (`market/`: `MarketData`, `HubMarketData`, pairs), the spec's terms (`spec/`: `SpecTerms` and the policy checks over them, all the trade-runner sees of a spec, B13), the wire protocol (`trading_service/`), `paths.py`, Telegram |
| `trader/api/cli/` | entry points | the Typer commands and the per-mode process lock |
| `trader/backtest/` | in-process | replays a spec through both sides |

`execution` and `strategy` never import each other, and `shared` imports
neither; `tests/test_architecture.py` enforces it. Only the trade-runner talks
to Jupiter: a `connect` gets prices and warm-up candles from `serve` (B12). Inside them, each module
belongs to one layer and may only import the layers below it. **Strategy code
never imports execution, venue or risk**, so a strategy can't reach the key,
the ledger or the policy.

| Layer | What | Modules |
|---|---|---|
| app | commands and wiring | `main.py`, `trader/api/cli/` (`run`, `backtest`, `serve`, `connect`), `trader/execution/runner.py`, `trader/execution/wiring.py` (mode -> components), `trader/execution/notification/` (daily report), `trader/backtest/` |
| strategy-side | the loop that knows no mode, key or ledger | `trader/strategy/bot/` (`async_websocket_bot.py`, `decision.py`, `config.py`), `trader/strategy/trading_service/` (`client.py`, `remote.py`), `trader/strategy/runner.py` |
| strategy | specs: pure signals | `trader/strategy/spec/` (`models.py`, `expr.py`, `parse.py`, `strategy.py`, `conditions.py`) |
| execution | buckets, the account, the one path to a swap | `trader/execution/trade/trading_service/` (`service.py`, `local.py`), `trader/execution/trade/gateway/` (`account.py`, `gateway.py`, `fills.py`, `orders.py`) |
| risk | limits and the audit log | `trader/execution/trade/policy/`, `trader/execution/trade/ledger/` |
| venue | moves funds | `trader/execution/trade/venues/jupiter/` (`async_jupiter_svc.py` provider, `executor.py` + `async_rpc_client.py` real), `trader/execution/trade/venues/paper/` (paper) |
| market | read-only prices and candles (`hub.py`: one price feed for every bot; `pair.py`: the token in the quote token) | `trader/execution/market/` (`data.py`, `hub.py`, `prices.py`, `jupiter/`: `async_jupiter_client.py`, `candles.py`), `trader/shared/market/` (`feed.py`, `pair.py`, no network) |
| core | plain data | `trader/shared/models/`, `trader/execution/models/`, `trader/shared/trading_service/` (`protocol.py`, `wire.py`), `trader/shared/notification/`, `trader/shared/spec/` (`terms.py`, `validate.py`), `trader/shared/indicators.py`, `trader/shared/paths.py`, `trader/shared/logging_config.py` |

## 3. One paper buy, hop by hop

`uv run main.py run paper docs/examples/spec-sol-dip.json`

### Startup (once)

| # | Where | What happens | Why it exists |
|---|---|---|---|
| S1 | `main.py:11` `main` | Sets up logging, runs the Typer app; an escaping error is printed through `redacted_excepthook`. | Typer's own hook would print the Helius key. |
| S2 | `trader/api/cli/bot.py:35` `run` | Loads the spec, validates it against the paper policy (`_check_limits`), builds everything. | Refuse a spec that could never trade. |
| S3 | `trader/execution/wiring.py:65` `build_trade_service` | paper → `paper_provider(SimulatedWallet)`; real → `AsyncJupiterProvider.on_chain(key)`. Adds `TradeGateway.for_mode` (ledger + policy) and the USD price oracle. | The **only** place a mode turns into components. |
| S4 | `trader/api/cli/bot.py:60` | `LocalTradeClient(service, "strategy:<spec_id>", ...)`, then `AsyncWebsocketTradingBot(BotConfig(...))`. | The bot only ever sees its bucket (`TradeClient`); B3 will swap in a socket client. |
| S5 | `trader/strategy/bot/async_websocket_bot.py:156` `_startup` | `trader.open()` → `TradeService.open_bucket` (`service.py:83`): marks `bucket_opened` once, rebuilds the position and PnL from the ledger (`AsyncAccount.restore_from_ledger` → `TradeGateway.restore`, `gateway.py:177`), reconciles against the wallet. | A restart continues where it stopped. |
| S6 | `async_websocket_bot.py:173` `_resume_strategy` | `strategy.resume(last_exit_at, opened_at, last_exit_price)`. | Cooldown, re-arm and `ttl_days` survive restarts. |
| S7 | `async_websocket_bot.py:156` | Fetches `strategy.warmup()` candles and seeds the indicators (`strategy.setup`). | Entries wait for converged indicators. |

### Each tick

| # | Where | What happens | Why it exists |
|---|---|---|---|
| 1 | `async_websocket_bot.py:186` `_tick` | Price from `MarketData.get_price`: a `HubMarketData` over the process's `PriceHub` (`execution/market/hub.py`: one websocket for all mints, the Price API for quiet ones; never older than 30s, one price per second), in the **quote token**: for a non-stable quote the CLI gave the bot `market_for(...)`'s `PairMarketData`, which divides two USD feeds. Then `trader.bucket()` → `TradeService.get_bucket` (`service.py:240`): `available` = `min(spendable, budget cap / quote_usd)` in the quote token, the quote's USD price, position, PnL. | The strategy sizes against what the bucket may spend; money stays USD, prices stay in the pair's own unit. |
| 2 | `async_websocket_bot.py:68` `process_market_data` → `trader/strategy/bot/decision.py:23` `order_for` | `SpecStrategy.on_market_refresh(price, available, position, quote_usd)` (`strategy_spec/strategy.py:96`) returns `OrderSignal(BUY, spend / price)` (`_entry`, `:176`; `spend` from `fixed_usd / quote_usd` or `pct_of_bucket`), wrapped as an `OrderRequest`. | The same decision code runs in the backtest. |
| 3 | `trading_service/local.py:45` → `service.py:163` `submit_order` | Refuses buys on a retiring bucket, takes the order lock. | Buckets share one wallet; orders go one at a time. |
| 4 | `service.py:290` `_place` → `:304` `_execute` | Dispatches on side: `account.buy(..., limit=<cap in the quote token>)`. Any exception becomes an `OrderReply` (denied / rejected / error), logged once. | Nothing from execution crosses into the strategy side as an exception. |
| 5 | `trader/execution/trade/gateway/account.py:311` `buy` → `:280` `_buy_limit` | One balance read; checks no open position, the minimum, the SOL fee reserve and the bucket cap; computes `spend = quantity * price` once (capped). | Budget and wallet limits in one place. |
| 6 | `account.py:259` `_usd_snapshot`, `:262` `_intent` | USD snapshot of both sides and SOL from the process's price oracle (the hub; stablecoins are 1), **before** the trade; builds the `TradeIntent` (spend amount, USD notional, idempotency key). | After EXECUTED nothing may fail, so no fetch happens later. |
| 7 | `trader/execution/trade/gateway/fills.py:50` `execute_trade` | The one pipeline for every trade: `gateway.submit`, then costs. Whether the swap filled or not, attempts the chain confirmed as failed get their fee read and booked (`failed_tx_fee` event, `PositionBook.charge`), never raising. | Enforces the order "EXECUTED first, costs second"; a failed transaction still cost the bucket its fee. |
| 8 | `trader/execution/trade/gateway/gateway.py:214` `submit` → `:238` `_authorize` → `trader/execution/trade/ledger/ledger.py:34` `authorize` | In one `BEGIN IMMEDIATE` transaction: idempotency lookup, `policy_state()` (daily notional, trades/hour, loss, breaker since this process started, unresolved intents), `policy.evaluate` (`policy/policy.py:294`, pure), insert the intent as EXECUTING or DENIED. | Two processes can never pass the same limit or reuse a key. |
| 9 | `gateway.py:254` `_execute` → `providers/jupiter/async_jupiter_svc.py:98` `buy` → `:153` `swap_with_details` → `_attempts` → `_do_swap` | Raw amount from the intent's spend; quote, price-impact cap, the quote's value against the Price API (≤ 2% below; on in real and paper), up to 3 attempts with rising slippage and a 60s deadline. Signatures of attempts that failed on-chain leave on the result or the final error (`failed_signatures`). | Retries are safe only before broadcast. |
| 10 | `trader/execution/trade/venues/paper/executor.py:64` `SimulatedExecutor.execute` → `paper/wallet.py:94` `apply_swap` | The output is the quote's minus `slippage_bps` (10), never below its `otherAmountThreshold`. Under a file lock: re-read, debit input + fee (base + the policy's priority-fee cap) + first-time rent, credit output, write atomically. Returns a `SwapResult` with the simulated costs. | Paper behaves like the chain (fees, rent, slippage) across processes. |
| 11 | `trader/execution/trade/ledger/intents.py:142` `mark_executed` | EXECUTING → EXECUTED with the signature and amounts. A failure here leaves the row EXECUTING, which blocks the account. | Fail closed: never buy twice after an unrecorded swap. |
| 12 | `async_jupiter_svc.py:222` `fetch_swap_costs` (from `fills.py`) | Real mode reads the confirmed transaction; paper already has the costs. Never raises. | Costs are recorded, never assumed. |
| 13 | `account.py:174` `_to_order` → `trader/execution/trade/gateway/orders.py:33` `order_from_fill` | Real amounts, else the quote's; USD rates; never raises. | A trade that happened is never lost. |
| 14 | `account.py:194` `_record_fill` → `gateway.py:197` `record_fill` → `intents.py:179` `attach_order` | Stores the order JSON, cost and PnL columns on the (EXECUTED) row; a failure is logged, not raised. `book.open(order)` (`models/book.py:99`) opens the position in memory. | The ledger is the truth; the book is its in-memory copy. |
| 15 | `service.py:163` (after `_place`) | `_check_max_loss`: realized loss past `max_loss_usd` retires the bucket; `_close_if_retiring` (`:173`) sells a leftover once per order. | Exits don't depend on the strategy. |
| 16 | `async_websocket_bot.py:78` `_handle_reply`, `:197` `_report_order` | Filled → log and Telegram, with the bucket after the fill (realized PnL; an open position marked to market, `Position.unrealized_usd`). Denied/rejected → pause orders 30s. Error → loop backoff. | The strategy keeps getting prices while orders pause. |

A **sell** takes the same path with `account.sell` (`account.py:386`): the
quantity is capped at the position and at the wallet, the idempotency key is
fixed per position (`<account>:sell:<entry order>:<qty>`), the intent records
whether it closes the position, budget rules don't apply, and
`book.close`/`book.reduce` compute the realized PnL.

## 4. Where state lives

| State | Where | Notes |
|---|---|---|
| Intents, orders, PnL, events | `data_dir()/ledger-<mode>.sqlite3` (`trader/execution/trade/ledger/`) | **The source of truth.** One schema, versioned by `PRAGMA user_version`; an older file is refused (move or delete it). Costs without a trade (`failed_tx_fee`) and the sent daily reports (`daily_report`) are events. |
| Position and PnL totals of a bucket | `AsyncAccount.book` (`models/book.py`) | Rebuilt from the ledger at startup (`gateway.restore`), then kept in step by each fill. |
| Signal state (entry price, peak, cooldown, re-arm, expiry) | `SpecStrategy` | Restored through `resume()` from the bucket snapshot. |
| Paper balances | `data_dir()/paper-wallet.json` (`paper/wallet.py`) | Created with 100 USDC + 0.5 SOL; delete it to start over. |
| Policy | `policy_file()` (`policy.toml`, untracked) | Model: `policy.example.toml`. Paper has roomy limits by default. |
| Logs | `logs_dir()/trader-<ts>-<pid>.log` | Rotating, pruned after 14 days, secrets redacted. |

`trader/shared/paths.py` resolves all of them from `TRADER_DATA_DIR` /
`TRADER_POLICY_FILE` / `TRADER_LOG_DIR` (relative to the project root), never
from the current directory. To look at a ledger:
`uv run --no-sync python .claude/scripts/ledger_dump.py paper`.

## 5. Real mode: what changes

Only hop 10 and hop 12. `OnChainExecutor.execute`
(`providers/jupiter/executor.py`) gets the swap transaction from Jupiter,
checks its programs against an allow-list, signs it, and simulates it asking
for the wallet and all its token accounts (`tx_inspection.py`): only the input
token may leave, by at most the quote's `inAmount`, plus SOL for fees. Then it
sends and waits for confirmation (`async_rpc_client.py`, Helius). A failed
inspection is a `TransactionInspectionError` and nothing is sent. After `send_transaction`, any failure is a
`TransactionSubmittedError`: the intent becomes UNCONFIRMED, it is never
retried, and it blocks the mode's trading until the owner checks the chain
and moves or deletes the ledger. `fetch_costs` (`executor.py:182`) parses the
confirmed transaction (`swap_costs.py`) for the real amounts, fee and rent.
Real mode is denied unless `real_trading_enabled = true`.

## 6. Backtest: what changes

`uv run main.py backtest spec.json` → `trader/backtest/spec.py:39`
`backtest_spec`: candles (or a `--record-ticks` file) become ticks, each closed
bar an interpolated path (rising bar open → low → high → close, falling bar
open → high → low → close; `backtest/ticks.py`), and too few bars for the
warm-up is an error. `Backtester` (`backtest/replay.py`) then runs hops 1–16
with three substitutions:

- quotes come from `ReplayQuoteClient` (tick price minus `fee_bps +
  slippage_bps`, and minus `network_fee_usd` per leg, since the replay wallet
  holds no SOL for fees); the executor adds no slippage of its own. The CLI
  measures `fee_bps` and `network_fee_usd` on Jupiter at the start
  (`backtest/costs.py`: a buy+sell quote at the spec's size, and the base fee
  plus the policy's priority-fee cap at the SOL price) unless both are given;
- the wallet is an in-memory `SimulatedWallet` with the spec's `budget_usd`
  in the quote token. For a non-stable quote (JUP-SOL) the ticks carry the
  quote's USD price (`Tick.quote_usd`, from its own candles: `fetch_ticks`
  divides the two series with `market/pair.py`'s `ratio_candles`), the service
  gets `ReplayPrices` as its price oracle, and equity is measured in USD;
- the gateway is `TradeGateway.in_memory()` (in-memory ledger, no policy
  limits, since ledger timestamps are wall-clock).

It uses the same `decision.order_for` as the bot, the replay clock and a fixed
seed, so the same input and the same costs always give the same result (a
summary, or JSON with `--json`); pass `--fee-bps` and `--network-fee-usd` to
fix the costs instead of measuring them. Every result reports the cost per
round trip from its in-memory ledger, computed like the live reports
(`Ledger.round_trip_costs`).

**Live vs backtest.** `trader/backtest/compare.py` (via
`.claude/scripts/live_vs_backtest.py`) replays a `--record-ticks` file the way
the bot saw it: `Backtester(warmup=...)` seeds the strategy with the candles
that closed before the first tick (the bot's S7), then trades on the ticks; the
result sits next to the bucket's executed legs and realized PnL in the same
window, read from the ledger.

## 7. Strategy specs

The full authoring guide is [`specs.md`](specs.md). In short, a spec is JSON: a pair and a timeframe; **entry** conditions (`all`/`any`);
an **exit** with a required **stop** (`stop_loss` / `trailing_stop`) plus
optional conditions; `fixed_usd` or `pct_of_bucket` sizing; a budget, a max
loss, a cooldown, and `ttl_days` or `expires_at`. Prices are in the pair's
quote token; money is in USD. `SpecStrategy` (`strategy_spec/strategy.py`):

1. updates the bars once per tick (`IndicatorBank`; a gap of more than
   `MAX_GAP_BARS` restarts the series and the spec warms up again);
2. with a position: the stop (always), then the exit conditions; exits never
   wait for the warm-up;
3. without one: enters only when warm (`spec.history()` bars), not expired,
   out of the cooldown, re-armed after the last exit, and the conditions hold;
4. every signal carries a rationale (`"spec 1c21... buy: rsi14<30"`).

Each condition type is a model in `models.py` plus a pure predicate in
`conditions.PREDICATES`; tests keep both in step with each other and with
[`specs.md`](specs.md), the guide specs are written from. The `expr` condition
is parsed by `strategy_spec/expr.py` (`ast.parse` in `eval` mode, a whitelist
walk that also checks number vs condition, and its own evaluator; never `eval`) when the spec is validated, and evaluated on the
same `TickContext` and `IndicatorBank` as the blocks.

## 8. Safety nets

- **Policy** per trade, per day, per hour, daily loss, allowed symbols; real
  mode off by default. Sells skip the budget rules so a position is never stuck.
- **Idempotency keys** in the ledger: no double execution across restarts.
- **UNCONFIRMED and own-account EXECUTING intents** block trading.
- **Circuit breaker:** N consecutive failures since the process started stop
  trading; a restart re-arms it.
- **Bucket budget and `max_loss_usd`** per spec; the SOL fee reserve.
- **Price-impact cap and slippage ceiling** in the provider; no retry after
  broadcast.
- **Quote check** against the Price API (2%), and in real mode the
  **transaction inspection** (allowed programs, simulated balances) before
  anything is sent.
- **No stale prices:** the hub refuses a price older than 30s, so a bot
  backs off instead of deciding.
- **Per-bucket hourly limit** next to the wallet-wide one.
- **Wallet checks:** one balance cache per `TradeService`; open budgets must
  fit the wallet; open positions are reconciled against the wallet at the first
  open, and a missing token blocks its buys.
- **One execution process per mode** (OS lock shared by `run` and `serve`).
- **Stopping the process stops trading** (there is no separate kill switch).

## 9. Several specs: `serve` + `connect`

`run` puts the bot and the `TradeService` in one process. For several specs,
the same hops split across processes at the `TradeClient` seam (hop 3):

- **`main.py serve <mode>`** (`trader/execution/runner.py`) holds the
  mode's lock, the key, the wallet, the ledger and one `TradeService`, and
  serves JSON lines on `127.0.0.1` (`docs/plan.md` §3.3). `hello` validates
  the spec with this process's policy and opens its bucket; `bucket` and
  `submit` are hops 1 and 3–16. Every 30s it retires expired specs and sells
  what's left in retired buckets, so exits don't depend on the other process.
  Its log shows each bucket open, each accepted `hello` and each disconnect,
  with the number of specs connected. It also runs the price hub and answers
  `price`, so N strategy-runners share one websocket and one Price API poll;
  the hub is also its own price oracle (budgets, USD values, the sweep, the
  daily report and the quote check), so nothing else polls the Price API.
- **`main.py connect spec.json`** (`trader/strategy/runner.py`, the
  strategy side) runs the usual bot with a `RemoteTradeClient`
  (`trading_service/remote.py`) instead of `LocalTradeClient`, and a
  `HubMarketData` over `RemoteTradeClient.price` (candles for the warm-up
  still come straight from Jupiter). It has no key,
  no ledger and no mode; on a dropped connection it reconnects and resends with
  the same idempotency key, so an order executes at most once.
- **One execution process per mode** (`trader/api/cli/lock.py`): `run` and
  `serve` take the same OS lock, so the shared balance cache, the budget
  allocation and the startup reconcile in `TradeService` see every order.
- **The daily report** (`trader/execution/notification/daily_report.py`) runs in the
  execution process, which owns the ledger: next to the server in `serve`, and
  in `run` as one of the bot's `BotConfig.background` tasks (the bot only
  starts and cancels it); `TradeRunner.background` does the same in `serve`. Each minute it checks whether the previous UTC day
  has a `daily_report` event; if not, it sends fills, costs, realized PnL and
  open positions marked at the hub's price for every bucket of the mode.

## 10. Where it's going

Details in [`plan.md`](plan.md) (stage B):
transaction inspection and market-sanity checks (B7), and perps (B8). Specs
stay files written from [`specs.md`](specs.md).
