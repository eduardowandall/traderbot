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

### Run the bot

```bash
uv run --env-file .env main.py run <mode> <SYMBOL> <strategy> '<key=value args>'
```

- **Mode**: `dry` fetches prices and simulates but never sends a real transaction. `real` ⚠️ trades real money.
- **Symbol**: format `OUTPUT-INPUT`. `SOL-USDC` buys SOL with USDC.
- **Strategy**: `random`, `target_value`, or `composer`; args are passed as a `'key=value ...'` string.

```bash
# Dry run, random strategy
uv run --env-file .env main.py run dry SOL-USDC random 'sell_chance=20 buy_chance=40'

# Dry run, strategy composer
uv run --env-file .env main.py run dry SOL-USDC composer 'buy_mode=all sell_mode=any'
```

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

- `main.py` — CLI entry point (`run`, `start`)
- `trader/bot/` — main loop: fetch price → run strategy → place order
- `trader/trading_strategy.py` — strategy base + implementations + `StrategyComposer`
- `trader/providers/jupiter/` — Jupiter HTTP client, Solana/Helius RPC, swap service
- `trader/models/` — data models + `SOLANA_MINTS` (known tokens, symbol ↔ mint)
- `logging_config.py` — logging (console + files in `.logs/`)

### Notes

- New tokens must be added to `SOLANA_MINTS` in `trader/models/mints.py`.
- New top-level strategies must be registered in `STRATEGIES` in `trader/__init__.py`.
