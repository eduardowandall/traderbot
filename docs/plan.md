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
and goal 6 is scored on its own so a new goal doesn't hide progress). 957
tests. The first real run (A6, 2026-10-06/07) is done: 5 JUP-SOL round
trips on a ~5 USD wallet, nothing UNCONFIRMED or failed, costs at or below
the backtest's, and the token account's rent refunded at retirement.

| Goal | Done | Missing | Score |
|---|---|---|---|
| 1. Specs as files | `docs/specs.md` is the authoring contract (a test keeps it in step with the code); `backtest`, `/smoke`, `serve` + `connect` for any number of specs; the trade-runner checks each spec's terms against its own policy | Only a tiny-budget spec has traded real money (A6) | **95%** |
| 2. Manual trades | Removed in stage U | Comes back with a wallet treasury | deferred |
| 3. Structured building | 18 condition types plus `expr`; a partial exit (A12); `fixed_usd` and `pct_of_bucket`; any registry token as the input; required stop, warm-up (across candle gaps), re-arm, `ttl_days`; backtests with measured costs, direction-aware candle paths and two series for non-stable pairs | No crossovers | **90%** |
| 4. One wallet, bucket per strategy | One `serve` holds every bucket of a mode; budget and max-loss caps; budgets must fit the wallet; startup reconcile of every bucket's positions; atomic authorization; three buckets traded side by side in the paper soak (A2) | Nothing planned: one wallet is the decision (2026-10-07) | **95%** |
| 5. Accurate trades and costs | Real amounts and fees from the confirmed tx; rent; failed-tx fees; net PnL; USD values on every pair; one priority-fee cap across real, paper and backtest; cost per round trip in every report; daily report; live vs backtest; intents killed mid-swap resolved from their logged sends, fees included; token account rent in replays (A19) | Nothing planned | **97%** |
| 6. Perps | Design (§8); the seams (A7); perps in paper (A8) and backtests (A9); Jupiter Perps read live (A10); requests built and simulated (A11a), sent through `serve real`, a real short round trip (A11b, A11c); hardening: the venue stop as an intent that the bot cancels when left over, partial closes, adding collateral, perp intents resolved after a crash, stale-oracle refusal (A12) | The real long run (A11b) | **90%** |
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
The design is in §8. A7 (the seams), A8 (perps in paper, schema 3), A9 (the
perp backtest) and A10 (reading Jupiter Perps) are done (2026-10-07, in
`history.md`); A11c (2026-10-08) and A12 (2026-10-09) too. Left: A11b's
long run.

