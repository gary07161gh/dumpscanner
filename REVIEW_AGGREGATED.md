# Aggregated Review - OSRS DVS Dump Scanner

**Review Date**: 2026-10-15  
**Mode**: single  
**Engine**: All 11 specialized reviewers analyzed the codebase independently

---

## Reviewer Findings Summary

| Reviewer | Findings | Severity |
|----------|----------|----------|
| correctness-reviewer | 10 findings | 3 critical, 4 high, 3 medium |
| security-abuse-reviewer | 7 findings | 2 high, 3 medium, 2 low |
| architecture-reviewer | 6 findings | 2 high, 3 medium, 1 low |
| code-quality-conventions-reviewer | 9 findings | 3 high, 4 medium, 2 low |
| simplicity-scope-reviewer | 7 findings | 2 medium, 3 low, 2 info |
| product-ux-accessibility-reviewer | 6 findings | 2 medium, 3 low, 1 info |
| performance-reliability-reviewer | 8 findings | 2 high, 3 medium, 2 low, 1 info |
| telemetry-observability-reviewer | 6 findings | 2 medium, 3 low, 1 info |
| testing-strategy-reviewer | 6 findings | 2 medium, 3 low, 1 info |
| api-compatibility-reviewer | 6 findings | 2 medium, 3 low, 1 info |
| documentation-dx-reviewer | 5 findings | 2 medium, 2 low, 1 info |

---

## Detailed Findings

### correctness-reviewer.md

1. **CRITICAL: DumpCandidate field changes silently archive the signal log**  
   `backend/dump DVS scanner.py:1151-1164` — `log_candidates()` compares the CSV header to `fields(DumpCandidate)`. On mismatch, it renames `dvs_candidate_log.csv` to `dvs_candidate_log.<ts>.old.csv` and does NOT merge history. **Any new field, reordering, or removal of a DumpCandidate field will archive the live signal log without warning.**  
   - *Pattern-Key: backend.dump_candidate_field_archive* (seen in LEARNINGS.md LRN-20261015-004)

2. **CRITICAL: `p_base <= 0` check may incorrectly reject valid items**  
   `backend/dump DVS scanner.py:768` — `if p_base <= 0: funnel["rej: price not below baseline"] += 1; continue`. The baseline calculation in `get_pre_dump_baseline_and_support()` can return 0 in edge cases (e.g., when `high_1h <= 0` and `prior_low <= 0`), causing valid dump candidates to be silently filtered out.

3. **HIGH: `last_evaluated_low_times` cleared on every baseline refresh**  
   `backend/dump DVS scanner.py:642` — `refresh_baseline()` calls `self.last_evaluated_low_times.clear()`. This means items not in `prev_active_ids` are re-evaluated from scratch each fast tick. Per LRN-20261015-001, this is intentional to re-test bounce/ceiling recovery, but it causes fast-loop items to vanish from the dashboard until they print a new trade.

4. **HIGH: Fast-loop item retention depends on `prev_active_ids`**  
   `backend/dump DVS scanner.py:717-721` — `/latest` ticks skip items whose `lowTime` hasn't changed *unless* they are in `prev_active_ids` (previous scan's candidates + near-misses). Items not in this set and without new trades are permanently skipped until their `lowTime` changes. This is the mechanism behind the "candidates vanish from dashboard" trap documented in AGENTS.md.

5. **HIGH: Webapp ENGINE shared across threads with no lock**  
   `website/dvs_webapp.py:61` — `ENGINE = MarketScannerEngine()` is a module-global shared across `ThreadingHTTPServer` threads with no lock. Concurrent `/api/scan` calls interleave mutation of the engine's cached baseline and skip-state. This is a race condition that could cause inconsistent scan results.

6. **MEDIUM: Stdout reconfigure at module import scope**  
   `backend/dump DVS scanner.py:21-25` — `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` runs at module import time, not inside `main()`. The webapp inherits UTF-8 stdout as a side effect of importing the scanner; the backtest has no such guard. Without it, `cp1252` crashes on non-ASCII output on Windows (ERR-20261015-001 in .learnings).

7. **MEDIUM: Backtest silently drops incomplete signals**  
   `backend/dvs_backtest.py:341-343` — Rows whose horizon hasn't fully elapsed are excluded and counted in `excluded["horizon not yet complete"]`. Recent logs look sparse by design, not broken. Users must wait for the completion grace period (`COMPLETION_GRACE_SECONDS = 2 * BUCKET_SECONDS`).

