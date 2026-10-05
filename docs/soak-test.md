# Paper soak test: one trade-runner, three specs

The owner's paper soak (item A2 in [`plan.md`](plan.md); "Owner task: a
one-week paper soak" in the old plan, now in `history.md`), run with B3's split
instead of `run`: one `serve paper` and one `connect` per spec. This file
records what was checked, how, and what was found. Anything here that needs
code becomes an item in `plan.md`. Open findings as of 2026-10-05: F1 is A4;
F2, F4 and F7 are fixed, and F3, F5 and F8 too (A1, in `history.md`); F6 is by
design. The F8 note on the local `policy.toml` comments is the owner's.

- **Started:** 2026-10-03 23:30 local (UTC+1). **This report:** 2026-10-04,
  about 12.5 h in.
- **Times:** log lines are local time (UTC+1); ledger rows are UTC. "10:49 UTC"
  and "11:49 local" are the same moment.
- **State was not fresh.** The paper ledger goes back to 2026-09-30 and the
  paper wallet is older than that (see §4.6). The random spec's bucket already
  had about 170 round trips from 2026-10-01, and they count toward its
  `max_loss_usd`.

## 1. Setup

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

### Timeline

| Local time | Event |
|---|---|
| 10-03 23:30 | Trade-runner #1 up; the three `connect`s say `hello` and start ticking. Paper policy: 60 buys/hour. |
| 10-03 23:56 | wma-composer is warm, 25 min after its start (see F1). |
| 10-04 01:00 | Daily report for 2026-10-03 (`daily_report` event at 00:00:02 UTC, 1 bucket). |
| 10-04 overnight | random runs into the 60 buys/hour limit every hour (§4.7). |
| 10-04 09:33 | Owner restarts the trade-runner with 120 buys/hour. All three `connect`s reconnect. |
| 10-04 11:49 | Owner edits `policy.toml` (11:48:51; now 660 buys/hour) and restarts the trade-runner. All three reconnect; random's open position is restored from the ledger and sold 10 s later. |
| 10-04 12:16 | random reaches `max_loss_usd` (-10.008 USD realized): bucket retired, its `connect` stops by itself (§4.9). sol-dip and wma-composer keep running. |

## 2. Verdict

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

## 3. Findings

Ordered by how much they matter. F1-F3 are worth a plan item.

### F1. Warm-up from candles collapses for thinly traded tokens (wma-composer)

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

### F2. A quiet websocket costs a 30 s timeout on every tick (wma-composer)

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

### F3. Log retention is about 2.5 hours for the SOL `connect`s

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

### F4. The trade-runner doesn't log who is connected

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

### F5. `random_chance` frequency depends on the feed rate

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

### F6. random retires on history from 2026-10-01

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

### F7. Paper and backtest cost models differ by about 4x

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

### F8. Small things

- `account.py` logs a buy fill at INFO and a sell fill at DEBUG (`ORDER
  PLACED`); the serve log only shows half the fills at INFO.
- `log_ticker` prints prices with 9 decimals: NOBODY (0.0006) shows 6
  significant digits.
- The local `policy.toml` (untracked) still has comments from an older example
  (`main.py swap`, `main.py resume`, `[dry.*]`, `ledger list`).
  `policy.example.toml` is already clean.
- A policy change needs a trade-runner restart. That worked well here (§4.2),
  so it is fine as long as it stays documented.

## 4. Checks and results

### 4.1 Processes and errors

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

### 4.2 Trade-runner restarts

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

### 4.3 Market data

| Spec | Ticks | Rate | Source |
|---|---|---|---|
| random (SOL) | 70,896 in 2.15 h | 9.15/s, median gap 0.03 s | websocket |
| sol-dip (SOL) | 89,811 in 2.73 h | 9.14/s, median gap 0.04 s | websocket |
| wma-composer (NOBODY) | 1,820 in 12.4 h | 0.04/s, median gap 30.2 s | Price API after a 30 s websocket timeout (F2) |

### 4.4 Strategy behavior

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

### 4.5 Ledger integrity

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

### 4.6 Paper wallet vs ledger

`wallet - (sum of executed flows - fees - rent)` should be constant. Sampled 6
times, 20 s apart, across 5 new fills: always **USDC 72.036903, SOL
0.706204384** to the raw unit. Every change to `paper-wallet.json` is in the
ledger. (That "starting" balance isn't the 100 USDC + 0.5 SOL default: the
wallet predates this ledger.)

### 4.7 Policy

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

### 4.8 Daily report

`daily_report` for 2026-10-03 at 00:00:02 UTC, 1 bucket (only random had
fills), from trade-runner #1. Neither restart sent it again.

### 4.9 Max-loss retirement (random)

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

### 4.10 Resources

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

## 5. How these checks were made

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
