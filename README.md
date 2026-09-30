# Trader

A Solana trading bot that swaps tokens through the **Jupiter DEX**. Every
strategy is a small JSON **spec** (entry conditions, a stop, exits, a budget);
the bot runs one spec against live prices, and every order passes a risk
policy and is recorded in a local ledger.

How the code works, following one trade: [`docs/architecture.md`](docs/architecture.md).

## Quick start (paper: no key, no money)

```bash
uv sync
uv run main.py run paper docs/examples/spec-random.json --seed 1
```

`paper` trades a **simulated wallet** (`.data/paper-wallet.json`, created with
100 USDC and 0.5 SOL) at real Jupiter prices and quotes. Stop it with Ctrl+C.

## The two commands

```bash
uv run main.py run <paper|real> <spec.json> [--seed N] [--record-ticks FILE]
uv run main.py backtest <spec.json> [--candles 1000 | --ticks FILE] [--seed N] [--fee-bps 30] [--slippage-bps 10]
```

- **run**: the mode is required. The spec's `symbol` (`OUTPUT-INPUT`:
  `SOL-USDC` buys SOL with USDC) is the pair. The spec trades in its own
  bucket with its `budget_usd`; losing `max_loss_usd` retires it.
  `--record-ticks` saves the prices for a later backtest.
- **backtest**: replays recent candles (or recorded ticks) with the spec's
  budget and prints one JSON object. The input token must be USDC or USDT.
  It refuses fewer bars than the spec needs to warm up.
- Examples are in `docs/examples/` (`spec-sol-dip.json` is the reference).

## Real mode

```bash
cp .env.example .env    # fill in HELIUS_RPC_URL and SOLANA_PRIVATE_KEY
cp policy.example.toml policy.toml   # set real_trading_enabled = true
uv run --env-file .env main.py run real <spec.json>
```

Use a dedicated low-balance wallet. Real mode stays denied until the policy
enables it.

## Configuration

| Variable | What |
|---|---|
| `SOLANA_PRIVATE_KEY` | Real mode: wallet private key (base58) |
| `SOLANA_PUBLIC_KEY` | Optional: checked against the key |
| `HELIUS_RPC_URL` | Real mode: Solana RPC (Helius) |
| `JUPITER_API_URL` / `JUPITER_API_KEY` | Optional: API host (default `https://api.jup.ag`) and key (keyless works, at a lower rate limit) |
| `TELEGRAM_CHAT_ID` / `TELEGRAM_BOT_TOKEN` | Optional: both set = Telegram notifications |
| `TRADER_DATA_DIR` / `TRADER_POLICY_FILE` / `TRADER_LOG_DIR` | Optional: state, policy and log locations (default `.data/`, `policy.toml`, `.logs/` under the project root) |

**Policy** (`policy.toml`, not in git; start from `policy.example.toml`).
Defaults: real mode off; $25 per trade, $100 per 24h, 10 trades per hour, a
$20 daily loss stops buys, 3 failures in a row stop trading. Paper gets roomy
limits by default. `[limits]` applies to every mode; `[paper.*]` and `[real.*]`
override only in that mode.

## What to know when running it

- **Costs and PnL.** Every swap records the network fee, account rent and any
  other SOL charged (real mode reads them from the confirmed transaction; paper
  simulates them). PnL is net of those, in the quote token with a USD
  estimate. LP fees and slippage are already in the amounts received.
- **Looking at what happened:**
  `uv run --no-sync python .claude/scripts/ledger_dump.py paper` prints the
  recent intents (executed, denied with the reason, failed), PnL per bucket
  and the paper wallet, read-only.
- **A process killed mid-swap** leaves an UNCONFIRMED intent, and that mode
  stops trading. Check the signature on an explorer, then move or delete
  `.data/ledger-<mode>.sqlite3`.
- **The circuit breaker** (3 failures in a row) re-arms when you restart the bot.
- **Starting paper over:** delete `.data/paper-wallet.json` and
  `.data/ledger-paper.sqlite3`.
- **A new token** goes in `SOLANA_MINTS` (`trader/models/mints.py`).

## Development

```bash
uv run pytest .          # tests
uv run pytest -m live    # live checks against Jupiter (paper, read-only, ~1 min)
uv run ruff check .      # lint (--fix to auto-fix)
uv run ruff format .     # format
uv run pyright .         # types
```

CI (`.github/workflows/ci.yml`) runs lint, format, types and tests on ubuntu and
windows. Contributor notes and the rules that are easy to break are in
[`AGENTS.md`](AGENTS.md); the roadmap is [`docs/plan.md`](docs/plan.md).
