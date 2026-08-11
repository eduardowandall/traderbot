# Tidy-up plan

Housekeeping before refactoring or building new features. Ordered by risk: config first, dead-code removal next, then fixes/tests/CI. No feature work here (except Phase 7).

## Phase 0 — Baseline ✅ DONE

1. `uv sync --extra dev` — **failed**; resolved with `uv sync --system-certs` (corporate TLS cert store) and later `uv sync` once the `dev` extra was dropped in Phase 1
2. Recorded state at baseline:
   - `uv run pytest .` — **39 passed, 2 failed** (solders 0.28 drift: `TokenAccountOpts` import location, `SimulateTransactionResp` now requiring `context`; both fixed in Phase 1)
   - `uv run ruff check .` — **passed**
   - `uv run ruff format --check .` — **passed**
   - `uv run pyright .` — **failed**: `desk.py` (`aiohttp`) + `manual_swap.py` (removed `jupiter_public_api`, bad `TokenAccountOpts` import); both files deleted in Phase 2

## Phase 1 — Config & tooling ✅ DONE

- `pyproject.toml`
  - `description` = "Solana trading bot (Jupiter DEX)" (still says "Mercado Bitcoin")
  - `authors` — remove the "Your Name" placeholder or set the real author
  - `requires-python` — align with `.python-version` (currently `>=3.13` vs `3.14`)
  - `[tool.ruff] target-version` — change `py38` → `py314` (stale; makes UP rules target py38)
  - Consolidate dev deps: keep `[dependency-groups].dev` only; move `pyright`, `pytest-mock`, `pytest-cov` there and drop `[project.optional-dependencies].dev` (duplicate, divergent)
  - Remove unused runtime deps after verifying: `aiohttp` (only used by deleted `desk.py`), `python-dotenv` (env loaded via `uv run --env-file`, never imported)
- `.gitignore` — add `.logs/`; un-ignore `uv.lock` (currently gitignored; commit the lockfile)
- Delete `Makefile` + `scripts/pre-commit.sh`; raw `uv` commands already documented in README/AGENTS.md

### Completed notes

- `description` = "Solana trading bot (Jupiter DEX)"; placeholder authors removed; `requires-python` = `>=3.14`; ruff `target-version` `py38` → `py314`; dev deps consolidated into `[dependency-groups].dev` (pyright, pytest-mock, pytest-cov moved there); `[project.optional-dependencies].dev` dropped; `aiohttp` + `python-dotenv` removed (confirmed unused); `solana` bumped to `>=0.40.1`
- Cleanup surfaced drift that had to be fixed for a green baseline:
  - `TokenAccountOpts` now lives in `solana.rpc.models` (import fixed in `trader/providers/jupiter/async_rpc_client.py`)
  - `test_async_rpc_client.py`: `SimulateTransactionResp` now takes `context`; bump transfer to `1_000_000` lamports (rent-exempt) so LiteSVM simulation passes
- `.gitignore`: added `.logs/`, un-ignored `uv.lock`
- Deleted `Makefile` + `scripts/pre-commit.sh`
- Final state: `ruff check` ✅ · `ruff format --check` ✅ · `pytest` 41/41 ✅ · `pyright` clean for `trader/` (remaining `desk.py`/`manual_swap.py` errors resolved by Phase 2 deletions)

## Phase 2 — Delete dead & stale code ✅ DONE

- Delete `desk.py` (stale Telegram-control script, removed CLI flags)
- Delete `manual_swap.py` (broken import of removed `jupiter_public_api`; ported properly in Phase 7)
- Remove "Mercado Bitcoin" remnants:
  - `trader/providers/__init__.py`, `trader/models/__init__.py`, `trader/models/public_data.py` docstrings/comments
- Dead models — verify unused in prod code, then remove:
  - `AccountData`, `AccountBalanceData` (MB-era, only stale manual_swap used them)
  - `JupiterSwapResponse`, `JupiterTokenInfo`, `JupiterPriceData` (only in `jupiter_data.py` + tests + re-exports; `JupiterQuoteResponse`/`JupiterRoutePlan`/`JupiterSwapInfo`/`MintBalance`/`TickerData` stay)
  - `PositionType.SHORT` (unused)