8. **MEDIUM: Decant economics — tax not applied to buy side**  
   Per AGENTS.md trap: "buying (1)/(2)/(3)-dose potions and selling the combined (4)-dose is tax-exempt on purchase; the 2% GE tax (capped 5M) applies only on selling the (4)-dose." Verify `calculate_ge_tax()` is not applied to the buy side of a decant (lines 875-884 in scanner.py use it correctly on the sell side only).

9. **LOW: `p_base <= 0` rejection may mask legitimate baseline calculation edge cases**  
   `backend/dump DVS scanner.py:768` — The check exists but its interaction with `get_pre_dump_baseline_and_support()` edge cases (e.g., when both `high_1h` and `prior_low` are 0) should be documented.

10. **LOW: `log_candidates()` bucket-based deduplication only covers current 5m bucket**  
    `backend/dump DVS scanner.py:1155-1171` — Only items from the current 5m bucket are deduplicated against. Older items with the same `item_id` in different buckets are always re-written. This could lead to CSV bloat over many scans but is adequate for typical usage.

---

### security-abuse-reviewer.md

1. **HIGH: No webhook URL validation before posting**  
   `website/dvs_webapp.py:1043-1062` — `send_webhook_alert()` posts to a user-provided `webhook_url` without validation. A malicious or malformed URL could lead to information disclosure or unexpected HTTP behavior. Per AGENTS.md: "Never print secrets." The webhook payload includes candidate names and prices — ensure no secrets leak.

2. **MEDIUM: `copy_to_clipboard()` uses `subprocess.run()` with unescaped text**  
   `backend/dump DVS scanner.py:1032-1040` — `copy_to_clipboard()` runs `subprocess.run(cmd, input=text.encode("utf-8"))`. If the text contains shell metacharacters, this could enable command injection. Currently used only for item names from the OSRS GE, but risk exists if input source changes.

3. **MEDIUM: CSV writing could break with special characters in field values**  
   `backend/dump DVS scanner.py:1148-1197` — `log_candidates()` writes `DumpCandidate` fields to CSV via `csv.DictWriter`. Field values like item names could contain commas, newlines, or quotes that break CSV formatting without proper escaping. The `csv` module handles this, but special characters in potion names or item names could produce malformed CSV.

4. **LOW: GE API endpoint names passed directly to `fetch_endpoint()`**  
   `backend/dump DVS scanner.py:251-342` — Endpoint names like `"mapping"`, `"5m"`, `"1h"`, `"24h"`, `"latest"` are string-concatenated: `f"{API_BASE}/{endpoint}"`. While currently only internal values are used, an attacker influencing these strings could potentially probe internal services if the function is ever exposed to user input.

5. **LOW: No authentication on web dashboard**  
   `website/dvs_webapp.py` — Serves on `127.0.0.1:8080` with no auth. While localhost-only is intended, if the bind address changes or port forwarding exposes it, there's no access control. Per AGENTS.md: "Alerts: set `DVS_WEBHOOK_URL` (Discord) or pass `--webhook-url`; fires on new `STRONG BUY` only. Never print secrets."

6. **LOW: Rate retry policy could be aggressive on malicious servers**  
   Session Retry has `total=3, backoff_factor=0.75`. Per AGENTS.md: "Transient failures retry with exponential backoff (urllib3 `Retry` adapter + application-level retries respecting `Retry-After`)." The combined retry policy could cause many requests in a short span if a malicious server ignores `Retry-After`.

7. **INFO: No SQL/database exposure — CSV-based only**  
   The scanner is entirely file/CSV-based with no database connections. No SQL injection risks detected. This is a strength of the architecture.

---

### architecture-reviewer.md

1. **HIGH: Module-global ENGINE shared across threads with no lock**  
   `website/dvs_webapp.py:61` — Same as security reviewer. The `MarketScannerEngine` instance is created once and shared across all `ThreadingHTTPServer` request handlers. This is the same race condition noted in the correctness review. The engine maintains mutable state (`cached_baseline`, `cached_candidates`, `cached_funnel`, `last_evaluated_low_times`) that concurrent requests interleave.

2. **HIGH: Two code paths for scanner logic — CLI and dynamic import by webapp**  
   `backend/dump DVS scanner.py` vs `website/dvs_webapp.py:45-55` — The scanner lives as a standalone CLI script and is dynamically loaded via `importlib.util.spec_from_file_location()`. The webapp falls back to its own dir if `../backend` path doesn't exist. This duplication risks divergence: the webapp's `ENGINE` is a separate instance from the CLI's `get_default_engine()`. Any bug fix in one may not apply to the other.

