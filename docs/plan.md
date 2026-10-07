# Plan

Renewed on 2026-10-05. This is the only roadmap. Items are numbered from
**A1** again: everything finished before 2026-10-05 (stages A to T and B,
whose labels code comments still cite) is in [`history.md`](history.md). For
how the code works today, read [`architecture.md`](architecture.md).

Plan here first, then implement: `/phase` writes an item's full design under
it before the code. Track progress in §5, record decisions in §9.

---

## 1. End goal

1. **Strategies are spec files written from the docs.** The owner or an
   agent writes a JSON spec following [`specs.md`](specs.md), backtests it,
   runs it in paper, and iterates on the file and the code. Nobody needs a
   command to create, submit or register a strategy.
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
6. **Perpetual futures.** A spec can hold a long or short perp position with
   leverage, through the same gateway, policy, ledger and buckets (design in
   §8).

## 2. Where we are

**Overall: about 94%** (the average of goals 1, 3, 4 and 5; goal 2 is deferred
and goal 6 is scored on its own so a new goal doesn't hide progress). 829
tests. The first real run (A6, 2026-10-06/07) is done: 5 JUP-SOL round
trips on a ~5 USD wallet, nothing UNCONFIRMED or failed, costs at or below
the backtest's, and the token account's rent refunded at retirement.

| Goal | Done | Missing | Score |
|---|---|---|---|
| 1. Specs as files | `docs/specs.md` is the authoring contract (a test keeps it in step with the code); `backtest`, `/smoke`, `serve` + `connect` for any number of specs; the trade-runner checks each spec's terms against its own policy | Only a tiny-budget spec has traded real money (A6) | **95%** |
| 2. Manual trades | Removed in stage U | Comes back with a wallet treasury | deferred |
| 3. Structured building | 18 condition types plus `expr`; `fixed_usd` and `pct_of_bucket`; any registry token as the input; required stop, warm-up (across candle gaps), re-arm, `ttl_days`; backtests with measured costs, direction-aware candle paths and two series for non-stable pairs | No crossovers | **90%** |
| 4. One wallet, bucket per strategy | One `serve` holds every bucket of a mode; budget and max-loss caps; budgets must fit the wallet; startup reconcile of every bucket's positions; atomic authorization; three buckets traded side by side in the paper soak (A2) | Nothing planned: one wallet is the decision (2026-10-07) | **95%** |
| 5. Accurate trades and costs | Real amounts and fees from the confirmed tx; rent; failed-tx fees; net PnL; USD values on every pair; one priority-fee cap across real, paper and backtest; cost per round trip in every report; daily report; live vs backtest; intents killed mid-swap resolved from their logged sends, fees included | Replays ignore rent | **96%** |
| 6. Perps | Research, venue choice and design (§8); the seams (A7: `Venue`, `BucketAccount`, `ExecutionResult`, direction-aware position blocks, pinned spec ids) | Everything else (A8–A12) | **15%** |
| (Foundations) | Policy, breaker, idempotency, UNCONFIRMED blocking and resolution, key only in `serve`, transaction inspection, quote check against the Price API, no stale prices, agent-session guard hook, enforced layering | A service entrypoint | **95%** |

## 3. Principles and decisions that stand

- **Strategies are files.** Agents and the owner write spec files from
  `docs/specs.md`; agents never trade, touch the key or edit the policy. The
  owner starts every run; real also needs `real_trading_enabled`.
- **The policy decides deterministically.** `evaluate()` is pure, owned by the
  owner and wallet-wide; per-strategy budgets are the bucket's job.
- **Execute exactly once.** One broadcast per idempotency key; nothing is
  retried after a send.
- **The ledger is the source of truth** for positions, PnL, budgets and the
  audit trail, and is reconciled against the wallet.
- **Fail closed.** No ledger, a broken policy or a stale price means no trade.

| Topic | Decision |
|---|---|
| Strategy form | A declarative JSON spec from vetted blocks plus a restricted `expr`. Never agent-written Python, never `eval`. |
| Interface | Files and docs. Agents don't use a CLI; the owner runs `serve`, `connect` and `backtest`. |
| Mints | Owner registry only (`SOLANA_MINTS` ∩ `allowed_symbols`). |
| Processes | One `serve` per mode (the only process with the key, wallet and ledger) and one `connect` per spec. Strategies are mode-agnostic. |
| Buckets | `strategy:<spec_id>`; with no open position a bucket may spend `max(0, budget_usd + min(0, realized_usd))`; long-only, one position at a time; exits on retire, expiry or max loss are sold by `serve` itself. |
| Spec storage | Files in the repo (`docs/examples/`); the id is the hash of the behaviour, so each version gets its own bucket. |
| Wallet | One wallet for all strategies (owner, 2026-10-07). Real mode uses a dedicated low-balance hot wallet. |
| Approval | A trade inside the owner's policy and the spec's terms (budget, max loss, largest buy) is approved by the owner; no per-trade approval step (owner, 2026-10-07). |
| Ledger schema | One `_SCHEMA` with a `user_version`, no migrations; a schema change waits for the end of a paper soak and takes every pending change with it. |

## 4. Roadmap

In order. Each item ships on its own with the suite green (`/check`). Size:
**S** under an hour, **M** several modules, **L** a design change.

### A7–A12. Perps
The design is in §8. A7 (the seams, done 2026-10-07) is in `history.md`; A8
bumps the ledger schema, so it waits for the end of a paper soak.

- **A8. Perp model, paper and spec — L.** The `market` block (+ `specs.md`)
  checked against the policy at `hello`; `PerpTerms`, `PerpPosition`,
  `PerpAccount` (a `BucketAccount`, A7; the wallet reconcile leaves perp
  positions out, through a `held()` on the protocol; `TradeService` gets
  the account kind from the venue instead of building a `SpotAccount`, and
  the direction is stored with the entry, not only on `Position`); ledger schema +1 (D6) and restore of an open perp position;
  `SimulatedPerpsVenue` and the liquidation check in `serve`'s sweep; policy
  `perps_enabled`, `max_leverage` (default 3), `allowed_perp_markets`,
  exposure as notional; the daily report and notifications show direction,
  leverage, liquidation price and accrued borrow. Done when a short spec in
  paper opens, holds, stops out and is liquidated in a forced test, with the
  ledger and PnL right after a restart.
- **A9. Perp backtest — M.** The replay uses the perp engine;
  `--borrow-bps-hour`; liquidation on the candle path; the summary shows
  liquidations and fees; two example specs.
- **A10. Jupiter Perps read-only — M.** IDL decoding (`anchorpy` or
  hand-written Borsh, after a spike); `positions()`, oracle price, borrow rate;
  paper uses the live borrow rate; live tests read the pool and custody and an
  empty position. No schema change: can run any time after A7.
- **A11. Jupiter Perps execution — L.** Requests, keeper wait, idempotent
  `counter` (D8); the required venue stop, closing the position if it can't be
  placed (D7); reconcile against position accounts (D10). Real perps stay off
  until the owner opens and closes a minimal position with a tiny budget.
- **A12. Hardening — M.** Oracle staleness (entries denied on a stale venue
  oracle); partial closes; adding collateral; shared perp state reads through
  the price hub.

### Backlog (numbered when scheduled)
- One round trip per `connect` tick (old B10 C7).
- A service entrypoint (systemd, Docker or a Windows service).
- Crossovers in specs (an `expr` only compares levels on the current bar).
- Rent of new token accounts in replays.
- Manual trades (goal 2), with a wallet treasury.

## 5. Progress

| Item | Status | Notes |
|---|---|---|
| A1 Soak quick fixes | done | 2026-10-05; in `history.md` |
| A2 Owner: paper soak | done | 2026-10-05, ended early by the owner; in `history.md` (F9-F13 -> A13, A14) |
| A3 Resolve UNCONFIRMED | done | 2026-10-05; in `history.md` |
| A4 Warm-up across candle gaps | done | 2026-10-05; in `history.md` |
| A5 Wallet re-read rule | done | 2026-10-05; in `history.md` |
| A13 Soak II small fixes | done | 2026-10-05; in `history.md` |
| A14 Replay fills like live fills | done | 2026-10-05; in `history.md` |
| A6 First real run | done | 2026-10-07; in `history.md` (run 1 findings -> A15; run 2 clean; rent refunded at expiry) |
| A15 First real run fixes | done | 2026-10-06; in `history.md` |
| A7 Perps: decoupling | done | 2026-10-07; in `history.md` |
| A8 Perps: model, paper, spec | open | Schema bump; after a soak |
| A9 Perps: backtest | open | |
| A10 Perps: Jupiter read-only | open | Any time after A7 |
| A11 Perps: Jupiter execution | open | |
| A12 Perps: hardening | open | |

## 6. Known issues and limitations

### 6.1 Money and accounting
- **Costs and net PnL.** After each swap the costs actually paid are recorded:
  the network fee (base + priority, from `meta.fee`), rent for token accounts
  the wallet opened (a cost, marked refundable), and other SOL the route
  charged. Attempts the chain confirmed as failed are a `failed_tx_fee` event
  and come off the bucket's realized PnL (an unreadable fee counts as the
  5000-lamport base fee). An intent resolved after a restart books the fee
  of its failed sends too (A3).