- `main.py` `run` docstring — fix stale example (`--api`, `--wallet-key`, `dynamic_target` don't exist)

### Completed notes

- Deleted `desk.py`, `manual_swap.py`; pyright is now fully clean (0 errors)
- `account_data.py` retains only `MintBalance` (used by `async_jupiter_svc.py`); MB docstrings removed from `models/__init__.py`, `providers/__init__.py`, `public_data.py`
- Removed the 3 dead Jupiter models from `jupiter_data.py`, re-exports, and the 3 matching test classes in `test_jupiter_data.py`
- `PositionType.SHORT` removed; `LONG` untouched
- `run` docstring now shows real examples (`run dry SOL-USDC random/ composer`); removed the now-stale "Stale code" section from AGENTS.md
- Final state: `ruff check` ✅ · `ruff format --check` ✅ · `pytest` 38/38 ✅ · `pyright` 0 errors ✅

## Phase 3 — Bug fixes & quality ✅ DONE

- `main.py` `_get_strategy_obj`: `NameError` when no strategy args are given (e.g. `run dry SOL-USDC composer` with no args) — `args` never initialized; fix + add test
- Extract duplicated `logger_wrapper` (`async_rpc_client.py` + `async_jupiter_client.py`) into a shared helper
- `async_jupiter_svc.py`: remove commented-out dryrun block in `_wait_for_confirmation`; replace bare `raise Exception` in `_do_swap_with_retry`
- `async_account.py`: clean up informal comments (`cachezinho babaca`, `temporario`)
- `tests/trader/bot/test_async_websocket_bot.py`: delete ~90-line commented-out `FakeSolanaClient` dead block

### Completed notes

- `_get_strategy_obj` now initializes `args` as `{}` when `strategy_args` is `None`; added `tests/test_main.py` with 6 tests (`__parse_kwargs` key/flag, strategy obj with/without args, unknown strategy raises)
- Bonus fix: `pyproject.toml` had `target-version` + `exclude` misplaced inside `[tool.pytest.ini_options]` (caused "Unknown config option" warnings) — moved under a proper `[tool.ruff]` section
- New shared helper `trader/providers/jupiter/logging_utils.py::logger_wrapper` (superset: catches `SolanaRpcException` specially, then generic); both provider clients import it
- `_do_swap_with_retry`: `raise e` → `raise`-style flow with `last_error`; final `raise Exception(...)` → `raise RuntimeError(...) from last_error` (pyright now sees `-> str` cleanly; no dead code)
- Removed commented `FakeSolanaClient` block (93 lines) + cleaned the `gambiarra` comment
- `async_account.get_balance` slang comments replaced with a concise note
- Final state: `ruff check` ✅ · `ruff format --check` ✅ · `pytest` 44/44 ✅ · `pyright` 0 errors ✅

## Phase 4 — Tests

- Rename `test_jupiter_private_api.py` → `test_async_jupiter_svc.py` (it tests `AsyncJupiterProvider`; no "private API" module exists) ✅
- Merge `test_jupiter_adapter.py` into the provider tests (it tests `get_price_ticker_data`; no "adapter" module exists) ✅
- **REMINDER (audit): verify NO test calls an external API / constructs a real network client.** Before merging any phase, re-grep `tests/` for `AsyncJupiterProvider(`, `AsyncRPCClient(`, `AsyncJupiterClient(`, `SolanaClient(`, `AsyncClient(`, `httpx.`, `LiteSVM`, `websocket`, `wss://`, `requests.` and confirm each hit is mocked/offline. Audit findings so far:
  - `test_async_jupiter_svc.py` was the only offender — every `AsyncJupiterProvider(...)` default-constructed a real solders `AsyncClient` (→ httpx2/`truststore.SSLContext`, which fails on this Python 3.14/corporate-cert box when the file runs alone). **Fixed: all clients now injected** (`AsyncMock(spec=...)` for jupiter + rpc, `AsyncMock(spec=SolanaClient)` for the solders layer via `AsyncRPCClient(client=...)`); `LiteSVM` used only for offline blockhash/signing.
  - `test_async_jupiter_client.py`: constructs a real `httpx.AsyncClient()` but every HTTP method is patched — no network actually happens. Safe, but could inject `client=mock` if ever touched.
  - `test_async_rpc_client.py` + bot test: offline (`LiteSVM`, `AsyncMock(spec=...)`).
  - All other modules (`test_main`, strategies, account, notification, `test_jupiter_data`): no external calls.
- Fix typo `TestTarketValue*` → `TestTargetValue*`
- Add `conftest.py` for shared fixtures (`mock_jupiter_client`, `mock_rpc_client`, `mock_sleep`)
- Add missing coverage: CLI arg parsing (incl. no-args case), `NullNotificationService`, `AsyncAccount.can_buy`/`can_sell` edge cases

### Completed notes

- `test_jupiter_private_api.py` → `test_async_jupiter_svc.py`; `TestGetPriceTicker` merged in from `test_jupiter_adapter.py` (deleted) — mocks `get_price` via injected `AsyncMock` instead of class-level `patch.object`
- Removed the real-client dependency: `setenvvar` fixture gone (no test default-constructs a client, so `HELIUS_RPC_URL`/`SOLANA_PRIVATE_KEY` are moot), `httpx`-patching dropped, `test_init` now asserts client wiring (`api.rpc_client is rpc_client`) instead of `isinstance(api.rpc_client.client, SolanaClient)`
- New `fake_solana_client` fixture: `AsyncMock(spec=SolanaClient)` with canned `is_connected`/`get_account_info`/`get_token_accounts_by_owner`/`get_latest_blockhash`/`simulate_transaction`/`send_raw_transaction`; follows the repo's `AsyncMock`-attribute-reassignment pattern (pyright-clean)
- Single-file run of `test_async_jupiter_svc.py` (the scenario that used to fail with `ssl.SSLError 0xa080024`) now passes 8/8
- Final state: `ruff check` ✅ · `ruff format --check` ✅ · `pytest` 44/44 ✅ · `pyright` 0 errors ✅

## Phase 5 — CI

- Add `.github/workflows/ci.yml`: `uv` setup, `ruff check`, `ruff format --check`, `pyright`, `pytest` on push/PR

## Phase 6 — Docs

- Update `AGENTS.md`: drop Makefile/pre-commit notes; update stale-code section after deletions
- Update `README.md`: document the new `swap` command

## Phase 7 — New feature: `main.py swap` command

- Add a `swap` Typer command built on `AsyncJupiterProvider` (buy/sell/swap with quantity, slippage) to replace `manual_swap.py`'s capability
- Wire it into README (`swap` usage) and AGENTS.md (replace the stale `manual_swap` reference)
- Add tests for the new command

## Deferred (explicitly out of scope)

- Wire `botconfigs.example.yaml` config loader (kept as WIP reference, not wired)
- `main.py start` / broader strategy wiring refactor

## Notes / risks

- `test_async_websocket_bot.py` asserts exact mock call sequences — brittle; keep unless a refactor forces changes
- `logging_config.py` `BotLoggerFileHandler` + `DictConfigurator` are legacy/complex — flag for a future refactor, not this pass
