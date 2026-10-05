# Perps plan

Status as of 2026-10-03: **planning only, nothing implemented; the owner's
decisions are in (§9).** This is the design for trading perpetual futures
(perps) next to spot. It extends
[`plan.md`](plan.md) (item B8 there). Track progress in §8 and record decisions
in §10. Plan here first, then implement.

---

## 1. Goal and scope

A spec can run as a **perp position** instead of a swap: long or short, with
leverage, on the same wallet, through the same gateway, policy, ledger,
buckets and `serve`/`connect` processes. Backtests and paper runs come before
real money, as for spot.

In scope (v1):
- One perp market per spec, one direction per spec (`long` or `short`), one
  position per bucket at a time. Hedging means two specs.
- Market entry and exit. Spec-level stops and exits as today, plus a stop that
  the venue holds on-chain (§4, D7).
- Paper, backtest, then real on one venue.

Out of scope (v1): flipping direction inside a bucket, adding to or removing
collateral from an open position, limit orders, cross-margin between buckets,
several venues at once, funding-rate strategies.

## 2. Perps in one page

What the bot has to model that spot doesn't have. Numbers are for Jupiter
Perps (the JLP pool), checked on 2026-10-03 (sources in §11).

| Concept | Spot today | Perp |
|---|---|---|
| Direction | Long only: buy, then sell | Long or short; a short profits when the price falls |
| Exposure | What you spent | `size_usd = collateral_usd x leverage` (1.1x to 250x on Jupiter) |
| What you hold | Tokens in the wallet | A **position account** on the venue; the wallet only holds the collateral you haven't posted |
| PnL | `quote received - quote spent - costs` | `direction x (exit - entry) / entry x size_usd - fees`, settled in collateral on close |
| Running costs | None | **Borrow fee** charged hourly from collateral (Jupiter: utilization x hourly rate x size). Most other venues charge **funding** instead, which can be paid or received |
| Trade fees | LP fees inside the swap amounts, network fee | 0.06% of size on open and on close, plus a price-impact fee and the network fee |
| Losing it all | The token goes to zero | **Liquidation**: when collateral minus fees falls to maintenance margin (0.2% of size on Jupiter), the venue closes the position and keeps **all the remaining collateral**. Borrow fees move the liquidation price closer every hour, even when the price doesn't move |
| Price | Jupiter quote | The venue's **oracle** (Jupiter: Edge by Chaos Labs, with Chainlink and Pyth as checks and fallback); liquidations and on-chain TP/SL use it, not the swap price |
| Execution | One transaction, final when confirmed | Jupiter: **two phases**. Our transaction creates a `PositionRequest`, then a Jupiter keeper fills or rejects it in a second transaction. "Confirmed" no longer means "executed" |

Liquidation price (Jupiter, from the docs):
`liq = entry -/+ |collateral - close_fee - borrow_fee - size/500| x entry / size`
(minus for longs, plus for shorts). Without fees, 3x leverage liquidates after
a ~33% adverse move, 10x after ~10%, 50x after ~2%.

Collateral on Jupiter: a **long posts the asset itself** (SOL for a SOL long),
a **short posts a stable** (USDC or USDT). Any input token is swapped
automatically, and a close can pay out in a chosen token. The collateral's USD
value is fixed when it is deposited. For the bot this means a long funded with
USDC and closed back to USDC: the bucket stays in USD, like a spot bucket
today.

## 3. Venue choice

| Venue | State (2026-10) | Python route | Paper/testnet | Fit |
|---|---|---|---|---|
| **Jupiter Perps** (JLP pool: SOL, ETH, wBTC) | Active; the largest remaining Solana perps venue | **No REST API** ("work in progress"). Build instructions from the Anchor IDL (program `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu`) with `anchorpy` or hand-written Borsh plus `solders`, sign with our key, send through Helius. No Python SDK; a community TypeScript repo has the IDL and account constants | None known | **Best fit**: same key, same RPC, same on-chain custody and confirmation model, same company as our spot venue. Costs: hand-built instructions and the two-phase flow |
| Jupiter "GUM" beta markets (JUP, HYPE, ...) | New (2026-09-22), order book, single-transaction orders, hourly funding, limited to JUP stakers | No documented API | No | Not now |
| Velocity (formerly Drift) | Reopened ~2026-09-30 after a $285M hack in April | `driftpy` **archived** on 2026-09-03; TypeScript and Rust only | Devnet | Too young and too risky |
| Pacifica | Active | REST + websocket API, supported by CCXT (Python) | **Testnet** | Easiest to code against, but custody isn't fully on-chain and it uses a separate signing model. Second choice |
| Flash Trade, Zeta | Closed (2026-09, 2025) | n/a | n/a | No |

