---
description: Review dump finder code for repo traps
allowed-tools: Read, Grep, Glob, Bash(git:*)
argument-hint: [file-path]
---

# Review Dump Finder Code

Review $ARGUMENTS for this OSRS dump finder repo (flat Python: `backend/dump DVS scanner.py`, `backend/dvs_backtest.py`, `website/dvs_webapp.py`).

If $ARGUMENTS is empty, review changed files: `git diff --name-only`.

Check against `AGENTS.md` traps, report file:line + severity:

1. `DumpCandidate` fields: new/reordered field archives `dvs_candidate_log.csv` via `log_candidates()`. Keep `timestamp_unix,item_id,name,p_dump,p_exit,p_base,est_fill,dvs,status`, defaults for new fields.
2. GE client: all `prices.runescape.wiki` calls via `fetch_endpoint()` in scanner. No bare `requests.get`/`.json()` in `scan()`, `refresh_baseline()`, webapp paths. Catch `GEApiError` by type, check `is_rate_limited()`, `is_server_error()`.
3. Scanner stdout reconfigure stays at module import scope, not inside `main()`.
4. Fast loop: `/latest` skips on unchanged `lowTime` ONLY for non-active items. Must retain `prev_active_ids` (candidates + near-misses). Do not flag clearing `last_evaluated_low_times` on baseline refresh as bug.
5. Webapp `ENGINE` global shared across threads, no lock. No second engine, no per-request isolation assumption.
6. `--relaxed` touches exactly 4 things (DVS 45, profit 40k, drop 2.0%, margin 5.0). Demand pillar, support-break, min price stay fixed.
7. Backtest sparse recent logs are by design (`excluded` count for incomplete horizons).
8. Decant: tax-exempt buy, 2% tax only on (4)-dose sell. No `calculate_ge_tax()` on buy side.
9. Ignore `misc/` hits (v2.5 snapshot, stale). Source of truth is `backend/`.
10. Never merge `backend/dvs_candidate_log_legacy.csv` into live log.

Also flag: standard bugs, security issues (webhook URL handling, no secrets in code), performance.

Output: bullet list per file, severity (blocker/major/minor), specific fix. Skip passing checks.