- **A11. Jupiter Perps execution — L.** Requests, keeper wait, idempotent
  `counter` (D8); the required venue stop, closing the position if it can't be
  placed (D7); reconcile against position accounts (D10). Real perps stay off
  until the owner opens and closes a minimal position with a tiny budget.
  Split by the owner on 2026-10-07 into A11a (build and simulate, nothing
  sent) and A11b (send, keeper wait, first real run); longs and shorts both.
  - **Spike (2026-10-07).** Hand-encoded Anchor instructions
    (`sha256("global:<snake_name>")[:8]` + Borsh params, the IDL's account
    order, an absent optional account passed as the program id) simulate
    cleanly on mainnet (`simulateTransaction`, `sigVerify` off,
    `replaceRecentBlockhash`): `createIncreasePositionMarketRequest` for a
    short (USDC collateral) and for a long (USDC in, swapped by the keeper,
    `jupiterMinimumOut` set), with a public exchange wallet as the payer;
    `createDecreasePositionMarketRequest` (entire position) and
    `createDecreasePositionRequest2` (a trigger: the venue stop) as the owner
    of a public open short, read-only. The trigger needs the custody's
    `dovesAgOracle` (the plain `dovesOracle` fails with 6002) and its Pyth
    account. PDAs: `perpetuals` = `["perpetuals"]`, the event authority
    `["__event_authority"]`, a request `["position_request", position,
    counter LE u64, 1 increase | 2 decrease]` with its token account the ATA
    of the request (off-curve). About 95k compute units. Request creation
    took collateral as small as 1 USD; the keeper's own minimum at execution
    is not known yet (A11b's first run).
- **A11b. Perp requests, sent — L.** Sign and send through `serve real`
  (the key stays there): `JupiterPerpsVenue` (a `PerpVenue`) logs each send
  first (A3), waits for the keeper (60 s, D8), maps a keeper rejection to
  REJECTED and a missing outcome to UNCONFIRMED; places the venue stop after
  every open and closes the position if it can't (D7, `perp_stop_failed`);
  tx inspection allows the perps program; the reconcile runs at start and
  in the sweep; the A3 resolver learns perp requests. Done when the owner
  opens and closes a minimal real perp (both sides) with a tiny budget.
  Notes from the A11a review: sign and inspect through the spot path
  (`AsyncRPCClient.sign_instructions` and its simulation, as
  `close_token_account` does) instead of a second builder; set the compute
  limit from the simulated units and the price from recent fees capped by
  the policy (today every request pays the whole cap); the sweep reads the
  derived position and request addresses in one `getMultipleAccounts`, never
  `getProgramAccounts`; read the position before any resend (a request
  account disappears once executed, D8).
  - **Design (2026-10-07).** Real perps run only in `serve real` with
    `perps_enabled` in `[real]`; paper and the backtest don't change.
    - **Sending** (one path with the spot swaps): `OnChainExecutor` gains
      `send_instructions(instructions, spends)`, the part of
      `close_token_account` that signs (`sign_instructions`), checks the
      programs (the allowlist now takes the perps program when the caller
      passes it), simulates with the wallet's accounts and checks that only
      the given token leaves, up to the given amount (USDC collateral for an
      open, nothing for a close or a stop), logs the send (`announce_send`,
      A3) and sends and confirms with the same error mapping (failed on chain
      / submitted). `close_token_account` uses it too.
    - **Fees:** the compute limit is the units of an unsigned simulation
      x 1.3; the price is the 75th percentile of the recent priority fees on
      the perps pool (`getRecentPrioritizationFees`), capped so the
      priority never passes `max_priority_fee_lamports`.
    - **`JupiterPerpsVenue`** (`trade/venues/jupiter_perps/venue.py`, a
      `PerpVenue`): `open_perp` reads the Doves price (and, for a long, a
      USDC->SOL Jupiter quote for `jupiterMinimumOut`, at the provider's
      slippage), sends the open request, then waits for the keeper: polls
      the request and the position accounts (one `getMultipleAccounts`,
      every 2 s, 60 s). Executed (the position grew): the fill is the
      position account (entry price, size, collateral). The request gone
      and the position unchanged: rejected (`SwapRejectedError`, REJECTED,
      the keeper returns the collateral). Neither in time:
      `TransactionSubmittedError` (UNCONFIRMED). `close_perp` does the same
      with the close request, and its fill is the USDC that came back (the
      owner's USDC account before the send and after the keeper; orders run
      one at a time under the service lock), at the Doves price.
    - **Venue stop (D7):** `PerpVenue.place_stop(terms, entry fill)` (paper:
      nothing to do). `PerpAccount.buy` calls it right after recording the
      open, at the spec's stop or half the distance to liquidation,
      whichever is closer (`PerpTerms` gains `stop_pct`, kept in
      `perp_json`: no schema change). If it fails: event
      `perp_stop_failed` and an immediate sell through the normal path
      (rationale: the venue stop could not be placed). Placed:
      `perp_stop_placed` with the request address. After a close, a stop
      request still on chain is logged and recorded (`perp_stop_left`): the
      owner cancels it in the Jupiter UI (cancelling from the bot is A12).
    - **Exits the venue made** (the stop fired, a liquidation): the sweep's
      `liquidations()` reads the open buckets' position addresses in one
      call; a position gone or at size 0 is an exit: its payout is the
      owner's USDC change in the latest transaction on the position account
      (`getSignaturesForAddress` limit 1 + `getTransaction`), 0 meaning
      liquidated. It is booked like A8's liquidations (outside the policy).
      `reconcile_perps` at start: positions the ledger doesn't know are
      `perp_mismatch` events and block perp entries in that market.
    - **Not here (A12):** the A3 resolver keeps a perp intent blocking (the
      owner checks the position); cancelling a leftover stop from the bot.
    - **Tests:** the venue with fake RPC/reader/executor (open executed,
      rejected, timed out; close; stop placed, failed -> closed; an external
      exit booked with its payout, and 0 as a liquidation); the send path
      against the spot fakes; the fee pick. Nothing live sends.
  - **Built (2026-10-07), waiting for the owner's run.** As designed:
    `OnChainExecutor.send_instructions` (the rent close uses it too, now
    with the balance simulation); `JupiterPerpsVenue`
    (`trade/venues/jupiter_perps/venue.py`); `PerpVenue.open_perp`/
    `close_perp` take the intent's key, plus `place_stop`/`stop_left`;
    `PerpTerms.stop_pct`; `PerpAccount` places the stop after a recorded open
    and sells at once if it fails (`perp_stop_failed`), checks for a leftover
    stop after a close (`perp_stop_left`), and books a venue exit at its
    payout (`venue_exit`; 0 is a liquidation, `perp_liquidated`);
    `perp_mismatch` when a bucket opens and the venue has a position the
    ledger doesn't; `wiring` gives real mode the venue only with
    `perps_enabled` in `[real]`. The reader gained `priority_fee`,
    `token_amount` and `last_payout`. Recent fees on the pool read 0 on
    2026-10-07, so the price is the floor (a quarter of the cap). Tests:
    `test_perps_venue.py` (13), `test_send_instructions.py`, two more in
    `test_perp_buckets.py`. 907 tests; a paper `/smoke` of a fast short is
    clean.
  - **Owner checklist for the first real perp run:** archive or keep the
    ledger as you prefer (schema 3); fund the hot wallet with USDC for the
    collateral (Jupiter's keeper minimum is unknown: try 10 USDC) and keep
    ~0.05 SOL for fees and the request/position accounts' rent; in
    `policy.toml` `[real.trading]` set `perps_enabled = true`,
    `allowed_symbols` with SOL and USDC, and `[real.limits]` `max_trade_usd`
    at least the exposure (collateral x leverage); a spec with `market`
    (e.g. a 2x short, 10 USDC, `max_hold` 10 min, a 2% stop); `serve real`
    + `connect` in a terminal. Watch the `serve` log for `perp_stop_placed`
    after the open, check the position and its stop in the Jupiter UI, and
    after the close look for `perp_stop_left` (cancel that stop in the UI).
    Then the same for a long. Report the keeper's minimum and timing.
  - **Run 1, the short (owner, 2026-10-08).** `perp-test-short`
    (`fc12cec93781`): 2x SOL short, 1 USDC of collateral (2 USD size),
    random entry, `max_hold` 10 min. Opened 17:37:25 UTC at 106.673, closed
    by `max_hold` at 17:47:29 at 106.388; nothing UNCONFIRMED, FAILED or
    REJECTED. The ledger matches the chain: posted 1.000000, the position
    held 0.99845 (opening fee 0.00155), 1.00279 came back (the move
    +0.00535 less the closing fee 0.00101), gross +0.00279, net +0.00066
    after the two legs' 20,000 lamports; round trip 23.6 bps. **Keeper:**
    1 USDC of collateral was accepted; it executed the open 1-2 s after the
    send and the close 1-4 s after. The A16 changes on the send path
    (guarded send, single signature) ran clean on all three sends. Before
    it traded, two `hello`s were refused as designed (`max_trade_usd` 10
    under the 20 USD exposure; a wallet with no USDC yet). The wallet's SOL
    across the trip, from the chain: -1,777,520 lamports, of which the
    ledger has 20,000. Findings:
    - **F1 (accounting).** The position account's rent (1,747,520
      lamports, ~0.19 USD) is paid at the first open and was not returned
      at the close (the position PDA stays); the ledger books none of it.
      Like the token account's rent (A15): book it at the open, and a
      refund if the account is ever closed.
    - **F2 (accounting).** The venue stop's own send (10,000 lamports) is
      booked nowhere; only the two legs are.
    - **F3 (false alarm).** `perp_stop_left` fired at 17:47:34, but the
      keeper closed the stop request (and refunded its rent) at 17:47:35:
      nothing was left to cancel. `stop_left` must give the keeper time
      (poll for a few seconds) before it alarms.
    - **F4 (labels).** Each send's 10,000 lamports is 5,000 base + 5,000
      priority, booked as `fee_lamports` with `priority_fee_lamports` 0.
    - **F5 (strategy side).** A refused `hello` (`HelloRefusedError`:
      reconnecting can't help) is retried by the bot loop until Ctrl+C;
      it should stop the bot with the reason.
    - Each request locks ~0.0037 SOL of rent for a few seconds (the keeper
      refunds it); an open needs ~0.009 SOL free on top of the 0.02 reserve.
    The long side is still to run.
- **A11c. Perp run 1 fixes (F1-F5) — M.** Done 2026-10-08 (in `history.md`).
- **A12. Hardening — L.** Done 2026-10-09 (in `history.md`).

### A16–A17. Code cleanup (owner, 2026-10-08)
A pause in features to tidy the code: simpler usage and one clear job per
module, with no change in behaviour (the suite stays green after each step,
nothing in the ledger, the wire or the spec ids changes). The execution side
first (A16), then the strategy side (A17).

- **A16. Execution cleanup — M.** Done 2026-10-08 (in `history.md`).
- **A16b. Execution cleanup, second pass — M.** Done 2026-10-08 (in `history.md`).
- **A17. Strategy cleanup — M.** Done 2026-10-08 (in `history.md`).
- **A17b. Strategy cleanup, second pass — S.** Done 2026-10-08 (in `history.md`).

### A18–A22. The backlog, in order (owner, 2026-10-09)
Fixes first, then cleanup, then features. The service entrypoint and manual
trades (goal 2) stay in the backlog. Each item writes its full design here
before the code (`/phase`).

- **A18. Borrow rate of a short — S.** Done 2026-10-09 (in `history.md`).
- **A19. Token account rent in replays — S.** Done 2026-10-09 (in `history.md`).
- **A20. Perps cleanup from the A12 reviews — M.** Done 2026-10-09 (in `history.md`).
- **A21. One round trip per `connect` tick — S.** Today a tick is a `price`
  op (two for a pair without a stable quote) plus a `bucket` op, and a fill
  adds another `bucket`. A `tick` op answers with the token's and the quote
  token's USD prices (the hub's, `StalePriceError` as today) and the bucket
  snapshot in one reply; `HubMarketData` and `TradeClient` share it, and the
  pair division (`market_for`) stays on the strategy side. The submit reply
  carries the snapshot after the fill. `price` and `bucket` stay for the
  warm-up and tests. Wire change: restart them together. Tests: one round
  trip per tick against a real `TradeRunner`; the live runner suite.
- **A22. Crossovers in specs — M.** An indicator can look back: in `expr`,
  `NAME(window, back)` is the indicator `back` closed bars ago (0–50,
  default 0; `IndicatorBank.get` takes the shift, the lookback grows by it),
  so a cross is `ema(9) > ema(21) and ema(9, 1) <= ema(21, 1)`. Two typed
  blocks for the common case: `ma_crosses_above {fast, slow, kind}` and
  `ma_crosses_below` (true on the tick the fast average moves to the other
  side of the slow one; four places, as AGENTS.md says). The ids of
  existing specs don't change (new types; an `expr` without the second
  argument renders as before). Tests: the cross fires once per cross in a
  backtest, the shifted value matches the indicator on the shorter series,
  `docs/specs.md` rows.