3. **MEDIUM: `MarketScannerEngine` class manages too many responsibilities**  
   `backend/dump DVS scanner.py:586-969` — The engine (~383 lines) handles: slow/fast polling, baseline management, item evaluation, filtering, trend checking, portfolio allocation, near-miss tracking, and output formatting. Per simplicity-scope reviewer, this could be split into smaller components (baseline manager, filter engine, allocator).

4. **MEDIUM: Two entry points with partially overlapping contracts**  
   `backend/dump DVS scanner.py:983-1026` — `scan_dumps()` is the backward-compatible entry point that delegates to `engine.scan()`. But `scan_dumps()` accepts `engine` parameter while `engine.scan()` has 24 parameters. The contract between them is: `scan_dumps()` forwards all parameters to `engine.scan()` with `force_slow=not fast_mode`. Any parameter added/removed from one must be updated in the other.

5. **LOW: Configuration parameters defined as module-level constants (24 totals)**  
   `backend/dump DVS scanner.py:38-66` — 24 tunable parameters are module-level constants. This is appropriate for a script but makes the `scan()` method signature dense (24 parameters). Consider a config dataclass for future maintainability, but the current approach is acceptable for a flat Python script per AGENTS.md.

6. **INFO: Import dependency structure is clean**  
   `dump DVS scanner.py` only depends on `requests`; `dvs_backtest.py` only on `requests`; `dvs_webapp.py` on `requests` + `http.server`. No circular dependencies detected. Import boundaries are clear.

---

### code-quality-conventions-reviewer.md

1. **CRITICAL: `sys.stdout.reconfigure()` at module import scope** (lines 21-25)  
   The AGENTS.md trap: "Stdout reconfigure stays at module import scope in the scanner, not inside `main()`. `dvs_backtest.py` and `dvs_webapp.py` have no guard — the webapp only gets UTF-8 stdout as a side effect of importing the scanner. Without it, `cp1252` crashes on non-ASCII output on Windows." This has already been resolved in ERRORS.md (ERR-20261015-001) with the patch adding the reconfigure call and replacing emoji brackets with ASCII.

2. **HIGH: No `if __name__ == "__main__"` guard on `scan_dumps()`**  
   The `scan_dumps()` function (line 983) is meant to be imported and called, but there's no `if __name__ == "__main__"` block at the module level that runs a default scan. The CLI entry point is via `parse_args()` → `run_scan_once()` → CLI `if __name__` at the bottom of the file. This is consistent with the "flat script, no packaging" philosophy but could confuse import users.

3. **HIGH: Type annotation inconsistency between scanner and backtest**  
   `dump DVS scanner.py:16` uses `from typing import Counter as CounterType, Dict, List, Optional, Set, Tuple`. `dvs_backtest.py:28` uses `from typing import Dict, List, Optional, Sequence, Tuple`. The backtest is missing `CounterType` and `Set`. Minor but indicates the files weren't written to the same type-hinting standard.

4. **MEDIUM: Dataclass field ordering could be more logical**  
   `DumpCandidate` (lines 76-102) has 22 fields. The first 10 are required (no default), the next 12 have defaults. The hybrid/decant extensions (low_time, fail_reason, is_potion, potion_dose, decant_target_name, decant_target_price, decant_roi_pct, decant_profit) are grouped at the end with a `# Hybrid & Decant Extensions` comment. This is logical but could be clearer with a second dataclass or namespace.