- Real mode reads the confirmed transaction, including the real in/out
  amounts. Paper simulates the base fee plus the priority-fee cap and
  first-time rent, and fills 10 bps below the quote (never below its
  `otherAmountThreshold`).
- LP/AMM fees, price impact and slippage are already in the actual amounts and
  are never subtracted again.
- PnL is kept in native units with a USD estimate. What the trade can't price
  comes from a Price API snapshot taken **before** the trade; if the API is
  down, costs are flagged `[!] incompleto` and a buy with an unknown USD value
  is denied unless `allow_unknown_notional = true`.
- **Two units.** The strategy side works in the pair's quote token; budgets,
  PnL and the policy are USD. On a non-stable quote without a price, a
  budgeted bucket offers 0. A backtest's return on such a pair includes the
  quote token's own USD move.
- **No commands to inspect or repair the ledger.** `serve` resolves
  UNCONFIRMED/EXECUTING intents from their logged sends (A3); one still
  pending on the chain, or from a build before A3, blocks the mode until the
  owner moves or deletes the file;
  `.claude/scripts/ledger_dump.py` prints it read-only.
- **Replays** measure fees on Jupiter now (or take flags), not at the time of
  the candles; the rent of a token account is not modelled. The network
  fee is a fixed USD amount per leg. The live vs
  backtest comparison assumes the bucket started the window flat with its
  whole budget.
