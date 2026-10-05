# Writing a strategy spec

Every strategy is a JSON file, written by hand or by an agent from this
guide. There is no command to create, submit or register one: you write the
file, backtest it, run it in paper, and iterate. This page is the contract; the
code that enforces it is `trader/strategy/spec/models.py` (format) and
`validate.py` (limits), and `tests/test_spec_docs.py` fails if a field or
condition type is missing here.

Specs live next to the examples in [`examples/`](examples/). Start from
[`examples/spec-sol-dip.json`](examples/spec-sol-dip.json).

## A complete spec

```json
{
  "version": 1,
  "name": "sol-dip",
  "agent_id": "example",
  "rationale": "Buy SOL after a 2% dip from the 30-bar high with a low RSI; trailing stop 3%, target 4% or 4h.",
  "symbol": "SOL-USDC",
  "timeframe": "1_MINUTE",
  "entry": {
    "mode": "all",
    "conditions": [
      {"type": "dip_from_high", "window": 30, "pct": 2},
      {"type": "rsi_below", "period": 14, "value": 35}
    ]
  },
  "exit": {
    "stop": {"type": "trailing_stop", "pct": 3},
    "mode": "any",
    "conditions": [
      {"type": "take_profit", "pct": 4},
      {"type": "max_hold", "minutes": 240}
    ]
  },
  "sizing": {"type": "fixed_usd", "usd": 20},
  "budget_usd": 50,
  "max_loss_usd": 10,
  "cooldown_minutes": 30,
  "ttl_days": 30
}
```

## Top-level fields

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | Always `1`. |
| `name` | yes | Short name: lowercase letters, digits, `-` (max 64). |
| `agent_id` | yes | Who wrote it (an agent or `owner`): letters, digits, `_.-`. |
| `rationale` | no | Why it should work, in plain words (max 2000 chars). Keep it in step with the numbers. |
| `symbol` | yes | `OUTPUT-INPUT`: `SOL-USDC` buys SOL paying USDC; `JUP-SOL` buys JUP paying SOL. The input (the **quote token**) can be any token in `SOLANA_MINTS`; the output must be a different one and not a stablecoin. See "Prices and units" below. |
| `timeframe` | yes | Bar size for the indicators: `15_SECOND`, `1_MINUTE` or `1_HOUR`. |
| `entry` | yes | `{"mode": "all" \| "any", "conditions": [...]}`, 1–8 entry conditions. `all` is the default. |
| `exit` | yes | `{"stop": {...}, "mode": "any" \| "all", "conditions": [...]}`: a required stop plus 0–8 exit conditions. `any` is the default. |
| `sizing` | yes | `{"type": "fixed_usd", "usd": N}`: each buy spends `N` USD (or less, if the bucket or wallet has less). `{"type": "pct_of_bucket", "pct": P}` (0 < P ≤ 100): each buy spends P% of what the bucket may spend at that moment (its remaining budget, capped by the wallet). |
| `budget_usd` | yes | The bucket's spending cap. Realized losses reduce it; profits don't raise it. |
| `max_loss_usd` | yes | Realized loss that retires the bucket (no more buys; a leftover position is sold). At most `budget_usd`. |
| `cooldown_minutes` | no | Minutes without entering after an exit (0–10080, default 0). |
| `ttl_days` | one of | Days of validity, counted from the first tick the spec runs (1–365). Preferred: it never goes stale. |
| `expires_at` | one of | A fixed expiry with a timezone (e.g. `"2026-10-15T00:00:00Z"`). Exactly one of `ttl_days` / `expires_at`. |
| `supersedes` | no | The 12-character id of the spec this one replaces (for your own bookkeeping). |

Numbers may be JSON numbers or strings (`"0.15"` keeps exact decimals).
Percentages are in percent: `"pct": 2` means 2%.

### Prices and units

