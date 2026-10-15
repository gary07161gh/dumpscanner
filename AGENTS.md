# Project Conventions & Agent Guide

## Architecture Overview
- **Backend Core**: [dump DVS scanner.py](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dump%20DVS%20scanner.py)
  - Stateful `MarketScannerEngine` maintains the two-speed hybrid polling engine:
    - **Slow Loop**: Refreshes baseline (`/mapping`, `/5m`, `/1h`, `/24h`) every 300s (synced ~12s after GE boundary).
    - **Fast Loop**: Evaluates `/latest` in ~150ms against the cached 5m baseline denominators.
  - Timeseries falling knife check (`KNIFE_CHECK_ENABLED`) runs only on finalists crossing the DVS and profit threshold.
- **Backtesting Engine**: [dvs_backtest.py](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dvs_backtest.py)
  - Reads [dvs_candidate_log.csv](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dvs_candidate_log.csv) and evaluates real historical hit rates, drawdowns, and target sweeps.
- **Web Dashboard**: [dvs_webapp.py](file:///c:/Users/Gary0/Downloads/dump%20finder/website/dvs_webapp.py)
  - Built-in `ThreadingHTTPServer` on port 8080.
  - Serves single-page dark-mode dashboard with real-time 1s client ticker for Dump Age, failure cell tinting for near misses, and potion decanting badges.

## Critical Technical Conventions
1. **Windows Console Encoding**:
   - Always include `if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the entry point of scripts that print table reports to prevent `cp1252` encoding crashes on emojis or unicode characters.
2. **Fast-Loop Active Candidate Retention**:
   - In `/latest` fast polling, never skip items blindly based on `lowTime == last_seen`. Always retain and re-evaluate `prev_active_ids` so active opportunities stay in the UI until they recover or collapse.
3. **CSV Schema & Backtest Compatibility**:
   - When modifying `DumpCandidate` fields, preserve the core columns required by `dvs_backtest.py` (`timestamp_unix`, `item_id`, `name`, `p_dump`, `p_exit`, `p_base`, `est_fill`, `dvs`, `status`). Additional fields must have default values.
4. **Decant Arbitrage Rules**:
   - Bob Barter at the Grand Exchange decants doses for free. Buying (1), (2), or (3) dose potions and selling as (4) dose is tax-exempt on purchase, with GE 2% tax applied only on exit of the (4)-dose potion.