- **One tick per second** from the hub: conditions that count ticks, like
  `random_chance`, fire at a steady rate.
- **The daily report needs Telegram**; without it the day is still marked as
  reported.

### 6.2 External APIs
- Quotes and swaps use `api.jup.ag` `/swap/v1` (not `/swap/v2/order`: with a
  `taker` it rejects an explicit `slippageBps`). `priceImpactPct` is a
  fraction.
- The price websocket and candles are undocumented frontend endpoints; prices
  fall back to the Price API V3, candles have none.
- Keyless requests hit `429` under bursts; only `serve` talks to Jupiter, so
  N `connect`s add no load beyond their warm-up through the `candles` op.

### 6.3 Security
- The key lives only in `serve`; `connect` has none.
- `str(Keypair)` is the base58 secret. Never format a keypair with `str()`
  or `{keypair!s}`.

## 7. Risks and open questions

Top risks:
1. **A runaway or prompt-injected agent.** Agents only write spec files;
   policy and budgets bound every trade; the guard hook keeps agent sessions
   out of real mode.
2. **Double execution or lost confirmations.** Idempotency keys, no re-send
   after broadcast, every send logged first, UNCONFIRMED blocking until resolved.
3. **Key compromise.** The key only in `serve`; a low-balance hot wallet.
4. **Jupiter changes undocumented endpoints.** Price API fallback;
   configurable hosts.
5. **Buckets drifting from the wallet.** Allocation and the startup reconcile.
6. **Strategy quality.** Backtests on real candles, paper before real.
7. **Perps venue risk.** In 2026 one Solana perps venue was hacked and two
   shut down: the venue sits behind a seam (D4).

Open questions for the owner (A6 answered pairs and limits for the first
run; the Helius key and the wallet were settled on 2026-10-07, §9):
- The next real budget: the proposal at the end of A6 in `history.md` (a
  10-15 USD wallet, 5 USD a buy, `max_daily_loss_usd = 3`).