**Decided (D1): Jupiter Perps**, SOL first, then ETH and wBTC. Keep the
venue behind a seam (D4) so Pacifica, or a future Jupiter perps REST API, can
replace the adapter without touching strategies, buckets or the ledger.

Venue risk is real: in 2026 one Solana perps venue was hacked and two shut
down. The plan treats "the venue disappears" as an expected failure (§6).

## 4. How coupled is the code? (difficulty assessment)

The question was whether perps are hard because the code is too coupled. The
honest answer: **about 60% of the work is inherent** (perps really are a
different instrument: direction, leverage, liquidation, running fees,
two-phase execution) and **about 40% is accidental coupling** that a refactor
can remove first (stage P0). The layering itself is good. The coupling is in
the **domain model**: "a trade is a swap of one mint for another" runs from
the provider all the way to the ledger columns.

### 4.1 What is already venue-agnostic (keep)

| Seam | Where | Why it helps perps |
|---|---|---|
| Strategy-side boundary | `trading_service/protocol.py` (`OrderRequest`, `OrderReply`, `BucketSnapshot`), `client.py` (`TradeClient`) | The bot, `connect` and the wire protocol don't know what executes the order. A perp bucket is just another bucket |
| `Strategy` protocol and one decision | `bot/config.py`, `bot/decision.py` | Live and backtest share the tick decision; only the context changes |
| The gateway flow | `execution/gateway.py` `submit` -> `_authorize` -> `_execute` | Idempotency, policy, ledger and execute are general; `execute` is already a callable |
| Pure policy | `policy/policy.py` `evaluate` | New rules are new functions in the tuple |
| `Executor` protocol | `providers/jupiter/executor.py` | Paper vs real is already pluggable, for swaps |
| Layer map | `tests/test_architecture.py` | New modules get a layer and stay out of strategy code |
| Process split and lock | `trader/execution/runner.py`, `trader/strategy/runner.py`, `trader/api/cli/lock.py` | One trade-runner per mode holds every bucket, spot or perp |

### 4.2 Where spot is baked in (fix before perps, or with them)

| # | Coupling | Where | Kind | Effort |
|---|---|---|---|---|
| C1 | Execution imports the **concrete** `AsyncJupiterProvider` (no venue protocol) | `execution/account.py:29`, `execution/balances.py:15`, `execution/fills.py:19`, `trading_service/service.py:35`, `wiring.py` | Accidental | S |
| C2 | `AsyncAccount` (434 lines) mixes the wallet, the bucket limits, intent building, fill -> `Order` and the book, all for a spot pair | `execution/account.py` | Accidental | M |
| C3 | The intent, the result and the order are **swap-shaped**: `spend_mint`/`receive_mint`/`spend_amount`, `SwapResult(in_amount, out_amount)`, `Order(input_mint, output_mint, quote_amount)`. The ledger columns and `mark_executed` copy them; the policy's `_mints` reads them | `models/intent.py`, `models/order.py`, `ledger/store.py`, `ledger/intents.py:142`, `policy/policy.py:225` | Mostly accidental (a perp also has amounts in and out, but no "receive mint") | M |
| C4 | **Long-only math**: `Position.unrealized_*`, `realized_pnl_percent`; stops and targets compare `price <= entry x (1 - pct)` and track a `peak` | `models/position.py`, `strategy_spec/conditions.py:147-173`, `strategy_spec/strategy.py` `_track` | Inherent, but small with a direction on the context | S-M |
| C5 | A bucket's money is "the wallet balance of the input mint"; allocation counts position cost; reconcile compares **wallet token balances** with ledger positions | `trading_service/service.py` `get_bucket`, `_check_allocation`, `_reconcile_wallet`; `gateway.open_positions` | Inherent for reconcile (perps live in position accounts), accidental for the rest | M |
| C6 | Backtests and paper simulate a **wallet of tokens** (`SimulatedWallet`, `ReplayQuoteClient`); equity = token balances x price | `paper/`, `backtest/replay.py` | Inherent: perps need a simulated margin engine | M |
| C7 | Spec format: `symbol` is `OUTPUT-INPUT`, `sizing` is "USD per buy", no direction or leverage. Adding a field with a default **changes every spec id** (`canonical_json` dumps defaults) | `strategy_spec/models.py:370, 438` | Accidental for the id; the rest is inherent | S |

