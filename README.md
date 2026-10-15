# OSRS DVS Dump Scanner

Flat Python scripts for an OSRS (Old School RuneScape) Grand Exchange Dump Vulnerability Score (DVS) scanner. No packaging, no tests, no CI, no complex setup.

## Quick Start

All scripts are in the `backend/` and `website/` directories. Quote paths because filenames contain spaces.

### Commands

```bash
# One-shot scan + rejection funnel
python "backend/dump DVS scanner.py" --debug

# Loose thresholds (quiet hours)
python "backend/dump DVS scanner.py" --relaxed --debug

# Continuous two-speed loop (30s fast polls)
python "backend/dump DVS scanner.py" --watch

# Backtester with horizon analysis
python backend/dvs_backtest.py --horizons 15,30,60 --volume-fills --out results.csv

# Dashboard; also serves /api/scan, /api/backtest
python website/dvs_webapp.py --port 8080
```

### Flags

| Flag | Description |
|------|-------------|
| `--debug` | Print the rejection funnel showing why items were filtered |
| `--relaxed` | Use looser thresholds (DVS >= 45, Est Profit >= 40k, Drop >= 2.0%, margin pillar 5.0) for slow market hours |
| `--fast` | Enable fast-polling on `/latest` against cached 5m baseline |
| `--watch` | Continuous two-speed loop (30s fast polls) |
| `--no-log` | Suppress CSV writes |
| `--webhook-url` | Set Discord webhook URL for STRONG BUY alerts |

## Data Paths

All paths derive from `Path(__file__).resolve().parent`, so scripts in `backend/` own:

- `backend/dvs_candidate_log.csv` — candidate signal log
- `backend/dvs_mapping_cache.json` — OSRS item mapping cache
- `backend/timeseries_cache/` — timeseries data cache

Stale duplicates of the above three also sit at the repo root — nothing in the current layout reads them.

## Traps (Verified in Source)

### DumpCandidate Field Archive Trap

`log_candidates()` compares the CSV header to `fields(DumpCandidate)`. On mismatch it renames `dvs_candidate_log.csv` to `dvs_candidate_log.<ts>.old.csv` and does **NOT** merge history. **Any new field, reordering, or removal of a DumpCandidate field will archive the live signal log without warning.** Do not add/reorder/remove DumpCandidate fields without understanding this behavior.

- See: `backend/dump DVS scanner.py:1151-1164`
- Logged as: `LRN-20261015-001` in `.learnings/LEARNINGS.md`

### Webapp ENGINE Race Condition

The `ENGINE = MarketScannerEngine()` is a module-global shared across `ThreadingHTTPServer` threads. Concurrent `/api/scan` calls interleave mutation of the engine's cached baseline and skip-state. A threading lock now protects ENGINE calls in the webapp.

- See: `website/dvs_webapp.py:61,1160-1180`

### Stdout Encoding Guard

Stdout reconfigure stays at module import scope in the scanner, not inside `main()`. The webapp inherits UTF-8 stdout as a side effect of importing the scanner. Without the reconfigure call, `cp1252` crashes on non-ASCII output on Windows.

- Already resolved: `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at module import scope

### Decant Economics

Buying (1)/(2)/(3)-dose potions and selling the combined (4)-dose is tax-exempt on purchase; the 2% GE tax (capped 5M) applies only on selling the (4)-dose. Do not apply `calculate_ge_tax()` to the buy side of a decant.

- Per AGENTS.md trap verification

### Legacy Signal History

`dvs_candidate_log_legacy.csv` at the repo root holds 82 pre-refactor signals stamped `scanner_version=2.5-legacy`. Their `dvs` and `est_profit` are **not comparable** with v2.7-hybrid output — keep it a separate file. Do not merge it into `backend/dvs_candidate_log.csv`.

- Read by passing `--log backend/dvs_candidate_log_legacy.csv` to the backtest; segment comparisons by vintage.

## Project Structure

```
C:/Users/Gary0/Downloads/dump finder/
├── AGENTS.md              # Project conventions, traps, and onboarding
├── REVIEW_AGGREGATED.md   # 11-reviewer aggregated analysis
├── backend/               # Core scanner/engine logic
├── website/               # Web dashboard (ThreadingHTTPServer on 8080)
├── .learnings/            # Durable cross-session knowledge
│   ├── LEARNINGS.md
│   ├── ERRORS.md
│   └── FEATURE_REQUESTS.md
├── misc/                  # v2.5 release snapshot (do not trust for current code)
└── dvs_candidate_log.csv  # Current candidate signal log
└── results.csv            # Last backtest results
└── dvs_mapping_cache.json # Item mapping cache
```

## Verification

The primary verification mechanism is running the CLI with `--debug` and reading the funnel output:

```bash
python "backend/dump DVS scanner.py" --debug
```

This prints the rejection funnel showing why items were filtered. No test files exist per design (`AGENTS.md`: "no test files exist — don't assume a suite is available").

## Key Links

- Project conventions: `AGENTS.md`
- Aggregated review: `REVIEW_AGGREGATED.md`
- Learnings log: `.learnings/LEARNINGS.md`
- Dashboard: http://127.0.0.1:8080