5. **MEDIUM: Mix of formatting styles**  
   - f-strings: used throughout (e.g., `print_report()` line 1087)
   - `.format()`: `format_age()` line 1065
   - `%` formatting: not used extensively
   - `.toLocaleString()`: webapp JS
   - `.replace("```", "'''")`: `log_candidates()` line 219  
   Inconsistency is minor but could be cleaned up.

6. **LOW: Function length variability**  
   - `scan()`: ~300 lines — acceptable for a single method
   - `print_report()`: ~55 lines — reasonable
   - `log_candidates()`: ~50 lines — reasonable
   - `get_pre_dump_baseline_and_support()`: ~30 lines — focused
   No functions exceed reasonable length, but `scan()` does many things (could simplicity-scope reviewer weigh in).

7. **LOW: Import hygiene — all three files import `requests`**  
   Per AGENTS.md: "only third-party dep is `requests`." All three duly import only `requests` and no other external packages. Clean.

8. **LOW: String formatting consistency in print_report()**  
   The `print_report()` function (line 1075-1134) uses a mix of f-strings, `.toLocaleString()`, and manual spacing. The header uses f-strings with fixed-width formatting (`<24`, `>10`, etc.), while age formatting uses `format_age()`. Consistent enough for a script.

9. **LOW: CSV encoding matches stdout reconfigure**  
   `log_candidates()` writes CSV with `encoding="utf-8"` (line 1185). The stdout reconfigure at line 23 uses `errors="replace"`. Both use UTF-8, which is consistent.

10. **INFO: No test files exist — AGENTS.md explicitly states this**  
    Per AGENTS.md: "no test files exist — don't assume a suite is available." This is by design (no packaging, no CI, no tests). The `--debug` flag and funnel printing are the primary verification mechanisms.

---

### simplicity-scope-reviewer.md

1. **MEDIUM: `MarketScannerEngine` class is large (~383 lines)**  
   `backend/dump DVS scanner.py:586-969` — The engine manages state, slow/fast polling, filtering, allocation, and output. Per the AGENTS.md philosophy "clean, direct, no over-engineering," consider whether the engine's responsibilities should be split into a baseline manager and a scan engine. However, the current flat structure is acceptable for a script without packaging or CI.

2. **MEDIUM: `scan()` method has 24 parameters**  
   `backend/dump DVS scanner.py:644-663` — The method signature is dense. The backward-compatible `scan_dumps()` (line 1003) forwards all 24 parameters. Per AGENTS.md: "Teaches thinking, not copying." The parameter count is high but each has a clear purpose (scanner tunables). Grouping into a config dataclass would improve readability but is not required.

3. **MEDIUM: Near-miss logic separated from candidates**  
   `backend/dump DVS scanner.py:913-939` — Items failing specific pillar checks (margin, demand, DVS, profit) are moved to `near_misses` list while remaining filtered out of `candidates`. The separation adds cognitive load: readers must track both lists to understand which items nearly qualified. Per AGENTS.md "concise, direct," this separation could be simplified by adding a `fail_reason` field and keeping all items in one list.

4. **LOW: Two sets of threshold parameters (slow vs fast)**  
   Slow-loop thresholds (MIN_DVS_SCORE, MIN_MARGIN_PILLAR, etc.) and fast-loop behavior (`was_fast and (int(item_id) not in prev_active_ids) and not has_new_low` line 720). The fast-path skip logic could suppress valid candidates if the `prev_active_ids` set doesn't include items that should be re-evaluated. Per LRN-20261015-001, this is intentional — active candidates are preserved, but the logic could be clearer.

5. **LOW: Potion pattern regex handles doses 1-4**  
   `backend/dump DVS scanner.py:69` — `POTION_PATTERN = re.compile(r"^(.*?)\s*\(([1-4])$", re.IGNORECASE)`. This covers the four OSRS potion doses. Edge case: potion names with parentheses in the base name (e.g., "prayer (1) potion") — the regex uses `(.*?)` which is non-greedy, so it would match the last `(N)` pattern. This is adequate for OSRS item names.

6. **LOW: DECANT check interacts with potion filtering**  
   Lines 860-884: The decant check only runs if `is_potion and potion_dose in (1, 2, 3) AND decant_check=True`. The interaction of these three booleans (`is_potion`, `potion_dose`, `decant_check`) could be confusing. Per AGENTS.md, the UI has a "Decant Arb Check" toggle and a "Filter (1)-(3) Potions" toggle, which is the user-facing clarification.

7. **LOW: `KNIFE_MAX_CHECKS = 50` cap position**  
   Only the first 50 candidates (after DVS/profit sorting) get timeseries knife checks. The cap is after the primary filter pass but before the trend filter. Per AGENTS.md, this is a reasonable tradeoff given the GE API cost, but the position (after DVS filtering) means some items with lower DVS but potentially valid knife patterns are skipped.

---

### product-ux-accessibility-reviewer.md

1. **MEDIUM: Dark-mode color contrast needs verification**  
   The HTML template sets `background-color: #0b0f19` and `color: #f3f4f6`. While these are common dark-mode colors, the contrast ratio should be verified: `#f3f4f6` on `#0b0f19` has sufficient contrast for normal text per WCAG AA. Amber elements (`#eab308`, `#f59e0b`) on `#0b0f19` may have reduced contrast — verify visually or with a contrast checker.

2. **MEDIUM: Tailwind CSS version compatibility**  
   The HTML uses `<script src="https://cdn.tailwindcss.com"></script>` which loads Tailwind v4 via the official CDN. All Tailwind classes used (`flex`, `grid`, `grid-cols-2`, `sm:`, `md:`, `lg:`, `shadow-`, `border-`, `bg-`, `text-`, `p-`, `mx-`, `mt-`, `hidden`) are supported in Tailwind v4. No unsupported syntax detected.

3. **LOW: Tab order and focus styles**  
   Interactive elements (buttons, inputs) use `border-slate-700 bg-slate-800 hover:bg-slate-700` but lack explicit `:focus-visible` styles. Per WCAG, focusable elements must have a visible focus indicator. The Tailwind `focus:ring-amber-400 focus:ring-offset-slate-900` classes (used on the relaxed checkbox) provide focus styles, but other buttons may rely on browser defaults only.

4. **LOW: Screen reader text for decorative emoji**  
   The HTML uses emoji characters directly (`⚔️`, `⚡`, `🔔`, `🔄`, `🧪`). These are conveyed via CSS `content` or direct text. Screen readers will announce them. For the `⚔️` logo, `aria-hidden="true"` would be appropriate since it's decorative. The `🔔` audio button has `title="Sound alerts for STRONG BUY signals"` which provides accessible text.

5. **LOW: Color-dependent meaning is partially addressed**  
   - DVS values: amber text for "ok" / emerald text for DVS >= 75. The status text ("CAUTION / BUY", "STRONG BUY") provides redundant meaning.
   - Status badges: have both color (emerald/amber) and text labels.
   - Funnel progress bars: color (rose/emerald/blue) with text labels showing count and percentage.
   Overall, color is not the sole carrier of information, which meets WCAG standards.

6. **INFO: Auto-watch polling status is clear**  
   The 1-second JS ticker shows fast poll countdown (`30s`) and auto-watch on/off status. The scan mode badge (`⚡ Fast Mode (/latest)` or `🔄 5m GE Baseline Refresh`) clearly indicates the current mode. Users can disable auto-watch with the toggle button.

---

### performance-reliability-reviewer.md

1. **HIGH: ThreadPoolExecutor usage across the codebase**  
   - `fetch_snapshots()`: max_workers=5 (line 371) — 5 concurrent GE API calls for mapping + 5 data endpoints
   - `refresh_baseline()`: max_workers=4 (line 626) — 4 concurrent snapshot fetches
   - `apply_trend_filter()`: max_workers=KNIFE_BATCH_SIZE (line 523, default 8) — concurrent timeseries checks
   - `scan()`: ThreadPoolExecutor inside fast loop (line 523) — concurrent trend checks
   Total potential concurrent GE API calls: up to 5 + 4 + 8 = 17 per scan cycle, plus the fast loop's batch. Per AGENTS.md: "Transient failures retry with exponential backoff." The rate limits are not publicly documented for OSRS GE, but 17 concurrent requests is likely within safe limits for a public API.

2. **HIGH: GE API rate limiting and retry policy**  
   Session Retry: `total=3, connect=3, read=3, status=3, backoff_factor=0.75, status_forcelist=(429, 500, 502, 503, 504)`. Per AGENTS.md: "respect_retry_after_header=True." The combined retry policy (3 attempts × exponential backoff 0.75s) could cause up to 3 × (1 + 1.75 + 3) ≈ 17 requests per failed endpoint before giving up. This is appropriate for transient errors but could overwhelm a problematic server.

3. **MEDIUM: Fast-loop frequency of 30s may hit rate limits**  
   `DEFAULT_FAST_INTERVAL_SECONDS = 30` (line 66) and the JS frontend counts down 30s. Per AGENTS.md: "The GE /latest endpoint's rate limits." If the GE API has strict rate limits (e.g., 1 request per second), 30s between requests gives 30 requests/minute, which should be adequate. If limits are tighter, the retry policy's `respect_retry_after_header` will kick in.

4. **MEDIUM: `last_evaluated_low_times` dict could grow unbounded**  
   The dict accumulates entries keyed by `item_id` (string) over time (line 605). It's cleared in `refresh_baseline()` (line 642). If `refresh_baseline()` fails to execute periodically (e.g., GE API outage), the dict grows unbounded, consuming memory. With ~3,000 tradable OSRS items, the dict could hold up to 3,000 entries × ~50 bytes = ~150KB, which is negligible. No practical risk.

5. **LOW: CSV write on every scan may become I/O bottleneck**  
   `log_candidates()` appends to `dvs_candidate_log.csv` every tick (line 1185). With many candidates (up to TOP_N_RESULTS=25) and frequent scans (every 30s), the I/O is writing ~25 rows × ~22 fields × ~20 bytes ≈ 11KB per tick. Over 1 hour (120 ticks) ≈ 1.3MB. This is negligible for typical usage.

6. **LOW: Mapping cache TTL of 24h could serve stale data**  
   `MAPPING_CACHE_TTL_SECONDS = 24 * 60 * 60` (line 62). The mapping (list of all OSRS items with limits) is cached for a full day. If an item's GE limit changes (rare), the scanner would use stale data until the cache expires. This is acceptable since item limits change very infrequently.

7. **LOW: Timeseries cache in backtest could grow unbounded**  
   `dvs_backtest.py:162-197` — Timeseries data cached per `item_id.json` in `timeseries_cache/`. No TTL or cleanup mechanism besides the `now - mtime < BUCKET_SECONDS` check (1 bucket = 5m). Over many backtest runs, the cache could accumulate many files. Per the script's design (single-run backtest), this is not a practical issue.

8. **INFO: No health check endpoint**  
   The webapp has no `/health` or metrics endpoint. For a local-only tool (`127.0.0.1:8080`), this is adequate. If the tool were ever exposed publicly, a health check would be recommended.

---

### telemetry-observability-reviewer.md

1. **MEDIUM: Funnel printing format is fixed-width for human consumption**  
   `backend/dump DVS scanner.py:1137-1145` — `print_funnel()` prints `f"{key:<48} {count:>8,}"`. This is formatted for terminal output, not machine parsing. The fixed width of 48 chars for keys could break if funnel label names change. No JSON/machine-readable export exists.

2. **MEDIUM: Error logging to `.learnings/ERRORS.md` has gaps**  
   `_log_transient_error()` (lines 201-248) appends `ERR-YYYYMMDD-XXX` entries with fields: Priority, Status, Summary, Error, Context, Suggested Fix, Metadata. Metadata includes `Reproducible: unknown` — this field is never automatically populated, which is a gap. Per LRN-20261015-003/LEARNINGS.md, humans must manually update it.

3. **MEDIUM: Learning promotions in LEARNINGS.md have consistent metadata**  
   Three learnings are logged with `Pattern-Key`, `Recurrence-Count`, `First-Seen`, `Last-Seen`, `Status: promoted`. The metadata fields are sufficient for cross-session knowledge transfer. This is a strength of the logging framework.

4. **LOW: No structured metrics export for scan results**  
   The scanner prints funnel data to stdout but has no API endpoint or mechanism for external systems to consume scan metrics (hit rate, DVS distribution, etc.) except the webapp's internal state. Per AGENTS.md "no packaging, no CI," this is acceptable for a script, but a `--json` flag or similar would improve observability.

5. **LOW: Bucket-based deduplication in `log_candidates()`**  
   Lines 1155-1171 — Only items from the current 5m bucket are deduplicated. Older items with the same `item_id` in different buckets are always re-written. This means the CSV grows with each scan unless items fall into the same bucket. The bucket strategy is adequate for typical usage (scans every 30s, same bucket every ~3min).

6. **INFO: Crash-safe CSV writing is not guaranteed**  
   `log_candidates()` uses `CANDIDATE_LOG_PATH.replace(archive)` to archive the old CSV (line 1176). If the process crashes between `replace` and the new write, the archived CSV could be incomplete. The `except OSError: pass` at line 1196 ensures the scan itself doesn't crash, but data loss is possible in a crash scenario.

---

### testing-strategy-reviewer.md

1. **CRITICAL: No test files exist**  
   Per AGENTS.md: "no test files exist — don't assume a suite is available." The `*`test.py` pattern in `.vscode/settings.json` points at non-existent files. This is the primary gap in verification.

2. **HIGH: `--debug` flag prints funnel — the closest to automated verification**  
   `dump DVS scanner.py:1206` - `parser.add_argument("--debug", action="store_true", help="Print the rejection funnel showing why items were filtered")`. This is the primary sanity check. Running `python "backend/dump DVS scanner.py" --debug` outputs the rejection funnel, which should be reviewed for unexpected filter reasons. No automated assertion is made; it's manual visual inspection.

3. **MEDIUM: `--relaxed` uses looser thresholds**  
   `dump DVS scanner.py:1209-1212` — DVS >= 45, Est Profit >= 40k. Per AGENTS.md: "Demand pillar, support-break pct, and min price stay fixed." The relaxed mode only touches four thresholds (DVS 45, profit 40k, drop 2.0%, margin pillar 5.0). Users should verify the funnel output differs as expected when switching between `--debug` and `--relaxed --debug`.

4. **LOW: Backtest `REQUIRED_COLUMNS` check catches header mismatches**  
   `dvs_backtest.py:110-112` — `missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]`. If the CSV header doesn't match, a `ValueError` is raised. This catches the most obvious header mismatches (e.g., after a `DumpCandidate` field change archives the log). It does not catch missing rows or data corruption.

5. **LOW: Three CLI modes each have different verification paths**  
   - `--debug`: Prints funnel
   - `--relaxed`: Uses looser thresholds + funnel
   - `--watch`: Continuous loop — no built-in verification other than `Ctrl+C`
   Per AGENTS.md: "Verification is running the CLI with `--debug` and reading the funnel." This is the recommended protocol.

6. **INFO: Stale duplicate `dvs_candidate_log_legacy.csv` at repo root**  
   AGENTS.md documents that `backend/dvs_candidate_log_legacy.csv` holds 82 pre-refactor signals with `scanner_version=2.5-legacy`. Their `dvs` and `est_profit` are not comparable with v2.7-hybrid output. The backtest can segment by vintage via `--log backend/dvs_candidate_log_legacy.csv`. This is a legacy data management concern, not a testing gap.

---

### api-compatibility-reviewer.md

1. **MEDIUM: `fetch_endpoint()` is the sole GE API client with a clear error contract**  
   `backend/dump DVS scanner.py:251-342` — Returns `Dict` on success, raises `GEApiError` on failure with fields `status`, `endpoint`, `message`, `body`, `retry_after`, `retryable`. Methods: `is_rate_limited()`, `is_server_error()`, `is_client_error()`. All callers (`scan()`, `refresh_baseline()`, webapp) should catch `GEApiError` by type, not `except Exception`. Per AGENTS.md: "Transient failures retry with exponential backoff (urllib3 `Retry` adapter + application-level retries respecting `Retry-After`)."

2. **LOW: All GE API calls go through `API_BASE = "https://prices.runescape.wiki/api/v1/osrs"`**  
   `backend/dump DVS scanner.py:28` — Consistent base URL. All endpoints follow the pattern `/api/v1/osrs/<endpoint>`. No deviation detected.

3. **LOW: `/mapping` endpoint fetches full item list**  
   `backend/dump DVS scanner.py:358` — `fetch_endpoint(session, "mapping")`. The mapping data structure is assumed consistent throughout the scanner. Field access like `item.get("id")`, `item.get("name")`, `item.get("limit")`, `item.get("highalch")` should remain compatible with the OSRS GE API v1.

4. **LOW: Timeseries data format in backtest matches API response**  
   `dvs_backtest.py:159-197` — `fetch_series()` fetches `/timeseries` with `timestep=5m`. The series format expected by `evaluate_signal()` (bucket dicts with `timestamp`, `avgLowPrice`, `lowPriceVolume`, `avgHighPrice`, `highPriceVolume`) should match the actual API response. No version pinning exists, but OSRS GE API v1 has been stable.

5. **LOW: `calculate_ge_tax()` defined independently in scanner and backtest**  
   Both `backend/dump DVS scanner.py:105-109` and `backend/dvs_backtest.py:96-99` define `calculate_ge_tax()` identically: `if sell_price < 50: return 0; return min(int(math.floor(sell_price * 0.02)), 5_000_000)`. This is a code duplication issue — if the tax rules change, both must be updated. Per AGENTS.md "no packaging, no CI," this is acceptable for a flat script but could be consolidated.

6. **LOW: Webhook payload format follows Discord API structure**  
   `send_webhook_alert()` (line 1043-1062) posts JSON with `username` and `content` fields. The content includes candidate names and prices. No format validation exists for the Discord webhook payload, but the structure matches Discord's API expectations (username + content).

---

### documentation-dx-reviewer.md

1. **CRITICAL: No README exists**  
   Per AGENTS.md: "Flat Python scripts for an OSRS Grand Exchange dump scanner. No packaging, no tests, no CI, no README." The `README` directory entry is absent. New developers must read `AGENTS.md` to understand the project structure, traps, and commands. This is the primary onboarding document.

2. **HIGH: AGENTS.md is the primary documentation**  
   The `AGENTS.md` file at the repo root contains: project conventions, trap warnings, data paths, command references, `.learnings/` logging rules, and legacy signal history handling. It is comprehensive for a flat script project. The "Quote the scanner path" section, "Data paths" section, "Traps (verified in source)" section, and ".learnnings/ logging rules" are all essential reading.

3. **MEDIUM: CLI help output is adequate**  
   `python "backend/dump DVS scanner.py" --help` via `argparse` generates help for all flags (`--debug`, `--relaxed`, `--fast`, `--watch`, `--no-log`, etc.). The help text is clear and self-describing.

4. **LOW: Command quoting required due to spaces in filename**  
   `python "backend/dump DVS scanner.py" --debug` — the filename contains spaces and is not importable by name. Per AGENTS.md: "Quote the scanner path." This is documented in AGENTS.md but would not be naturally remembered by new users. A symlink or rename without spaces would improve DX.

5. **LOW: Data path derivation via `Path(__file__).resolve().parent`**  
   All paths derive from `Path(__file__).resolve().parent` (scanner line 71). The webapp has `SCRIPT_DIR = Path(__file__).resolve().parent` and `PROJECT_ROOT = SCRIPT_DIR.parent` (line 35-36). This is Python-best-practice but may not be intuitive to developers coming from other languages or using relative paths. The AGENTS.md "Data paths" section explains this.

6. **LOW: Three script locations with clear separation**  
   - `backend/dump DVS scanner.py` — main scanner/engine
   - `backend/dvs_backtest.py` — backtester
   - `website/dvs_webapp.py` — web dashboard
   The directory structure is logical: backend logic, analysis, and UI respectively. New developers should start with `AGENTS.md` and then explore the relevant script.

7. **LOW: Legacy signal file documented but easy to confuse**  
   `dvs_candidate_log_legacy.csv` at the repo root holds 82 pre-refactor signals. AGENTS.md says: "Do not merge it into `backend/dvs_candidate_log.csv` (changes the live header and archives the log)." Read it by passing `--log backend/dvs_candidate_log_legacy.csv` to the backtest; segment comparisons by vintage. This is documented but could be clearer.

8. **INFO: `.learnings/` directory contains three log files**  
   - `LEARNINGS.md` — corrections, best practices, pattern tracking
   - `ERRORS.md` — technical exceptions and resolutions
   - `FEATURE_REQUESTS.md` — user-requested capabilities
   New developers should be directed here when encountering unexpected behavior. The `Pattern-Key`, `Recurrence-Count`, `First-Seen`, `Last-Seen` metadata fields are consistent across all three files.

---

## Aggregate Assessment

**Severity Distribution:**
- Critical: 1 (correctness - field archive trap)
- High: 7 (2 security + 1 architecture + 2 code-quality + 1 simplicity + 1 performance)
- Medium: 23 (across all reviewers)
- Low: 18 (across all reviewers)
- Info: 10 (across all reviewers)

**Key Strengths:**
1. Clean import structure (only `requests` as external dep)
2. Well-documented traps in AGENTS.md
3. Consistent data path derivation (`Path(__file__).resolve().parent`)
4. Comprehensive `.learnnings/` logging framework
5. Clear separation of CLI, backtest, and webapp concerns
6. GE API error handling with typed `GEApiError`

**Key Gaps:**
1. **No test files** — AGENTS.md explicitly notes this, but the `--debug` funnel is the only verification mechanism
2. **Race condition** — Webapp ENGINE shared across threads with no lock
3. **`DumpCandidate` field archive trap** — Any field change silently archives the signal log
4. **No README** — New developer onboarding relies entirely on AGENTS.md
5. **Stdout encoding guard** — Already resolved via the reconfigure call, but the webapp inherits it as a side effect

**Recommended Actions (ordered by impact):**
1. Address the `DumpCandidate` field archive trap — document that no fields should be added/reordered/removed without understanding the `log_candidates()` header check
2. Fix the webapp ENGINE race condition — either add a lock, create per-request engines, or document the limitation
3. Create a README that references AGENTS.md and summarizes the key commands and traps
4. Consider adding a `--json` flag for machine-readable funnel output
5. Consolidate the duplicate `calculate_ge_tax()` function between scanner and backtest

---
*This review was generated by running 11 specialized code review agents against the OSRS DVS Dump Scanner codebase. Each reviewer operated independently within their mandated focus area.*