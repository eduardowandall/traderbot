# Agent-Readiness Plan

Goal: let AI agents trade on the owner's behalf through this bot without being
able to lose, strand, or leak funds beyond limits the owner sets.

Status as of 2026-09-23. This document combines a full code review, an
adversarially verified strategy/execution audit, and three independent
architecture proposals (safety-first, incremental-MVP, interface-first).

---

## 1. Verdict

**Not ready for agents to move real money.** Readiness is about **3/10**.

The execution core (Jupiter quote, then build, sign, simulate, send and confirm)
is a reasonable base. Several things are missing entirely:

- a programmatic interface
- a policy/guardrail layer
- persistent state
- idempotent execution
- separation between the component that decides and the component that holds
  the private key

Before this review, two sell-path bugs could also strand most of a position
on every exit. They are fixed now (§2).

## 2. What was fixed in this pass

Committed in `440b5e1`. Each fix has regression tests. Test count went from
65 to 103, and coverage from 76% to 86%. Phase 0 (§6) then brought it to 132
tests.

| # | Severity | Problem | Fix |
|---|---|---|---|
| 1 | Critical | `AsyncJupiterProvider.sell` converted the amount with the **input** mint's decimals. On SOL-USDC a 1 SOL sell requested 0.001 SOL. On BONK-USDC it requested 10x the position. The bot test asserted the wrong raw amount. | Uses the decimals of the token being spent (`output_mint`). Bot and provider tests corrected. |
| 2 | Critical | `StrategyComposer` sized SELLs from the USDC balance through the buy sizing, so it sold about 20% of the position. The account then marked the position closed and left about 80% unmanaged. This is the default strategy of `start` and `run composer`. | SELL now closes the full position quantity. |
| 3 | High | `check_signature_is_confirmed` returned True for **failed** transactions. Failed transactions still land with a confirmation status. | Checks `status.err` first and raises `TransactionFailedError`. Returns False while pending. |
| 4 | High | `_do_swap_with_retry` re-quoted and re-sent after the transaction had already been broadcast, for example on a confirmation timeout. A trade that landed but confirmed slowly could be sent up to 3 times. | Failures after send raise `TransactionSubmittedError`, which is never retried. Failures before send (quote, build, simulate) are still retried. |
| 5 | High | `_wait_for_confirmation` busy-looped with no sleep on RPC errors, and treated definitive failures as pending until the 30s timeout. | Sleeps on every poll and fails fast on `TransactionFailedError`. |
| 6 | High | Sell used the intended entry quantity. After slippage the wallet holds less, so every exit failed with insufficient funds and the position got stuck. | `AsyncAccount.sell` caps the amount at the wallet balance. |
| 7 | High | The websocket only reconnected on `ConnectionClosedError`. A normal close (`ConnectionClosedOK`, not a subclass) caused a tight error loop that never reconnected. Reconnects were recursive, and every message was assumed to be a price for `data[0]`. | Catches `ConnectionClosed`, uses a bounded reconnect loop, skips non-price messages and messages for other assets, and parses prices as exact `Decimal`. |
| 8 | High | The Telegram `requests.post` had no timeout inside the event loop, so it could hang the bot forever. | `timeout=10`. |
| 9 | Medium | The main loop retried errors instantly (log flood, RPC hammering). `stop()` did nothing. | Exponential backoff from 1s to 60s, reset on success. The loop honours `is_running`. |
| 10 | Medium | `start` with no arguments traded **real** money. `run`/`swap` also defaulted to real. | Defaults changed to `dry`. |
| 11 | Medium | Trailing stop peak started at the first tick while in a position, not at the entry price, so losses could exceed the stop. | Peak is `max(peak, entry, price)`. |
| 12 | Low | Composer mode validated with `assert`. Under `-O` an invalid mode silently disabled selling. | `ValueError`. |
| 13 | Low | `provider.get_candles` ignored `interval`/`candle_qty`. `logger_wrapper` dropped function metadata. CI ran `uv sync` without `--locked`. | Arguments forwarded; `functools.wraps` added; CI uses `uv sync --locked`. |