## 8. Perps design

Moved here from `perps.md` on 2026-10-05; the owner's decisions are from
2026-10-03. Numbers are for Jupiter Perps (the JLP pool), checked that day.

### 8.1 Scope
One perp market per spec, one direction per spec (`long` or `short`), one
position per bucket. Market entry and exit; spec-level stops and exits as
today, plus a stop the venue holds on-chain (D7). Paper, backtest, then real
on one venue. Out of scope: flipping direction in a bucket, changing
collateral of an open position, limit orders, cross-margin, several venues,
funding-rate strategies.

### 8.2 Perps in one page

| Concept | Spot today | Perp |
|---|---|---|
| Direction | Long only | Long or short; a short profits when the price falls |
| Exposure | What you spent | `size_usd = collateral_usd x leverage` (1.1x to 250x on Jupiter) |
| What you hold | Tokens in the wallet | A position account on the venue; the wallet holds only unposted collateral |
| PnL | `quote received - quote spent - costs` | `direction x (exit - entry) / entry x size_usd - fees`, settled in collateral |
| Running costs | None | Hourly **borrow fee** from collateral (utilization x hourly rate x size) |
| Trade fees | LP fees inside the amounts, network fee | 0.06% of size on open and close, a price-impact fee, the network fee |
| Losing it all | The token goes to zero | **Liquidation** at maintenance margin (0.2% of size): the venue keeps all remaining collateral; borrow fees move the liquidation price closer every hour |
| Price | Jupiter quote | The venue's oracle (Edge by Chaos Labs, Chainlink and Pyth as checks) |
| Execution | One transaction | **Two phases**: our transaction creates a `PositionRequest`, a Jupiter keeper fills or rejects it later |

Liquidation price: `liq = entry -/+ |collateral - close_fee - borrow_fee -
size/500| x entry / size` (minus for longs). Without fees, 3x liquidates after
a ~33% adverse move, 10x after ~10%. A long posts the asset, a short a stable;
any input is swapped automatically and a close can pay out in a chosen token,
so a long funded and closed in USDC keeps the bucket in USD.

### 8.3 Venue

| Venue | State (2026-10) | Fit |
|---|---|---|
| **Jupiter Perps** (SOL, ETH, wBTC) | Active, the largest remaining Solana perps venue; no REST API: instructions built from the Anchor IDL (program `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu`), signed with our key, sent through Helius | **Chosen (D1)**: same key, RPC, custody and confirmation model as spot |
| Pacifica | Active, REST + websocket, CCXT, testnet | Second choice: custody not fully on-chain |
| Jupiter GUM beta | New order book, JUP stakers only, no API | Not now |
| Velocity (ex-Drift) | Reopened after a $285M hack; Python SDK archived | Too risky |
| Flash Trade, Zeta | Closed | No |