### Backlog (numbered when scheduled)
- A service entrypoint (systemd, Docker or a Windows service): kept here by
  the owner on 2026-10-09.
- Manual trades (goal 2), with a wallet treasury: deferred again on
  2026-10-09.

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
| A8 Perps: model, paper, spec | done | 2026-10-07; in `history.md`; the owner archives the ledgers before running it (schema 3) |
| A9 Perps: backtest | done | 2026-10-07; in `history.md` |
| A10 Perps: Jupiter read-only | done | 2026-10-07; in `history.md` |
| A11a Perps: requests built and simulated | done | 2026-10-07; in `history.md` |
| A11b Perps: requests sent, first real run | short done, long waiting for the owner | Built 2026-10-07; run 1 (short) 2026-10-08: clean, findings F1-F5 in §4 |
| A11c Perp run 1 fixes | done | 2026-10-08; in `history.md` |
| A12 Perps: hardening | done | 2026-10-09; in `history.md` (all of it, perp resolving included: owner) |
| A16 Execution cleanup | done | 2026-10-08; in `history.md` |
| A16b Execution cleanup, second pass | done | 2026-10-08; in `history.md` |
| A17 Strategy cleanup | done | 2026-10-08; in `history.md` |
| A17b Strategy cleanup, second pass | done | 2026-10-08; in `history.md` |
| A18 Borrow rate of a short | done | 2026-10-09; in `history.md` |
| A19 Token account rent in replays | done | 2026-10-09; in `history.md` |
| A20 Perps cleanup from the A12 reviews | done | 2026-10-09; in `history.md` |
| A21 One round trip per `connect` tick | open | |
| A22 Crossovers in specs | open | |

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
  the candles. The rent of a token account is paid on the first buy and
  refunded only if the replay ends flat (A19). The network
  fee is a fixed USD amount per leg. The live vs
  backtest comparison assumes the bucket started the window flat with its
  whole budget.