**Behaviour changes to be aware of:**

- Composer exits now sell the full position instead of about 20% of it.
- `main.py start` is now dry by default.
- A swap whose confirmation times out now raises instead of being re-sent.
  Check the signature on an explorer before retrying manually.

## 3. Known issues still open

Code-quality and refactoring items live in
[`refactoring-backlog.md`](refactoring-backlog.md).

### 3.1 Money and correctness

- **Net PnL after all costs (2026-09-25).**
  - **What counts as a cost:** after each swap, the costs actually paid are
    recorded, and PnL is reported net of them:
    - the network fee (base + priority, from `meta.fee`)
    - rent for token accounts the wallet opened (counted as a cost, marked
      refundable)
    - other SOL the route charged
  - **Where the numbers come from:**
    - Real mode reads the confirmed transaction.
    - Dry mode asks Solana what the transaction would cost
      (`getFeeForMessage`).
    - Paper mode simulates the base fee, and the rent when an account is
      first opened.
  - **Not subtracted again:** LP/AMM fees, price impact and slippage are
    already in the actual amounts. They are logged for information only.
  - **Units:** PnL is recorded in native units, in the pair's quote token plus
    costs in SOL, with a USD estimate using rates taken from the trade itself.
    Pairs that don't involve SOL (e.g. JUP-USDC) keep costs in SOL only and are
    flagged `[!] incompleto`.
  - **Where to see it:** `main.py pnl <mode>` and `ledger list`.
  - **Safety:** costs are fetched only after the ledger marks the intent
    EXECUTED, and a failure there never retries or fails the swap.