### 4.3 Verdict

Coupling makes perps **harder than they need to be, but not blocked**. Without
P0, perps would mean `if instrument == "perp"` branches inside
`AsyncAccount`, `Position`, the gateway restore and the service, which is
exactly the kind of code the owner cut in stage U. With P0, perps become a
second implementation behind two seams (the venue and the bucket account) plus
a direction on the strategy context.

| Area | Size |
|---|---|
| P0 decoupling (no behaviour change) | M (several modules) |
| Perp model, paper engine, spec format, policy | L |
| Perp backtest | M |
| Jupiter Perps read-only adapter (decode accounts) | M |
| Jupiter Perps execution (requests, keeper wait, on-chain stop) | L |
| **Total** | **roughly the size of stage B3 + B2 + B5 together** |

## 5. Decisions

D1, D2 (both directions), D7, D10 (leverage) and D11 were decided by the owner
on 2026-10-03; the rest are the design.

- **D1. Venue:** Jupiter Perps, SOL market first (§3).
- **D2. Long and short; direction and leverage live in the spec, not in the
  order.** A spec has
  `"market": {"kind": "perp", "direction": "short", "leverage": 3}`; omitted
  means spot. The spec chooses its leverage; the policy caps it (D10). `OrderSide.BUY`/`SELL` keep meaning **enter**/**exit** the
  bucket's position, so `OrderRequest`, the wire protocol and `decision.py` are
  unchanged. One direction per bucket.
- **D3. Direction-aware conditions, written once.** `TickContext` gets
  `direction`; `peak` becomes "best price since entry" (the low for a short),
  and the five position blocks (`stop_loss`, `trailing_stop`, `take_profit`,
  `trailing_take_profit`, `below_last_exit`) compare in the favourable
  direction. Market blocks (`rsi_below`, MAs, ...) don't change: the author
  picks them for a short. Spot is `direction = long`, so spot results don't
  change (the backtest headline is the regression check).
- **D4. Two seams.**
  - `Venue` (core protocol, venue layer implements): `balances()`,
    `native_fee_reserve`, and per instrument `open(...)`/`close(...)` returning
    an `ExecutionResult`, `fetch_costs(result)` (never raises), and
    `positions()` for reconcile. `SpotVenue` wraps today's
    `AsyncJupiterProvider` unchanged; `JupiterPerpsVenue` and
    `SimulatedPerpsVenue` are new.
  - `BucketAccount` (execution protocol): what `TradeService` needs from a
    bucket: `book`, `enter`, `exit`, `available_usd()`, `committed_usd()`
    (allocation), `held()` (reconcile), `restore_from_ledger()`. `SpotAccount`
    is today's `AsyncAccount` cut down; `PerpAccount` is new.
- **D5. Money in a perp bucket is collateral.** `sizing.usd` and `budget_usd`
  count **collateral**; exposure is `collateral x leverage`. The policy's
  `notional_usd` for a perp intent is the **exposure** (`max_trade_usd` and
  the daily notional limit then bound real risk). A liquidation is a realized
  loss of the whole collateral and counts toward `max_loss_usd`.
- **D6. One ledger, a perp column set, one schema bump.** `TradeIntent` gets
  `instrument` (`spot`/`perp`) and an optional `PerpTerms` (market, direction,
  leverage, collateral, size, request account). The ledger stores perp terms
  and fills in a `perp_json` column plus `instrument`; `SCHEMA_VERSION` 2 -> 3.
  Older ledgers are refused as today, so **the bump waits until the owner's
  paper soak ends** (D11). The policy's daily totals and the breaker keep
  spanning both instruments.
- **D7. A stop that doesn't need our process (required).** Every real perp
  open also places a venue-side stop-loss trigger (Jupiter TP/SL request on the oracle
  price) at the spec's stop, or at half the distance to liquidation if that is
  closer. It is cancelled on a normal exit. Spot never needed this: a stopped
  spot bot holds tokens, while a stopped perp bot can be liquidated. Paper
  simulates it. If the stop can't be placed, the bot closes the position it
  just opened and records a `perp_stop_failed` event: a real perp position
  never stays open without a venue stop.
- **D8. Two-phase execution maps onto the intent lifecycle**, with no new
  status:
  - the request transaction fails on-chain -> retry, as today
    (`TransactionFailedOnChainError`);
  - the request confirms and the keeper fills it before a deadline (60s) ->
    `EXECUTED`;
  - the keeper rejects it (slippage, oracle), so the request closes and
    the collateral comes back -> `FAILED` with `recusado:` (doesn't count
    toward the breaker);
  - the deadline passes with the request still open, or anything fails after
    our send -> `UNCONFIRMED` (blocks the mode, as today).
  The request account's `counter` seed comes from the idempotency key, so a
  resend after a crash finds the same request instead of creating a second.
  Before any resend, the adapter reads the position size (one position
  account per market and side: a second fill makes the position bigger, not a
  new one).
- **D9. Accounting.** `PerpPosition`: direction, entry price, size, collateral,
  open fee, liquidation price. Realized PnL is **collateral returned minus
  collateral posted**, in USD, from the actual token deltas of the close, plus
  network fees in SOL as today. Borrow fees and price-impact fees are already
  inside that amount and are **never subtracted again** (the same rule as LP
  fees and slippage on spot). Unrealized PnL (B5's mark-to-market) adds the
  accrued borrow fee and the close fee estimate.
- **D10. Risk rules.**
  - `perps_enabled = false` by default in real; on in paper.
  - `max_leverage` in `policy.toml`, **default 3 in every mode**; the owner
    can raise it (`[limits]` for every mode, `[paper.*]`/`[real.*]` for one).
    `allowed_perp_markets`.
  - Spec validation (`run`, and `hello` in `serve` with the trade-runner's own
    policy): `leverage <= max_leverage`, otherwise the spec is refused; the
    gateway checks it again on each intent. The stop must trigger well
    before liquidation (stop distance at most half of `1 / leverage`, after
    fees); a perp spec needs a `max_hold` exit, so borrow fees can't eat the
    collateral unnoticed.
  - Reconcile: ledger perp positions against the venue's position accounts
    (`venue.positions()`), not wallet balances. A position missing on the
    venue is booked as liquidated or closed outside the bot (`perp_mismatch`
    event); entries in that market stop until the owner checks.
- **D11. Sequencing: perps come after B6** (§7).

## 6. Design details

### 6.1 Spec format

```json
"symbol": "SOL-USDC",
"market": {"kind": "perp", "venue": "jupiter", "direction": "short", "leverage": 3},
"sizing": {"type": "fixed_usd", "usd": 20},
```

- `symbol` keeps `BASE-QUOTE`: the base is the perp market, the quote is the
  stable the bucket pays and is paid in.
- `market` is excluded from `canonical_json` when it is spot, so **existing
  spec ids don't change** (a test pins one).
- New rows in `docs/specs.md`; `tests/test_spec_docs.py` keeps them in step.

### 6.2 Paper and backtest engine

`trader/execution/venues/paper/perps.py` (venue layer), shared by paper and backtest:
- positions kept in `paper-wallet.json` under a `perps` key, under the
  existing file lock;
- open/close at the feed price plus the Jupiter fee model: 0.06% of size each
  way, a linear price-impact fee, hourly borrow at a configurable rate (paper
  reads the live rate from the custody account once P3 exists; backtests take
  `--borrow-bps-hour`);
- liquidation checked on every price it sees; in paper the trade-runner's 30s
  sweep also checks open perp positions, so a liquidation happens even with no
  strategy connected;
- the venue-side stop (D7) is simulated the same way.

The backtest replays the same interpolated candle paths. Spot candles stand in
for the oracle price; the difference is noted in the output, since historical
oracle prices and borrow rates have no public API.

### 6.3 Real adapter (Jupiter Perps)

- **Read-only first (P3):** decode `Pool`, `Custody` (oracle price, borrow
  rate, utilization), `Position` and `PositionRequest` from the IDL with
  `getAccountInfo` at Confirmed. `Position` is a PDA from owner, custody and
  collateral custody, so it can be read without `getProgramAccounts` (heavy,
  often not allowed on cheaper RPC plans). These reads go into `tests/live/`.
- **Execution (P4):** build increase/decrease request instructions, sign and
  send through the existing RPC client, wait for the keeper (D8), read the
  position for amounts and fees, place and cancel the venue stop (D7). Real
  amounts come from the confirmed transactions, as on spot.
- B7's transaction inspection must allow the perps program and its accounts.
- The guard hook needs no change: it already blocks every real run from agent
  sessions.

### 6.4 Failure modes it must handle

| Failure | Behaviour |
|---|---|
| Keeper slow or down | Deadline -> UNCONFIRMED; the venue stop still protects the position |
| Keeper rejects | FAILED `recusado:`; nothing moved |
| Oracle outage | No fills, no liquidations on the venue; the bot's entries are denied when the venue reports a stale oracle |
| Liquidated while the bot is down | Restart reconcile finds the position missing -> books the collateral as lost, `perp_mismatch` event |
| Venue gone | Entries blocked; the owner closes positions by hand; a later adapter can replace it (D4) |
| Borrow fees creeping | `max_hold` required; the daily report shows accrued borrow |

## 7. Interaction with the active plan

B5 was finished on 2026-10-03, while this plan was being written. B6 (more
expressive strategies) is next, and the owner chose **perps after B6**. The
owner's paper soak hasn't started, and no stage so far has changed the ledger
schema, so the soak's ledgers keep opening.

Rules:
1. **Nothing here starts before B6 is merged with the suite green.** B6
   changes the spec format (percent sizing, `expr`, non-stable inputs) and the
   bucket sizing that P0 and P1 build on. Starting before it would mean
   designing the `market` block and `PerpAccount` twice.
2. **P0 may run during the soak**: it changes no behaviour and no schema, and
   the soak's results stay comparable (the backtest headline of the example
   specs must not change).
3. **The schema bump (P1) waits for the end of the soak**, and takes any other
   pending schema change with it, so ledgers are only refused once.
4. P1 builds on what B6 leaves: percent sizing is a percent of the bucket's
   **collateral**, an `expr` condition works for perps unchanged, and B6's
   non-stable inputs don't apply to perps (perp buckets pay in a stable).
5. B5's mark-to-market and daily report get a perp branch in P1 (accrued
   borrow, liquidation distance), not a second report.

Order: **B6 -> P0 (may overlap the soak) -> soak ends -> P1 -> P2 -> P3 ->
P4 -> P5.** P3 (read-only) needs no schema change and can run any time after
P0. Real-money perps come after a paper soak of perp specs, as for spot.

## 8. Phases

Each phase ships on its own with the suite green (`ruff check`, `ruff format
--check`, `pyright`, `pytest`) and `docs/` updated. Size: **S** under an
hour, **M** several modules, **L** a design change.

#### P0. Decoupling, no behaviour change — M
1. `Venue` protocol in core; execution imports it instead of
   `AsyncJupiterProvider` (C1). `SpotVenue` is a thin wrapper.
2. `ExecutionResult` (signature, raw amounts in/out, venue payload) replaces
   `SwapResult` in the gateway and `mark_executed`; `SwapResult` stays inside
   the spot venue (C3, the part that needs no schema change).
3. `BucketAccount` protocol; `AsyncAccount` becomes `SpotAccount` and loses
   what `TradeService` can own (allocation and reconcile inputs through
   `committed_usd()`/`held()`) (C2, C5).
4. `direction` on `TickContext` and on `Position` (default long); the five
   position blocks and `_track` use it (C4). Tests run them for both
   directions.
5. `canonical_json` excludes default-valued new fields (C7), with a test that
   pins the ids of `docs/examples/`.
Done when: the example specs' backtest results are identical before and
after, and `tests/test_architecture.py` maps the new modules.

#### P1. Perp model, paper and spec — L (after the soak)
- `market` block in the spec (+ `docs/specs.md`), validation against the
  policy (D10).
- `PerpTerms`, `PerpPosition`, `PerpAccount`; ledger schema 3 (D6); restore
  of an open perp position.
- `SimulatedPerpsVenue` (§6.2), the liquidation sweep in `serve`/`run`.
- Policy: `perps_enabled`, `max_leverage` (default 3 in every mode),
  `allowed_perp_markets`, exposure as notional. Tests: a spec above the cap is
  refused by `run` and by `serve`'s `hello`; an intent above it is denied.
- B5's report and notifications show direction, leverage, liquidation price
  and accrued borrow.
- Done when: `run paper` on a short spec opens, holds, stops out and is
  liquidated in a forced test, with the ledger and PnL right after a restart.

#### P2. Perp backtest — M
- The replay uses the perp engine; `--borrow-bps-hour`; liquidation on the
  candle path; the summary shows liquidations and fees paid.
- Two example specs (`docs/examples/spec-sol-short-*.json`).

#### P3. Jupiter Perps read-only — M
- IDL decoding (pick `anchorpy` vs hand-written Borsh after a spike),
  `positions()`, oracle price, borrow rate; paper uses the live borrow rate.
- `tests/live/`: read the pool and custody accounts, decode an empty
  position for the paper wallet's public key.

#### P4. Jupiter Perps execution — L
- Requests, keeper wait, idempotent `counter` (D8); the required venue stop,
  and closing the position if it can't be placed (D7); reconcile against
  position accounts (D10).
- Real perps stay off (`perps_enabled = false`) until the owner opens and
  closes a minimal position by hand through `run real` with a tiny budget.

#### P5. Hardening — M
- Oracle-staleness checks; entries denied on a stale venue oracle.
- Partial closes; adding collateral to move the liquidation price away.
- Shared perp state reads for many strategy-runners (with B7's price hub).

#### Progress

| Phase | Status | Notes |
|---|---|---|
| Research and plan | **done** (2026-10-03) | This document |
| P0 Decoupling | not started | Waits for B6 to be merged |
| P1 Perp model, paper, spec | not started | Waits for P0 and the end of the paper soak (schema 3) |
| P2 Perp backtest | not started | |
| P3 Jupiter Perps read-only | not started | Can start any time after P0 (no schema change) |
| P4 Jupiter Perps execution | not started | |
| P5 Hardening | not started | |

## 9. Owner decisions (2026-10-03)

| Question | Answer |
|---|---|
| Venue: Jupiter Perps or Pacifica? | **Jupiter Perps** (D1) |
| Directions: both, or shorts only? | **Long and short** (D2) |
| Leverage cap | **Set by the spec, capped by `max_leverage` in `policy.toml`, default 3** (D2, D10) |
| Venue-side stop on every real perp position? | **Yes, required** (D7) |
| Perps before or after B6? | **After B6** (D11, §7) |

Still open: none. New questions go here as phases start.

## 10. Decision log

- **2026-10-03:** perps researched and planned (this document). Jupiter Perps
  proposed as the venue (no REST API: Anchor IDL plus our own transactions;
  two-phase keeper flow). Velocity (formerly Drift) rejected for now: hacked in
  April 2026, reopened days ago, Python SDK archived. Coupling assessment:
  layering is sound; the domain model is swap-shaped end to end, so a P0
  refactor (venue and bucket-account seams, direction on the context) comes
  before any perp code. Nothing starts before B5 is merged; the schema bump
  waits for the end of the paper soak.
- **2026-10-03 (owner):** Jupiter Perps; long and short; leverage set by the
  spec and capped by `max_leverage` in `policy.toml` (default 3, every mode);
  a venue-side stop is required on every real perp position (if it can't be
  placed, the position is closed); perps start after B6.

## 11. Sources

Checked on 2026-10-03.
- Jupiter Perps docs: https://docs.jup.ag/user-docs/trade/perps (fees,
  liquidation, positions and collateral, technical reference, FAQ) and
  https://developers.jup.ag/docs/perps (position and position-request
  accounts).
- IDL and account constants (community, TypeScript):
  https://github.com/julianfssen/jupiter-perps-anchor-idl-parsing
- Jupiter beta markets (GUM order book):
  https://solanacompass.com/news/jupiter-perps-adds-six-markets-including-tokenized-spacex-hype-and-zec-via-gum-orderbook
- Velocity/Drift: https://www.chainalysis.com/blog/lessons-from-the-drift-hack/,
  https://docs.velocity.exchange/developers, https://github.com/drift-labs/driftpy
- Pacifica: https://docs.pacifica.fi/api-documentation/api/rest-api
- Venue comparison: https://madeonsol.com/blog/solana-perp-dexs-compared

Not verified: keeper latency (the docs only say "a short delay"), the maximum
number of open positions per wallet (the docs say 6 in one place and 9 in
another), and whether historical borrow rates can be downloaded anywhere.