A spec sees prices **in its quote token** (the input): SOL-USDC prices SOL in
USDC (that is, in dollars), JUP-SOL prices JUP in SOL. Every price condition
(`price_below`'s `value`, moving averages, highs and lows, the entry price of a
`take_profit`) uses that unit, so a JUP-SOL spec trades the JUP/SOL ratio and
its gains are gains in SOL. The money side stays in **USD**: `budget_usd`,
`max_loss_usd`, `fixed_usd` and the policy limits, converted with the quote
token's USD price (Price API). If that price is unknown the bucket offers
nothing to spend until it comes back.

## How a spec trades

On every price tick:

1. **With a position:** the **stop** is checked first and always wins (it is
   OR'd with the exit conditions, so `exit.mode: all` can never disable it).
   Then the exit conditions, combined by `exit.mode`. A sell closes the whole
   position. Exits never wait for the warm-up.
2. **Without a position:** it may buy only when all of these hold:
   - the indicators are **warm**: `timeframe` bars equal to the spec's warm-up
     (below);
   - the spec has not expired;
   - the cooldown since the last exit is over;
   - the entry is **re-armed**: after an exit, the entry conditions must be
     false at least once before they can fire again (so a spec doesn't buy
     back right after its take-profit);
   - the entry conditions hold, combined by `entry.mode`.
3. Every signal carries a rationale built from the conditions that fired, e.g.
   `spec 1c21270544fc buy: dip2%_from_high30, rsi14<35`.

**Warm-up.** At startup the bot seeds the indicators from candles. The spec
needs as many bars as its slowest condition: the `window` for SMA/WMA and
highs/lows, 5x the window for EMA, 10x the period + 1 for RSI, capped at 900
bars. A gap in the price feed longer than 5 bars restarts the series, and the
spec warms up again before its next entry.

**Id.** The spec id is a hash of everything except `name`, `agent_id`,
`rationale` and `supersedes`: two specs that trade the same way share an id,
and rewording the rationale doesn't change it. The spec trades in bucket
`strategy:<id>`, so changing any number starts a new bucket (fresh budget and
PnL).

## Conditions

`window` is a number of `timeframe` bars (2–500). `pct` is a percent
(0 < pct ≤ 50; for `pct` with a default of 0, 0 ≤ pct ≤ 50). Moving averages
take `"ma": "sma" | "ema" | "wma"` (default `sma`).

### Market conditions (entry or exit)

| `type` | Parameters | True when |
|---|---|---|
| `rsi_below` | `period` (default 14), `value` (1–99) | RSI(period) < value |
| `rsi_above` | `period` (default 14), `value` (1–99) | RSI(period) > value |
| `price_below_ma` | `ma`, `window`, `pct` (default 0) | price < MA(window) − pct% |
| `price_above_ma` | `ma`, `window`, `pct` (default 0) | price > MA(window) + pct% |
| `fast_ma_above_slow` | `ma`, `fast`, `slow` (fast < slow) | MA(fast) > MA(slow) |
| `fast_ma_below_slow` | `ma`, `fast`, `slow` (fast < slow) | MA(fast) < MA(slow) |
| `dip_from_high` | `window`, `pct` | price ≤ highest close of `window` bars − pct% |
| `rebound_from_low` | `window`, `pct` | price ≥ lowest close of `window` bars + pct% |
| `price_below` | `value` (USD) | price < value |
| `price_above` | `value` (USD) | price > value |
| `volatility_below` | `window`, `pct` | std. dev. of the last `window` bar returns < pct% |
| `random_chance` | `pct` (1–100, integer) | a random draw hits, `pct`% of the time (for tests; `--seed` makes it repeatable) |
| `expr` | `expr` (text, max 200 chars) | the expression holds; see below |

### `expr`: a restricted expression

When no block says it, write it: `{"type": "expr", "expr": "rsi(14) < 30 and
price < sma(20) * 0.98"}`. It is parsed when the spec loads (a mistake is a
spec error with its position), never run as code.

- **Values:** numbers, `price`, `entry_price`, `peak` (the highest price since
  entry), `last_exit_price`, and the indicators `sma(n)`, `ema(n)`, `wma(n)`,
  `rsi(n)`, `high(n)`, `low(n)` (highest/lowest close), `volatility(n)` (in %),
  with `n` an integer number of bars (2–500).
- **Operators:** `+ - * /`, unary `-`, one comparison per pair of values
  (`< <= > >=`), `and`, `or`, `not`, parentheses. `and` binds tighter than
  `or`.
- **Missing values:** an indicator still warming up, `entry_price` without a
  position, or a division by zero leaves its comparison unknown; `x and
  unknown` is false when `x` is false, `x or unknown` is true when `x` is true,
  and only a true expression fires. So `not rsi(14) < 30` does **not** fire
  while the RSI is warming up.
- **Warm-up** counts the indicators inside, like the blocks (EMA 5x, RSI 10x
  + 1). Spaces and redundant parentheses don't change the spec id.

Examples: `(sma(5) - sma(20)) / sma(20) > 0.01` (5-bar average 1% above the
20-bar one); `price >= entry_price * 1.03 and rsi(14) > 70` (an exit);
`price < low(50) * 1.01 or rsi(7) < 20` (an entry).

### Entry-only conditions

| `type` | Parameters | True when |
|---|---|---|
| `below_last_exit` | `pct` (default 0) | price ≤ the last exit price − pct%. False before the first exit. |
| `no_last_exit` | — | the bucket has never exited. Pair it with `below_last_exit` under `entry.mode: any` to make the first buy explicit. |

### Exit-only conditions

| `type` | Parameters | True when |
|---|---|---|
| `take_profit` | `pct` | price ≥ entry price + pct% |
| `trailing_take_profit` | `pct`, `trail_pct` | the peak since entry reached entry + pct%, and the price fell `trail_pct`% from that peak |
| `max_hold` | `minutes` (1–43200) | the position has been open that long |

### Stops (`exit.stop`, exactly one)

| `type` | Parameters | True when |
|---|---|---|
| `stop_loss` | `pct` | price ≤ entry price − pct% |
| `trailing_stop` | `pct` | price ≤ the peak since entry − pct% |

## Limits checked before it runs

The trade-runner (`serve`) refuses a spec that breaks any of these when its
`connect` says `hello` (the backtest only checks the format):

- `symbol`: a known pair, two different tokens, output not a stablecoin, both
  in the policy's `allowed_symbols` if that list is set;
- the largest buy (`sizing.usd`, or `pct` of `budget_usd`) ≤ the mode's
  `max_trade_usd` (paper: 1000 by default, real: 25) and ≤ `budget_usd`;
- `max_loss_usd` ≤ `budget_usd`;
- the expiry is in the future and at most 30 days away.

## The loop: write, backtest, paper, iterate

1. Write the file (copy an example; keep `rationale` true to the numbers).
2. Backtest it on recent candles:
   `uv run main.py backtest my-spec.json --candles 1000`
   (or `uv run --no-sync python .claude/scripts/spec_check.py my-spec.json` for
   the headline only; `/spec` in Claude Code). It prints the return,
   drawdown, win rate and the first trades (`--json` for the full result), and
   refuses too few bars for the warm-up.
   Few trades mean little: widen the window or loosen a condition.
3. Run it in paper: `uv run main.py serve paper` in one terminal and
   `uv run main.py connect my-spec.json` in another (or `/smoke --spec
   my-spec.json` for an isolated 40s run of both, with a report).
4. Change the numbers and repeat. A different behaviour is a different id, so
   each version gets its own bucket and PnL.
5. Real mode is the owner's step: `uv run --env-file .env main.py serve real`,
   then `uv run main.py connect my-spec.json`, with
   `real_trading_enabled = true` in `policy.toml`.

Most new ideas fit in an `expr`. A building block that doesn't exist yet (a
new indicator, a crossover) is a code change: a model in `models.py`, its union
there, and a predicate in `conditions.PREDICATES` — then a row in this page
(and, for an indicator, a function in `expr.py`).
