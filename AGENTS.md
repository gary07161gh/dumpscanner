# Project Conventions & Agent Guide

Flat Python scripts for an OSRS Grand Exchange dump scanner. No packaging, no tests, no CI, no README. Only third-party dep is `requests`.

## Commands

Quote the scanner path — the filename contains spaces and is not importable by name.

```bash
python "backend/dump DVS scanner.py" --debug    # one-shot scan + rejection funnel
python "backend/dump DVS scanner.py" --relaxed  # loose thresholds (quiet hours)
python "backend/dump DVS scanner.py" --watch    # continuous two-speed loop (30s fast polls)
python backend/dvs_backtest.py --horizons 15,30,60 --volume-fills --out results.csv
python website/dvs_webapp.py --port 8080        # dashboard; also serves /api/scan, /api/backtest
```

- `--no-log` suppresses CSV writes. Logging is **on by default**, including in the dashboard.
- Alerts: set `DVS_WEBHOOK_URL` (Discord) or pass `--webhook-url`; fires on new `STRONG BUY` only. Never print secrets.
- `--relaxed` touches exactly four thresholds (DVS 45, profit 40k, drop 2.0%, margin pillar 5.0). Demand pillar, support-break pct, and min price stay fixed.
- Verification is running the CLI with `--debug` and reading the funnel. `.vscode/settings.json` points unittest at `*test.py`, but **no test files exist** — don't assume a suite is available.

## Data paths

All paths derive from `Path(__file__).resolve().parent`, so scripts in `backend/` own:
`backend/dvs_candidate_log.csv`, `backend/dvs_mapping_cache.json`, `backend/timeseries_cache/`.
Stale duplicates of those three also sit at the repo root — nothing in the current layout reads them.

## Traps (verified in source)

- **`DumpCandidate` field changes silently archive the signal log.** `log_candidates()` compares the CSV header to the full dataclass field list (`timestamp_utc`, `timestamp_unix`, plus every `DumpCandidate` field). On mismatch it renames the CSV to `dvs_candidate_log.<ts>.old.csv`; history is not merged. `dvs_backtest.py` reads `REQUIRED_COLUMNS` (`timestamp_unix`, `item_id`, `name`, `p_dump`, `p_exit`, `p_base`, `est_fill`, `dvs`, `status`). Give any new field a default; never add/reorder/remove a `DumpCandidate` field without expecting the archive.
- **Webapp loads the scanner via `importlib`** from `../backend` (falling back to its own dir) and inserts `backend/` on `sys.path` for `import dvs_backtest`. Renaming or relocating the scanner breaks the dashboard.
- **Stdout reconfigure stays at module import scope** in the scanner, not inside `main()`. `dvs_backtest.py` and `dvs_webapp.py` have no guard — the webapp only gets UTF-8 stdout as a side effect of importing the scanner. Without it, `cp1252` crashes on non-ASCII output on Windows.
- **Fast-loop item retention:** `/latest` ticks skip items whose `lowTime` hasn't changed *unless* they are in `prev_active_ids` (previous scan's candidates + near-misses). `last_evaluated_low_times` is cleared on every baseline refresh, so each slow tick re-evaluates everything. Don't optimize by skipping on `lowTime` equality alone — active rows vanish from the dashboard until they recover.
- **`ENGINE` in the webapp is a module-global** shared across `ThreadingHTTPServer` threads with no lock. Concurrent `/api/scan` calls interleave mutation of the engine's cached baseline and skip-state. Don't add a second engine or assume per-request isolation.
- **Backtest silently drops incomplete signals.** Rows whose horizon hasn't fully elapsed are excluded and counted in `excluded`, so recent logs look sparse by design, not broken.
- **Decant economics:** buying (1)/(2)/(3)-dose potions and selling the combined (4)-dose is tax-exempt on purchase; the 2% GE tax (capped 5M) applies only on selling the (4)-dose. Do not apply `calculate_ge_tax()` to the buy side of a decant.

## External APIs (OSRS GE)

All `prices.runescape.wiki` calls must go through `fetch_endpoint()` in `backend/dump DVS scanner.py`, which returns parsed JSON or raises `GEApiError`. No bare `requests.get(...)` / `.json()` in `scan()`, `refresh_baseline()`, or webapp paths. `GEApiError` carries `status`, `endpoint`, `message`, `body`, `retry_after`, `retryable`, plus `is_rate_limited()`, `is_server_error()`, `is_client_error()`. Catch it by type, not `except Exception`, when distinguishing a rate limit (429) or server outage (5xx) from a data/contract bug. Transient failures retry with exponential backoff (urllib3 `Retry` adapter + application-level retries respecting `Retry-After`). The GE client never writes CSV/log state; changing a fetch must not alter `DumpCandidate` fields (which would archive the signal log).

## Stale directories

`misc/` is a v2.5 release snapshot, not source. `misc/bundle_project.py` embeds pre-2.7 copies of the scanner and backtest (no `MarketScannerEngine`, `get_pre_dump_baseline` without `_and_support`), writes them into `misc/`, then zips to `misc/dump_finder_v2.5.zip`. Don't edit or run it expecting it to package current code, and don't trust a `grep` hit there over `backend/`.

## Legacy signal history

`backend/dvs_candidate_log_legacy.csv` holds 82 pre-refactor signals stamped `scanner_version=2.5-legacy`. Their `dvs` and `est_profit` are **not comparable** with v2.7-hybrid output — keep it a separate file. Do not merge it into `backend/dvs_candidate_log.csv` (changes the live header and archives the log). Read it by passing `--log backend/dvs_candidate_log_legacy.csv` to the backtest; segment comparisons by vintage.

## `.learnings/` logging rules

Durable, cross-session knowledge lives in `.learnings/` (`LEARNINGS.md`, `ERRORS.md`, `FEATURE_REQUESTS.md`). It is **tracked** — do not add it to `.gitignore`.

1. **Errors:** log to `ERRORS.md` with `ERR-YYYYMMDD-XXX` whenever a command, tool call, import, or runtime operation fails. Capture the actual error, context, and a concrete suggested fix.
2. **Corrections:** when corrected or you discover outdated knowledge, log to `LEARNINGS.md` (category `correction` or `knowledge_gap`).
3. **Patterns:** log non-obvious approaches as `best_practice` with a stable `Pattern-Key`, `Recurrence-Count`, `First-Seen`, `Last-Seen`.
4. **Feature requests:** log to `FEATURE_REQUESTS.md` with `FEAT-YYYYMMDD-XXX`.
5. Treat any trap found in `backend/` or `website/` the same way — log it before fixing, or confirm it's already recorded.

Before adding a new entry, grep `.learnings/` for the keyword/`Pattern-Key`; if similar, add `**See Also**`, increment `Recurrence-Count`, update `Last-Seen`.