- **Prices are USD, balances are in the input token.** The Jupiter feed
  quotes USD. `Order.price` stays in USD: it is the real fill only when the
  input token is a USD stablecoin (USDC/USDT); otherwise it is the market
  price at the signal. `Order.fill_price` keeps the raw input-per-token
  price. Strategy sizing (`balance / price`) and `requested_quantity` are only
  meaningful for stablecoin inputs; on pairs like `USDC-SOL` they mix SOL and
  USD. The proper fix is to price the output in input-token units (feed the
  input token's USD price too).
- **Fills are only as good as the quote.** Since Phase 0, `Order` records the
  quote's `inAmount`/`outAmount`. The on-chain amount can still differ within
  slippage, and reconciling against the balance delta is not done yet.
- ~~Price impact never checked~~, ~~slippage escalation without a
  ceiling~~, ~~no SOL fee reserve~~ (done in Phase 0).
- **Dry mode is not paper trading.** It reads the real wallet and never
  changes balances. **Use `paper` mode** (Phase 2) to test strategies end to
  end. `dry` is still useful for checking the real transaction path
  (simulation against the real wallet).
- **`TargetValueStrategy`'s trailing stop only works inside a narrow band.**
  If the price gaps below `target - 1.1%` there is no exit, and there is no
  stop-loss below entry at all.
- **An undocumented hard-coded `Decimal("5")` order cap** sits in three
  strategies (in input-token units, not USD).
- ~~Positions live only in memory~~ (Phase 1: restored from the ledger).

### 3.2 Reliability and ops

- ~~Clients never closed / no graceful shutdown~~ (done in Phase 0).
- Telegram still uses synchronous `requests` inside the loop. It should move
  to `httpx.AsyncClient`.
- ~~Top-level `logging_config` not in the wheel~~ (moved into `trader/` in
  Phase 0).

### 3.3 Security

- The private key lives in the same process as strategy code. Once agents
  exist, this is the largest risk (§5.3).
- ~~Telegram token only via CLI; `urllib3`/`httpx` logging secret URLs~~
  (done in Phase 0). CLI `--notification-args` still works but is
  discouraged.
- `repr(Keypair)` is safe, but **`str(Keypair)` is the base58 secret**. Never
  format a keypair with `str()` or `{keypair!s}`.
- `assert` is used for env validation (`get_keypair_from_env`,
  `AsyncRPCClient`).

### 3.4 External APIs (verify first)

- **`lite-api.jup.ag` is deprecated.** Jupiter is moving to `api.jup.ag` with an
  `x-api-key` header (free tier ~60 req/min), and Swap API V2 launched in
  March 2026. The shutdown date has been postponed, but it is coming. Make the
  base URL and API key configurable (`JUPITER_API_URL`, `JUPITER_API_KEY`).
- **The price websocket (`trench-stream.jup.ag`) and candles
  (`datapi.jup.ag`) are undocumented frontend endpoints.** They are reached
  with a spoofed `Origin`/`User-Agent` and can break without notice. Plan a
  fallback to the documented Price API V3.

## 4. Readiness gap analysis

| Area | Today | Gap | Priority |
|---|---|---|---|
| Programmatic interface | Typer CLI plus a blocking loop | No API or MCP surface an agent can call | P0 |
| Decision/execution split | `AsyncAccount.place_order` calls the provider directly | No proposal/intent layer between deciding and executing | P0 |
| Guardrails | Minimum balance checks only | No per-trade or daily limits, allow-list, price-impact cap, or kill switch | P0 |
| Idempotency | Fixed here for retries (§2 #4) | No idempotency key and no dedupe across processes | P0 |
| Persistence | SQLite ledger (Phase 1) | Done: orders, positions and PnL survive restarts | done |
| Key isolation | Key in the bot process | Agent-reachable code shares memory with the key | P1 |
| Human-in-the-loop | Telegram can only send | No approval flow | P1 |
| Paper trading and backtesting | Dry mode is semi-real | No simulated wallet, no replay harness | P1 |
| Observability | Free-text logs, PT/EN mix | No structured trade journal or decision rationale | P1 |
| Configuration | CLI strings (`'k=v k=v'`), unused YAML | No validated config or policy file | P1 |
| Multi-agent | One pair per process, whole wallet | No sub-accounts, budgets, or wallet lock | P2 |
| Market data | Websocket price, 100 candles | Undocumented endpoints; no quotes/balances API | P1 |
| Packaging | Runs from repo root only | `logging_config` outside the package; no service entrypoint | P2 |

## 5. Target architecture

```
  Agent(s) ── MCP ──▶ Agent Gateway ──▶ Policy Engine ──▶ decision
  (no key)            authn, schema,    pure function       │
                      rate limit        limits/budgets      │ allow / needs approval / deny
                                             │               ▼
  Existing strategies ─── proposals ────────┘        Approval (Telegram/CLI, TTL)
                                                             │
                                                             ▼
                                   Executor (only process with the key)
                                   re-quote → bound check → tx inspection
                                   → sign → send once → confirm → reconcile
                                                             │
                         Ledger (SQLite, append-only) ◀─────┘  ← source of truth
                         Kill switch — checked by gateway, policy, executor (fail-closed)
```

### 5.1 Principles

1. **Agents propose, they never execute.** Every trade becomes an *intent*:
   `symbol_in`, `symbol_out`, `amount_in_ui`, `max_price_impact_bps`,
   `rationale`, `agent_id`, `idempotency_key` and `expires_at`. The existing
   strategies go through the same path, so agents and strategies are
   interchangeable proposers.
2. **Deterministic policy decides.** It is a pure function,
   `evaluate(intent, policy, ledger_state, market_snapshot) -> Allow | RequireApproval | Deny(reasons)`,
   with no I/O and 100% branch coverage. `policy.yaml` belongs to the owner and
   cannot be changed through MCP.
3. **Execute exactly once.** Each idempotency key produces at most one
   broadcast. Before any retry, the executor checks the signature status and
   the balance delta.
4. **The ledger is the source of truth** for positions, PnL, budgets and the
   audit trail. On startup, state is reconciled against on-chain balances.
5. **Fail closed.** If the kill switch cannot be read, or the ledger or policy
   is unavailable, no trade happens.

### 5.2 Agent interface (MCP server, `trader/agent/mcp_server.py`)

| Tool | Type | Notes |
|---|---|---|
| `get_price(symbol)`, `get_candles(symbol, interval, n)` | read | Returns Decimal strings |
| `get_balances()`, `get_positions()`, `get_pnl()` | read | Read from the ledger plus the chain; UI units |
| `get_policy_summary()` | read | Limits and remaining budget, so the agent can plan within them |
| `quote(symbol_in, symbol_out, amount_in_ui)` | read | Includes out amount, price impact and route |
| `preview_trade(...)` | dry | Quote plus simulate plus policy evaluation; never signs or sends |
| `propose_trade(..., rationale, idempotency_key)` | side-effect | Returns an `intent_id` and a status |
| `get_intent(id)`, `list_intents()`, `cancel_intent(id)` | read / side-effect | |
| `halt()` | side-effect | Agents may pull the brake. Only a human can `resume` |

**Never exposed:** signing, sending, raw transactions, mint addresses (symbols
only, resolved against the allow-list), slippage above the policy cap, policy
edits, approvals, or `resume`.

### 5.3 Default guardrails (`policy.yaml`)

- **Mode:** `dry` by default. `real` requires both a CLI flag and an env var.
- **Allow-list:** symbol pairs from `SOLANA_MINTS`, with optional per-agent
  scopes.
- **Size:** at most $25 per trade, at most 20% of the balance, and a dust
  minimum.
- **Budgets:** $100 notional per day per agent, a daily realized-loss stop of
  $20, and at most 10 trades per hour.
- **Market sanity:**
  - price impact at most 100 bps, and slippage (including retry escalation) at
    most 100 bps
  - the quote must be within 2% of the independent websocket price
  - reject market data older than 30s
- **Reserve:** always keep at least 0.02 SOL for fees.
- **Approval:** auto-approve up to $10. Between $10 and $25, require Telegram
  or CLI approval (single use, 5-minute TTL, bound to the intent hash). Above
  $25, deny.
- **Circuit breakers:** halt after 3 consecutive execution failures, any
  ledger/chain reconciliation mismatch, or a hit on the daily loss stop. Only a
  human can reset.
- **Transaction inspection** (executor): allowed program IDs are Jupiter,
  Token, Token-2022, ATA, ComputeBudget and System. The only balance allowed to
  decrease is the wallet's own source account, by no more than the quoted
  `inAmount`.

These defaults are placeholders for the owner to tune (see §8). They are not
advice on position sizing.

## 6. Phased roadmap

Each phase ships on its own and has exit criteria that tests can check.

### Phase 0 — Harden the core (mostly done in this pass)

- [x] Sell decimals, composer sell size, failed-transaction detection,
      no re-send after broadcast, confirmation loop, sell capped at balance
- [x] Websocket reconnect, Telegram timeout, loop backoff, working `stop()`,
      dry-by-default CLI
- [x] Price-impact cap (`max_price_impact_pct`, default 1%, `SwapRejectedError`,
      never retried) and a slippage ceiling for retry escalation
      (`max_slippage_bps`, default 100). CLI `swap` gets `--max-price-impact`,
      and `--slippage-bps` is capped at 1000.
      **Open:** confirm in Jupiter's docs whether `priceImpactPct` is a percent
      or a fraction. It is treated as a percent for now (TODO in
      `_check_price_impact`).
- [x] SOL fee reserve (0.02 SOL) on buys and sells that spend SOL; buys are
      capped at the spendable balance. CLI `swap` quantity is parsed as
      `Decimal`.
- [x] `Order` records the quote fill (`inAmount`/`outAmount` through
      `SwapResult`) plus `requested_quantity`/`requested_price`. Realized PnL
      uses the exit quantity.
      **Open:** reconcile against the on-chain balance delta (Phase 1 ledger).
- [ ] Configurable Jupiter base URL and API key; move to `api.jup.ag`
      (**postponed** until the owner reviews the API)
- [x] Graceful shutdown: `aclose()` on the client, RPC and provider;
      `CancelledError` handled; `swap` closes its clients
- [x] `logging_config` moved to `trader/logging_config.py`; `httpx`/`urllib3`
      loggers at WARNING (they leaked the Helius API key and the Telegram
      token); Telegram credentials from `TELEGRAM_CHAT_ID`/`TELEGRAM_BOT_TOKEN`;
      the token is redacted from error logs
- **Exit criteria:** regression tests for each item. A timeout mock shows
  exactly one send. CI is green.

### Phase 1 — Ledger + Policy + Intents (no agents yet) — **done**

- [x] `trader/ledger/`: SQLite at `.data/ledger-<mode>.sqlite3`, one file per
      mode so dry never mixes with real. It has an `intents` table (with the
      order JSON and realized PnL) and an append-only `events` table
      hash-chained with sha256 (`ledger verify`).
- [x] `trader/policy/`: a pure `evaluate()` (100% branch coverage) and a
      **TOML** loader (`policy.toml`, or `TRADER_POLICY_FILE`; stdlib
      `tomllib`, so no new dependency). See `policy.example.toml`. The
      defaults are conservative: **real mode is off** until
      `real_trading_enabled = true`; $25 per trade, $100/24h, 10 trades/h,
      $20 daily loss, and a breaker after 3 failures. Sells that close a
      position skip the budget limits.
- [x] `trader/execution/gateway.py`: `TradeGateway.submit(intent, execute)`
      handles idempotency, policy, ledger and execution. The bot (through
      `AsyncAccount`) and `main.py swap` both go through it.
- [x] Execution failures:
      - Failures before send are marked FAILED and can be retried.
      - After broadcast, or on Ctrl+C mid-execution, the intent is marked
        UNCONFIRMED and **all** trading is blocked until
        `main.py ledger resolve` is run.
- [x] Kill switch: the `.data/HALT` file, set with `main.py halt` / cleared
      with `main.py resume`. It fails closed. `resume` also re-arms the
      breaker.
- [x] Positions and PnL are restored from the ledger on startup. In real
      mode the wallet is reconciled on startup; a mismatch is recorded as a
      `reconcile_mismatch` event, and the position is not changed
      automatically.
- [x] `TradeIntent`, `PolicyDecision` and `IntentRecord` live in
      `trader/models/intent.py`.
- **Exit criteria met:**
  - The same idempotency key submitted twice makes one provider call
    (`test_same_idempotency_key_executes_once`).
  - A policy violation blocks the trade.
  - A restart mid-position rebuilds state: covered by tests, and checked live
    in dry mode on `USDC-SOL`.
  - `evaluate()` has 100% branch coverage.
- **Open:**
  - The dry-mode sell still fails, because the wallet is real. Fixed by the
    Phase 2 `SimulatedWallet`.
  - A bot trade resolved manually as `executed` has no order JSON, so its
    position is not restored automatically.
  - Budgets are per wallet and mode, not per strategy or agent (Phase 6).

### Phase 2 — Real paper trading + replay — **done**

- [x] **`paper` mode** (`trader/paper/`). `PaperJupiterProvider` gets real
      Jupiter prices and quotes, and applies each quote to a `SimulatedWallet`
      (`.data/paper-wallet.json`). It needs **no private key and no RPC**.
      Everything else is reused from the real path: the price-impact cap,
      retries, `AsyncAccount`, the policy, and the gateway and ledger
      (`.data/ledger-paper.sqlite3`). Swaps are charged the 5000-lamport
      network fee. Manage the wallet with `main.py paper reset` / `balance`.
- [x] **Tick recording**: `run --record-ticks FILE.csv` writes each price
      received.
- [x] **Replay/backtest** (`trader/backtest/`): `main.py backtest SYMBOL
      STRATEGY ARGS --ticks FILE | --candles N --interval ...`.
      - It uses the same `AsyncAccount` → `PaperJupiterProvider` →
        `SimulatedWallet` path, with synthetic quotes built from the tick price
        plus `--fee-bps` (default 30).
      - It reports equity, return, realized PnL, max drawdown, trades, win
        rate and rejected signals.
- [x] **Determinism**: strategies now take an injectable `clock` and a seeded
      `rng` (`TradingStrategy.set_clock` / `seed`, which `StrategyComposer`
      propagates to its children). `RandomStrategy` takes a `seed=` argument.
      The WMA strategy uses the clock, so replays respect its `period`.
- **Exit criteria:**
  - Replay is deterministic: the same seed gives an identical result and a
    different seed a different one. Covered by tests and checked on real
    recorded ticks.
  - The paper-mode loop works end to end. A live 90s run on SOL-USDC made 5
    round trips, all in the ledger, until the $100/24h policy budget stopped
    it. **The one-week paper run is left for you to do.**
- **Found and fixed on the way:** on pairs whose input is not a USD
  stablecoin (e.g. `USDC-SOL`), the policy's USD value was `quantity × price`,
  which mixes SOL and USD. A trade of about $56 counted as about $0.48 and
  slipped past `max_trade_usd`. That value is now *unknown*, so those pairs
  are denied unless `allow_unknown_notional = true`.
- **Limitations:**
  - Backtests need a USD stablecoin as the input token (same unit issue as
    §3.1).
  - Paper fills use the quoted `outAmount`, with no extra slippage model.
  - Replay ignores the network fee.

### Phase 3 — Read-only and dry MCP server

- Add an `mcp` command to `main.py`. Tools: every read tool plus `quote` and
  `preview_trade`. Use the official `mcp` Python SDK.
- **Exit criteria:** an agent (Claude Desktop or Claude Code) can inspect the
  market, quote and preview. A test proves that no signing path is reachable
  from the MCP process.

### Phase 4 — Agents propose, in dry mode

- `propose_trade`, `cancel_intent`, `halt`, and event notifications (fills,
  denials, halts).
- Optionally, an `AgentDecisionStrategy`: an async decision provider with a
  timeout. On timeout or error it holds, and it is combined with the
  deterministic guards. `on_market_refresh` must become async first, or the
  agent must run off the loop.
- **Exit criteria:** two or more weeks of shadow proposals with zero policy
  bypasses, and budgets and rate limits exercised.

### Phase 5 — Isolated executor + real mode with a human in the loop

- The executor runs as a separate process and is the only one with
  `SOLANA_PRIVATE_KEY`. It communicates over a local socket with an auth token
  and inspects every transaction before signing.
- Use a dedicated low-balance hot wallet, never the main wallet.
- Every agent trade needs approval at first. Caps stay small.
- **Exit criteria:** the gateway/bot process environment has no key. A
  tampered-transaction fixture is refused. 50+ approved trades reconcile with
  zero mismatches.

### Phase 6 — Graduated autonomy + multi-agent

- Auto-approve below a threshold per agent, a daily report, and circuit
  breakers tested with fault injection.
- Sub-accounts or budgets per agent, with a wallet-level lock or reservation
  so two agents cannot spend the same balance.
- **Exit criteria:** fuzz tests show no over-spend with concurrent agents, and
  the ledger matches the chain over 2 weeks at small notional.

## 7. Top risks

1. **Prompt injection or a runaway agent.** Mitigated by the policy engine,
   budgets and kill switch, because the agent never has execution authority.
2. **Double execution or lost confirmations.** Mitigated by idempotency keys,
   no re-send after broadcast, and reconciliation.
3. **Key compromise through agent-reachable code.** Mitigated by the isolated
   executor and a low-balance hot wallet.
4. **Jupiter API changes** (lite-api sunset, undocumented websocket and
   candles). Mitigated by configurable endpoints and a fallback to the
   documented Price API.
5. **Strategy quality.** Agents inherit bad sizing or exits. Mitigated by
   paper trading and replay before real mode.

## 8. Open questions for the owner

- Which pairs, and what maximum notional per trade and per day, are you
  comfortable with?
- Which approval channel do you want: Telegram inline buttons, CLI prompt, or
  both?
- Should agents run inside the bot loop, or as external MCP clients (Claude
  Desktop/Code)? This plan assumes external first.
- One wallet with budgets, or a separate hot wallet per agent?
- Should `main.py start` (a near-duplicate of `run`) be removed?