- **Paper perps are a model** (A8, and the backtest, A9): the spot price stands in for the
  venue oracle (real opens refuse a Doves price older than 30 s, A12), borrow is
  Jupiter's live rate when the position opens (A10; the backtest takes
  `--borrow-bps-hour`), kept for the life of the position while the real one
  moves with utilization, and the impact fee is linear. A liquidation is only seen while a
  `connect` has its bucket open. A perp intent left EXECUTING is resolved from its
  logged send like a swap (A12). A partial close or a top-up resolved after a
  crash takes the price of the oracle at resolution, not the keeper's.
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
on one venue. Out of scope: flipping direction in a bucket, limit orders,
cross-margin, several venues, funding-rate strategies. Adding collateral to
an open position came in with A12 (owner, 2026-10-09).

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
- **2026-10-08 (owner):** a pause for a code cleanup, execution first
  (A16), then strategy (A17): behaviour, the ledger schema, the wire and the
  spec ids stay as they are. `SpotVenue` and the `trading_service` names
  stay (A16 notes why).
- **2026-10-08 (owner):** the duplicated signature-status reading that A16b
  left alone goes into A12 (its last bullet). A16, A16b and A17 stay
  uncommitted for now.
- **2026-10-09 (owner, A12):** A12 takes everything listed under it in one
  item, partial closes and adding collateral included (so §8.1 no longer
  lists changing the collateral of an open position as out of scope), and
  the A3 resolver learns perp intents.
- **2026-10-09 (owner):** the backlog becomes A18–A22, fixes first, then
  cleanup, then features. The service entrypoint stays in the backlog and
  manual trades (goal 2) stay deferred.