### 8.4 How coupled the code is
About 60% of the work is inherent (direction, leverage, liquidation, running
fees, two-phase execution) and 40% is accidental coupling that A7 removes
first. The layering is sound; the coupling is in the domain model ("a trade is
a swap of one mint for another").

Already venue-agnostic: the strategy-side boundary
(`trader/shared/trading_service/protocol.py`,
`trader/strategy/trading_service/client.py`), the `Strategy` protocol and one
decision (`trader/strategy/bot/config.py`, `decision.py`), the gateway flow
(`trader/execution/trade/gateway/gateway.py`), the pure policy, the `Executor`
protocol (`trader/execution/trade/venues/jupiter/executor.py`), the layer map,
and the process split.

| # | Coupling | Where | Kind | Effort |
|---|---|---|---|---|
| C1 | Execution imports the concrete `AsyncJupiterProvider` | `trade/gateway/account.py`, `balances.py`, `fills.py`, `trade/trading_service/service.py`, `wiring.py` | Accidental | S |
| C2 | `AsyncAccount` mixes wallet, bucket limits, intents, fills and the book, all spot | `trade/gateway/account.py` | Accidental | M |
| C3 | Intent, result and order are swap-shaped (`spend_mint`/`receive_mint`, `SwapResult`, `Order.input_mint`); the ledger and the policy read them | `execution/models/intent.py`, `shared/models/order.py`, `trade/ledger/store.py`, `intents.py`, `trade/policy/policy.py` | Mostly accidental | M |
| C4 | Long-only math in positions, stops, targets and `peak` | `shared/models/position.py`, `strategy/spec/conditions.py`, `strategy.py` | Inherent, small with a direction | S-M |
| C5 | A bucket's money is the wallet balance of the input mint; reconcile compares wallet balances | `trade/trading_service/service.py` | Inherent for reconcile | M |
| C6 | Paper and replays simulate a wallet of tokens | `trade/venues/paper/`, `trader/backtest/replay.py` | Inherent: needs a margin engine | M |
| C7 | Spec has no direction or leverage; a new field with a default changes every spec id | `strategy/spec/models.py` | Accidental for the id | S |

Total: roughly the size of the old B3 + B2 + B5 together. **After A7:** C1
is gone (execution sees only `Venue`), C2 is split behind `BucketAccount`,
C4 has its direction, C7 has pinned ids; C3, C5, C6 and the `market` field
are A8's.

### 8.5 Decisions (D1–D11)
- **D1. Venue:** Jupiter Perps, SOL first, then ETH and wBTC.
- **D2. Long and short; direction and leverage in the spec:**
  `"market": {"kind": "perp", "venue": "jupiter", "direction": "short",
  "leverage": 3}`; omitted means spot. BUY/SELL keep meaning enter/exit, so the
  wire protocol and `decision.py` don't change.
- **D3. Direction-aware conditions, written once:** `TickContext.direction`;
  `peak` is the best price since entry; the five position blocks compare in the
  favourable direction; market blocks don't change. Spot is long, so its
  results don't change.
- **D4. Two seams:** `Venue` (`balances()`, `open`/`close` returning an
  `ExecutionResult`, `fetch_costs`, `positions()`), implemented by
  `SpotVenue`, `JupiterPerpsVenue`, `SimulatedPerpsVenue`; and `BucketAccount`
  (`book`, `enter`, `exit`, `available_usd()`, `committed_usd()`, `held()`,
  `restore_from_ledger()`), implemented by `SpotAccount` and `PerpAccount`.
- **D5. Money in a perp bucket is collateral:** `sizing` and `budget_usd`
  count collateral; the policy's notional is the exposure; a liquidation is a
  realized loss of the whole collateral and counts toward `max_loss_usd`.
- **D6. One ledger, one schema bump:** `TradeIntent.instrument` and an
  optional `PerpTerms`, stored in `perp_json` + `instrument`; it waits for the
  end of a paper soak.
- **D7. A venue-side stop is required:** every real perp open places a TP/SL
  stop at the spec's stop or at half the distance to liquidation, whichever is
  closer; cancelled on a normal exit; if it can't be placed, the position is
  closed and a `perp_stop_failed` event recorded.
- **D8. Two-phase execution on the intent lifecycle:** request failed on-chain
  -> retry; keeper fills within 60s -> EXECUTED; keeper rejects -> REJECTED
  (not counted by the breaker); deadline passed or anything failing after our
  send -> UNCONFIRMED. The request's `counter` seed comes from the idempotency
  key, and the position size is read before any resend.
- **D9. Accounting:** realized PnL = collateral returned - collateral posted,
  from the actual token deltas, plus network fees; borrow and price-impact fees
  are inside that amount and never subtracted again. Unrealized adds the
  accrued borrow and the close-fee estimate.
- **D10. Risk:** `perps_enabled` off in real, on in paper; `max_leverage`
  default 3 in every mode; `allowed_perp_markets`; at `hello` and per intent,
  `leverage <= max_leverage`, the stop well before liquidation (at most half
  of `1 / leverage` after fees), a required `max_hold` exit; reconcile against
  the venue's position accounts, a missing one booked as liquidated or closed
  outside the bot (`perp_mismatch`), entries in that market stop.
- **D11. Sequencing:** after the first real spot run (2026-10-05; first set as
  "after B6").

### 8.6 Design details
- **Spec:** `symbol` stays `BASE-QUOTE` (the base is the market, the quote the
  stable the bucket pays in); `market` is left out of `canonical_json` when it
  is spot, so existing ids don't change; new rows in `docs/specs.md`.
- **Paper and backtest engine** (`trader/execution/trade/venues/paper/perps.py`):
  positions under a `perps` key in `paper-wallet.json`; the Jupiter fee model
  (0.06% each way, linear price impact, hourly borrow: live rate in paper after
  A10, `--borrow-bps-hour` in backtests); liquidation and the venue stop checked
  on every price and by `serve`'s 30s sweep. Spot candles stand in for the
  oracle; the output says so.
- **Real adapter:** read-only first (decode `Pool`, `Custody`, `Position`,
  `PositionRequest` with `getAccountInfo` at Confirmed; `Position` is a PDA, no
  `getProgramAccounts`), then requests, the keeper wait and the venue stop.
  Transaction inspection must allow the perps program. The guard hook needs no
  change.

| Failure | Behaviour |
|---|---|
| Keeper slow or down | Deadline -> UNCONFIRMED; the venue stop still protects |
| Keeper rejects | REJECTED; nothing moved |
| Oracle outage | No fills or liquidations; entries denied on a stale oracle |
| Liquidated while the bot is down | Reconcile books the collateral as lost (`perp_mismatch`) |
| Venue gone | Entries blocked; the owner closes by hand; a new adapter behind D4 |
| Borrow fees creeping | `max_hold` required; the daily report shows accrued borrow |

### 8.7 Sources
Checked on 2026-10-03.
- Jupiter Perps: https://docs.jup.ag/user-docs/trade/perps,
  https://developers.jup.ag/docs/perps
- IDL (community, TypeScript):
  https://github.com/julianfssen/jupiter-perps-anchor-idl-parsing
- GUM markets: https://solanacompass.com/news/jupiter-perps-adds-six-markets-including-tokenized-spacex-hype-and-zec-via-gum-orderbook
- Velocity/Drift: https://www.chainalysis.com/blog/lessons-from-the-drift-hack/,
  https://docs.velocity.exchange/developers, https://github.com/drift-labs/driftpy
- Pacifica: https://docs.pacifica.fi/api-documentation/api/rest-api
- Comparison: https://madeonsol.com/blog/solana-perp-dexs-compared

Not verified: keeper latency, the maximum open positions per wallet (6 or 9),
and whether historical borrow rates can be downloaded.

## 9. Decision log

Decisions up to 2026-10-04 are in [`history.md`](history.md).

- **2026-10-03 (owner, perps):** Jupiter Perps; long and short; leverage set
  by the spec and capped by `max_leverage` (default 3, every mode); a
  venue-side stop on every real perp position (closed if it can't be placed).
  Velocity rejected for now (hacked, Python SDK archived). The domain model is
  swap-shaped end to end, so a decoupling step (A7) comes before perp code.
- **2026-10-05:** the plan is renewed: finished work to `history.md`, perps
  folded in from `perps.md`, items numbered from A1. The next goal is a first
  tiny-budget real spot run; resolving UNCONFIRMED intents (A3) is required
  before it, and perps come after it. A keeper rejection maps to REJECTED (the
  status added in old B10) instead of a `recusado:` prefix.
- **2026-10-05 (owner, A2):** the paper soak ends after the second run (about
  3.5 h on the current build, 2 h 22 min with three buckets in one
  `serve`) instead of a week; no end-of-week memory sample. The run was clean;
  its findings are A13 (F10-F13) and A14 (F9). The report moved from
  `soak-test.md` into `history.md` (A2).
- **2026-10-06 (owner, A6 run 1 -> A15):** a token account the bot opened is
  closed when its bucket retires flat, and the refund is recorded (credited
  to the bucket that paid the rent); sells re-read the token account directly
  when the wallet read comes up short; `hpack`/`h2` logs go to WARNING.
- **2026-10-07 (A6 done):** the exit criteria for a bigger budget (5 clean
  round trips, the rent refunded at retirement, live cost per round trip at
  or below the backtest's) all hold; A6 moved to `history.md`. The next real
  budget is the owner's call (§7). Perps (A7) come next; A7 must keep the
  ids of `docs/examples/`, `ad3606394fc0` included.
- **2026-10-07 (owner):** the Helius key rotation is not a concern and is
  dropped from the open questions; one wallet for every strategy stays;
  no owner approval for large trades: a trade that passes the policy and
  the spec's terms is already approved by the owner, so the backlog item is
  removed.
