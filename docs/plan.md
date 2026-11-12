# Plan

Rewritten on 2026-10-16. Holds only what is ahead. Finished items (A1–A38
and the stages before them), the perps design (§8, D1–D11) and the
decision log are in [`history.md`](history.md). How the code works:
[`architecture.md`](architecture.md). How to write a spec:
[`specs.md`](specs.md).

Plan here first, then implement: `/phase` writes an item's design under it in
§2 before the code; a finished item moves to the end of `history.md`.

---

## 1. Where we are

Spot and perps run in paper, backtest and real (5 real JUP-SOL round trips;
one real short and three real long perp round trips, reconciled to the
chain). 1197 tests. Every goal is ~95%: specs as files, structured blocks
(crossovers too, A22), one wallet with a bucket per strategy, accurate
costs, perps.

**Next focus: the backtest.** A34–A38 (2026-10-21) gave it past windows
(paged, cached), a scenario catalog, parameter variants ranked off the
holdout windows, ~2x faster replays with cells in parallel, and stresses.
What is left to trust it is planned as A39–A43 below (2026-10-27), from the
gaps A38 left.

## 2. Roadmap

In order. Each item ships with `/check` green. Size: **S** < 1 h, **M**
several modules, **L** a design change.

### Backtest trust (A39–A43, planned 2026-10-27)

From the gaps A38 left. Order: A39 -> A40 -> A41 -> A42 -> A43. A39 and A40
change what a tuning run concludes; A41 is cheap; A42 and A43 are bigger.

#### A39. Walk-forward ranking — S/M
- Today only the `holdout` tag guards against tuning to the catalog. With
  `--walk-forward` (on `--rolling` or any windows, ordered by start), fold k
  picks the best variant on the windows before k (`--train N`: the last N;
  default all before) by the same rank as A36, and records that variant's
  result on window k: out of sample.
- No new replays: every variant already ran on every window, so this reads
  the `run_matrix` rows. Output: the out-of-sample median and worst next to
  the base spec's on the same windows, and how often the pick changed (a
  pick that jumps every fold is noise).

#### A40. Intrabar path from finer candles — M
- A candle replays as open -> low -> high -> close (open -> high -> low ->
  close on a down bar), so a stop and a target inside one bar always resolve
  in that order. For a 1 m or 1 h spec, `--intrabar fine` builds each bar's
  path from the 15 s (or 1 m) candles inside it, in their real order (the
  same history cache, A34); the indicators still see the spec's bars.
- For a 15 s spec (nothing finer): `--intrabar worst` puts the adverse
  extreme first (low first for a long), a pessimistic bound.
- Every result counts the exits where a stop and a target were both inside
  one bar (`ambiguous_exits`), so the order's effect is visible even
  without the flags.

#### A41. Network fee at the window's SOL price — S
- The network fee is a fixed USD per leg measured at today's SOL price.
  Keep it in lamports (5000 + the priority cap) and price it at the SOL
  price of the tick: the pair's own when a side is SOL, else SOL's candles
  for the window (a third series, cached). `REPLAY_SOL_USD` then only covers
  a `--ticks` file without SOL.
- The pool fee and slippage stay measured now (no history exists for them);
  `--volume-slippage` (A38) is the proxy for thinner markets.

#### A42. Live vs backtest from any start — M
- `live_vs_backtest.py` assumes the bucket started the window flat with its
  whole budget. Seed the replay with the bucket's state at the first tick,
  as the bot does at a restart: the ledger's legs before the window go into
  the replay's in-memory ledger and wallet, and the strategy resumes from
  them (`resume(last_exit_at, opened_at, last_exit_price)`, the open
  position and its peak).
- Then any recorded window compares, not only one that started flat.

#### A43. Perp model and perp stresses — L
- Spot candles stand in for the venue's oracle, borrow is a flat
  `--borrow-bps-hour`, and the impact fee is linear. Read the custody's
  impact parameters (A10's reader) and apply Jupiter's formula, and take the
  borrow rate from the custody's utilization curve at the live rate as the
  default instead of 1 bps/h.
- Then `--costs` scales the perp fee and impact (and borrow), so a perp
  takes cost stresses too; `--volume-slippage` stays spot-only.
- The oracle stays the spot candles (the Doves feed has no history);
  documented as the remaining gap.

### Backlog
- A service entrypoint (systemd, Docker or a Windows service).
- Manual trades with a wallet treasury (deferred).
- Rename the swap-shaped names (`spend_mint`/`receive_mint`,
  `Order.input_mint`/`output_mint`): waits for the next schema bump.

## 3. Principles

- **Strategies are files.** Agents and the owner write specs from
  `specs.md`; agents never trade real, touch the key or edit the policy.
- **The policy decides deterministically** (`evaluate()` is pure,
  wallet-wide); per-strategy limits are the bucket's.
- **Execute exactly once.** One broadcast per idempotency key; no retry after
  a send.
- **The ledger is the source of truth**, reconciled against the wallet and
  the perps venue.
- **Fail closed.** No ledger, a broken policy or a stale price: no trade.
- **One wallet, one bucket per spec** (`strategy:<spec_id>`; the id hashes
  the behaviour, so each version is a new bucket).
- **Ledger schema:** one `_SCHEMA`, no migrations; a change waits for the end
  of a paper soak and takes every pending rename with it.

## 4. Known limitations

### 4.1 Money and accounting
- **Costs and net PnL:** the network fee, token-account rent (refunded when
  a bucket retires flat) and other SOL a route charged are read from the
  confirmed tx; failed sends' fees are booked (5000 lamports if unreadable).
  LP fees, impact and slippage are inside the amounts, never subtracted.
- **Paper** charges the base fee plus the priority cap and first-time rent,
  and fills 10 bps below the quote.
- **USD values** not in the trade come from a Price API snapshot taken
  before it; without it costs are `[!] incompleto` and a buy of unknown USD
  is denied (unless `allow_unknown_notional`).
- **Two units:** a backtest on a non-stable quote includes that token's own
  USD move.
- **Perp rent** is a one-time deposit per market and side (1,747,520
  lamports, measured, not read); a paper wallet reset forgets it. Request
  accounts lock ~3.7M lamports each until the keeper closes them.
- **Paper perps:** borrow is fixed at the open's live rate; a liquidation is
  only seen while a `connect` has the bucket open.
- **One tick per second** from the hub; tick-counting conditions fire at a
  steady rate.
- **The daily report needs Telegram**; without it the day is still marked
  reported.

### 4.2 External APIs
- Swaps use `api.jup.ag` `/swap/v1` (`/swap/v2/order` with a `taker` rejects
  `slippageBps`); `lite-api.jup.ag` is being retired; `JUPITER_API_URL`
  overrides.
- The price websocket and candles are undocumented; prices fall back to the
  Price API V3, candles have no fallback.
- Keyless requests hit `429` under bursts.
- Jupiter Perps is read from a trimmed community IDL; if the live perps suite
  fails, it may have changed.

### 4.3 Security
- The key lives only in `serve`. Never format a `Keypair` with `str()`: it
  is the base58 secret.

### Open questions (owner)
- The keeper's minimum collateral for a long (1 USDC worked for a short, 10
  for a long).
