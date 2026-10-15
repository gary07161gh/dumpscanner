# Project Conventions & Agent Guide

Flat Python scripts for an OSRS Grand Exchange dump scanner. No packaging, no tests, no CI, no README.

## Commands

Only third-party dep is `requests` (urllib3 comes with it). There is **no `requirements.txt`**.

```bash
python "backend/dump DVS scanner.py" --debug          # one-shot scan + rejection funnel
python "backend/dump DVS scanner.py" --relaxed         # loose thresholds for quiet hours
python "backend/dump DVS scanner.py" --watch           # continuous two-speed loop (30s fast polls)
python backend/dvs_backtest.py --horizons 15,30,60 --volume-fills --out results.csv
python website/dvs_webapp.py --port 8080               # dashboard; also serves /api/scan, /api/backtest
```

- Quote the scanner path — the filename contains spaces and is **not** importable by name.
- Alerts: set `DVS_WEBHOOK_URL` (Discord) or pass `--webhook-url`; only fires on new `STRONG BUY`.
- `--no-log` suppresses CSV writes. Logging is **on by default**, including in the dashboard.
- Verification is running the CLI with `--debug` and reading the funnel. `.vscode/settings.json` points
  unittest at `*test.py`, but **no test files exist** — don't assume a suite is available.

## Data paths

All paths derive from `Path(__file__).resolve().parent`, so the scripts in `backend/` own:
`backend/dvs_candidate_log.csv`, `backend/dvs_mapping_cache.json`, `backend/timeseries_cache/`.
Stale duplicates of those three also sit at the **repo root** — nothing in the current layout reads them.

## Gotchas

- **Adding/reordering a `DumpCandidate` field silently archives the signal log.** `log_candidates()`
  compares the header to the current field list and, on mismatch, renames the existing CSV to
  `dvs_candidate_log.<unix_ts>.old.csv`. Your history is not merged or preserved. Keep the columns
  `dvs_backtest.py` requires (`timestamp_unix`, `item_id`, `name`, `p_dump`, `p_exit`, `p_base`,
  `est_fill`, `dvs`, `status`) and give any new field a default.
- **The webapp loads the scanner via `importlib` from `../backend`** (falling back to its own dir) and
  inserts `backend/` on `sys.path` for `import dvs_backtest`. Renaming or relocating the scanner breaks
  the dashboard; renaming it to something importable changes nothing else.
- **Keep the stdout reconfigure at module import scope in the scanner, not inside `main()`.**
  `dvs_backtest.py` and `dvs_webapp.py` have no guard of their own — the webapp only gets UTF-8 stdout
  as a side effect of importing the scanner. Without it, `cp1252` crashes on non-ASCII output.
