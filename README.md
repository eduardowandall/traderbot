# Trader

A Solana trading bot that buys and sells tokens through the **Jupiter DEX**
using pluggable trading strategies. It fetches live prices from Jupiter,
decides when to trade through a configured strategy, and executes swaps
on Solana.

## Running

### Requirements

- Python 3.14
- `uv` (package manager)
- A [Helius](https://www.helius.dev/) API key — set as `HELIUS_RPC_URL` in `.env`
- A Solana wallet (e.g., [Phantom](https://phantom.app/), [Solflare](https://solflare.com/)) — set its private key as `SOLANA_PRIVATE_KEY` in `.env` (required even for dry runs)

### Setup

```bash
uv sync
cp .env.example .env
```

Fill in `.env`:

| Variable             | Description                                             |
| -------------------- | ------------------------------------------------------- |
| `HELIUS_RPC_URL`     | Helius RPC endpoint used for Solana blockchain calls    |
| `SOLANA_PRIVATE_KEY` | Wallet private key (base58). Required even for dry runs |
| `SOLANA_PUBLIC_KEY`  | Optional. Validated against the derived key             |
| `TELEGRAM_CHAT_ID`   | Optional. Chat for `--notification-service telegram`    |
| `TELEGRAM_BOT_TOKEN` | Optional. Bot token for Telegram notifications          |
| `JUPITER_API_URL`    | Optional. Jupiter quote/swap API base URL. Defaults to `https://api.jup.ag` |
| `JUPITER_API_KEY`    | Optional. Sent as `x-api-key`. Without it, requests still work but at a lower (keyless) rate limit. Get one at [developers.jup.ag/portal](https://developers.jup.ag/portal) |

### Run the bot

```bash
uv run --env-file .env main.py run <mode> <SYMBOL> <strategy> '<key=value args>'
```

- **Mode**: `dry` (default) fetches prices and simulates but never sends a real transaction. `paper` uses a simulated wallet (see below). `real` ⚠️ trades real money.
- **Symbol**: format `OUTPUT-INPUT`. `SOL-USDC` buys SOL with USDC.
- **Strategy**: `random`, `target_value`, or `composer`; args are passed as a `'key=value ...'` string.

```bash
# Dry run, random strategy
uv run --env-file .env main.py run dry SOL-USDC random 'sell_chance=20 buy_chance=40'

# Dry run, strategy composer
uv run --env-file .env main.py run dry SOL-USDC composer 'buy_mode=all sell_mode=any'
```

### Swap

Execute a one-shot swap between any two known symbols:

```bash
uv run --env-file .env main.py swap <mode> <SYMBOL_IN> <SYMBOL_OUT> <quantity> [--slippage-bps N] [--max-price-impact PCT]
```

- **Mode**: `dry` builds the swap but never sends a real transaction. `real` ⚠️ trades real money.
- **Symbols**: any pair from `SOLANA_MINTS` (SOL, USDC, USDT, BONK, JUP, ...).
- **Quantity**: amount of `SYMBOL_IN` to spend (in UI units).
- **Slippage**: tolerance in basis points (default 50; max 1000). Retries escalate by 25 bps, up to 100 bps.
- **Max price impact**: the swap is refused if the quote's price impact is above this % (default 1).
- If a transaction is sent but not confirmed in time, the command fails with `TransactionSubmittedError` and is **not** retried. Check the signature before trying again.

```bash
# Dry run: swap 1000 JUP for USDC
uv run --env-file .env main.py swap dry JUP USDC 1000

# Real swap: sell 0.5 SOL for USDC with 1% slippage
uv run --env-file .env main.py swap real SOL USDC 0.5 --slippage-bps 100
```

### Paper trading and backtests

`paper` mode trades against a **simulated wallet** using real Jupiter prices
and quotes. It needs no private key and no RPC, and both buys and sells work.

```bash
uv run main.py paper reset "USDC=20 SOL=0.5"     # simulated balances (.data/paper-wallet.json)
uv run main.py run paper SOL-USDC random 'sell_chance=5 buy_chance=5' --record-ticks .data/ticks/sol.csv
uv run main.py paper balance
uv run main.py ledger list paper
```

Backtests replay recorded ticks, or Jupiter candles, through a strategy. They
are deterministic: the same `--seed` always gives the same result.

```bash
uv run main.py backtest SOL-USDC random 'sell_chance=5 buy_chance=5' --ticks .data/ticks/sol.csv --balance 20 --seed 1
uv run main.py backtest SOL-USDC composer --candles 1000 --interval 1_MINUTE
```

The input token of the pair must be USDC or USDT, because prices are in USD.
`--fee-bps` (default 30) models swap costs.

### Net PnL and costs

Every swap records what it actually cost:
- the network fee (base + priority)
- rent for new token accounts (refundable)
- any other SOL the route charged

PnL is reported net of those costs, in the pair's **quote token** (USDC for
SOL-USDC, SOL for USDC-SOL), with an estimated USD value next to it.

```bash
uv run main.py pnl paper                 # net PnL per account: native + ~USD, costs paid in SOL
uv run main.py ledger list paper         # per trade: costs and net PnL
```

**Where the costs come from:**
- Real mode reads them from the confirmed transaction.
- Dry mode estimates the fee Solana would charge.
- Paper mode simulates the base fee and the account rent.

**Not subtracted again:** LP fees, price impact and slippage are already in
the amounts actually received.

**Pairs without SOL** (e.g. JUP-USDC) keep costs in SOL only and are marked
incomplete.

### Risk policy, ledger and kill switch

Every order goes through a risk policy and is recorded in a local ledger. This
covers orders from strategies and from `swap`.

- **Policy**: copy `policy.example.toml` to `policy.toml` and adjust it. Without
  a file, conservative defaults apply:
  - **real mode is disabled** until you set `real_trading_enabled = true`
  - $25 per trade and $100 per 24h
  - 10 trades per hour
  - a $20 daily loss stops new buys
  - 3 failures in a row halt trading
- **Per-mode overrides**: `[paper.limits]`, `[dry.limits]` and `[real.trading]`
  (etc.) override only the keys they set, only in that mode. For example, you
  can give paper loose limits while real keeps the strict ones. Typos in any
  section are rejected.
- **Ledger**: `.data/ledger-<mode>.sqlite3`. On restart, open positions and
  PnL are restored from it.
- **Locations**: `.data/` and `policy.toml` are always read from the project
  root, whatever directory you run commands from, so `halt` always reaches the
  running bot. Override them with `TRADER_DATA_DIR` and `TRADER_POLICY_FILE`
  (relative values are taken from the project root).

```bash
uv run main.py ledger list dry          # recent intents (executed / denied / failed)
uv run main.py ledger verify dry        # check the tamper-evident event chain
uv run main.py halt "reason"            # kill switch: no trade runs
uv run main.py resume dry               # clear the kill switch and re-arm the breaker
uv run main.py ledger resolve real <intent_id> failed --note "not on explorer"
```

If a transaction is sent but never confirmed, **all trading stops** until you
check it on an explorer and resolve it as `executed` or `failed`.

## Contributing

### Development

```bash
uv run pytest .              # run tests
uv run ruff check .          # lint
uv run ruff check --fix .    # lint and auto-fix
uv run ruff format .         # format
uv run pyright .             # type check
```

CI runs automatically via GitHub Actions (`.github/workflows/ci.yml`) on every push/PR: lint, format check, type check, and the full test suite.

### Architecture

- `main.py` — CLI entry point (`run`, `start`, `swap`)
- `trader/bot/` — main loop: fetch price → run strategy → place order
- `trader/trading_strategy.py` — strategy base + implementations + `StrategyComposer`
- `trader/providers/jupiter/` — Jupiter HTTP client, Solana/Helius RPC, swap service
- `trader/models/` — data models + `SOLANA_MINTS` (known tokens, symbol ↔ mint)
- `trader/policy/` — risk policy (`evaluate()` is pure; loaded from `policy.toml`)
- `trader/ledger/` — SQLite ledger of intents/orders/events (hash-chained)
- `trader/execution/` — `TradeGateway` (only path to a swap) and kill switch
- `trader/paper/` — simulated wallet + paper provider (real quotes, simulated fills)
- `trader/backtest/` — tick recording and deterministic replay
- `trader/logging_config.py` — logging (console + files in `.logs/`)
- `docs/plan.md` — goal, progress and roadmap; `docs/architecture.md` — a guided tour of the code

### Notes

- New tokens must be added to `SOLANA_MINTS` in `trader/models/mints.py`.
- New top-level strategies must be registered in `STRATEGIES` in `trader/__init__.py`.