- **Fast-loop item retention:** `/latest` ticks skip items whose `lowTime` hasn't changed *unless* they
  are in `prev_active_ids` (the previous scan's candidates + near-misses). Also note
  `last_evaluated_low_times` is cleared on every baseline refresh, so each slow tick re-evaluates
  everything. Don't "optimize" this by skipping on `lowTime` equality alone — active rows vanish from
  the dashboard until they fully recover.
- **`ENGINE` in the webapp is a module-global shared across `ThreadingHTTPServer` threads with no lock.**
  Concurrent `/api/scan` calls interleave mutation of the engine's cached baseline and skip-state.
  Don't add a second engine or assume per-request isolation.
- **`--relaxed` relaxes exactly four things** (DVS 45, profit 40k, drop 2.0%, margin pillar 5.0). The
  demand pillar, support-break pct, and min price stay fixed.
- **Backtest silently drops incomplete signals.** Rows whose horizon hasn't fully elapsed are excluded
  and counted in `excluded`, so recent logs look sparse by design, not broken.
- **Decant economics:** buying (1)/(2)/(3)-dose potions and selling the combined (4)-dose is
  tax-exempt on purchase; the 2% GE tax applies only on selling the (4)-dose. Don't apply
  `calculate_ge_tax()` to the buy side of a decant.

## Stale directories

`misc/` is a v2.5 release snapshot, not source. `misc/bundle_project.py` embeds **pre-2.7 copies** of
the scanner and backtest (no `MarketScannerEngine`, `get_pre_dump_baseline` without `_and_support`),
writes them into `misc/`, then zips that folder to `misc/dump_finder_v2.5.zip`. Don't edit or run it
expecting it to package current code, and don't trust a `grep` hit there over `backend/`.

## Legacy signal history

`backend/dvs_candidate_log_legacy.csv` holds 82 pre-refactor signals, migrated from the
repo-root log by `backend/migrate_legacy_log.py` and stamped `scanner_version=2.5-legacy`.
Those rows were scored by the pre-v2.7 scanner, so their `dvs` and `est_profit` are **not
comparable** with v2.7-hybrid output — keep it a separate file. Do not merge it into
`backend/dvs_candidate_log.csv`: that changes the live header and archives the log.
Read it by passing `--log backend/dvs_candidate_log_legacy.csv` to the backtest, and
segment comparisons by vintage. The migration script is read-only on its inputs,
deterministic, and refuses to write to the live log.

## Learning capture (`.learnings/`)

Durable, cross-session knowledge lives in `.learnings/` (tracked, team-wide):
`LEARNINGS.md`, `ERRORS.md`, `FEATURE_REQUESTS.md`. Read them before starting any
non-trivial task in this repo — `learning-aggregator` can be used to roll them up.

### Logging rules (apply always, not just on request)

1. **Errors:** log to `.learnings/ERRORS.md` with `ERR-YYYYMMDD-XXX` whenever a command,
   tool call, import, or runtime operation fails. Capture the actual error, context,
   and a concrete suggested fix. Do not invent a fix and present it as understood.
2. **Corrections:** when the user corrects you, or you discover your knowledge was
   outdated, log to `.learnings/LEARNINGS.md` (category `correction` or `knowledge_gap`).
3. **Best practices / patterns:** when you find a non-obvious approach worth keeping,
   log as `best_practice` with a stable `Pattern-Key`, `Recurrence-Count`, `First-Seen`,
   and `Last-Seen`.
4. **Feature requests:** log to `.learnings/FEATURE_REQUESTS.md` with `FEAT-YYYYMMDD-XXX`.
5. Treat a "trap" discovered in `backend/` or `website/` the same way — if it isn't
   already in `.learnings/`, either log it before fixing or confirm it is already promoted.

### Recurring-pattern detection

Before adding a new entry, grep `.learnings/` for the relevant keyword/`Pattern-Key`.
If a similar entry exists:

- Add `**See Also**` linking to the prior entry.
- Increment `Recurrence-Count`; update `Last-Seen`.
- Bump priority if the issue keeps recurring.

### Promotion rule (single threshold used everywhere)

A recurring pattern is promoted into project memory (`AGENTS.md`, and/or a memory file)
only when **all three** are true:

- `Recurrence-Count >= 3`
- Seen across at least 2 distinct tasks
- Occurred within a 30-day window

Promoted entries are written as short prevention rules, not long incident write-ups, and
the original entry gains `**Status**: promoted` plus `**Promoted**: AGENTS.md`.

### Periodic review

Review `.learnings/` (run `learning-aggregator` or read `LEARNINGS.md` directly):

- At the start of a new major task.
- After completing a feature.
- When working in an area with past learnings.
- Weekly during active development.

### Git-tracking decision

`.learnings/` is intentionally **tracked** (team-wide) so fixes and footguns propagate
to every agent and contributor. Do not add it to `.gitignore`.

The repo-root `dvs_candidate_log.csv` is the migration's source of truth; it is untracked
by git now (see `.gitignore`), so copy it before discarding.

## External APIs (OSRS GE)

- All `prices.runescape.wiki` calls must go through `fetch_endpoint()` in
  `backend/dump DVS scanner.py`, which returns the parsed JSON or raises
  `GEApiError`. No bare `requests.get(...)` / `.json()` in `scan()`,
  `refresh_baseline()`, or webapp code paths.
- `GEApiError` carries `status`, `endpoint`, `message`, `body`, `retry_after`,
  and `retryable`, plus `is_rate_limited()`, `is_server_error()`,
  `is_client_error()`. Catch it by type, not by `except Exception`, when you
  need to distinguish a rate limit (429) or server outage (5xx) from a real
  data/contract bug.
- Transient 429/5xx/connection failures retry with exponential backoff; the
  first failure of each (endpoint, kind) pair is appended to
  `.learnings/ERRORS.md` via `_log_transient_error` (de-duplicated per process).
  Do not remove that line; it is the feed into `learning-aggregator`.
- Transport-level retries also live in the mounted `urllib3.Retry` adapter
  (429/500/502/503/504 with Retry-After). Application-level retries sit on
  top and respect `Retry-After` for 429.
- The GE client never writes to or parses CSV/log state; keep that separation —
  changing a fetch must not affect `DumpCandidate` fields (which would archive
  the signal log via `log_candidates()`).

## Next Suggested Actions (skill convention)

When wrapping up a multi-step task, code-change, or analysis, append a
`## Suggested next actions` section per the `next-suggested-actions` skill:
2–4 short imperative lines prefixed `→ `, each ≤ 10 words, one optional
`(Recommended)` chip. Never hide caveats or errors in chip text. If the task is
complete with nothing to do, emit `→ Done — no further action needed`. For
choice-points that demand immediate selection, use the native question tool
instead of chips.
