# Tweaks Implementation Plan

You are working in the root directory of an OSRS Grand Exchange dump-finding project containing `dump DVS scanner.py` and `dvs_backtest.py` (plus optional `dvs_webapp.py`, `dvs_candidate_log.csv`, and `dvs_mapping_cache.json`).

Implement all of the following updates across the codebase and verify there are no syntax or runtime errors.

---

## TASK 1: Update `dump DVS scanner.py` (to v2.7)

1. **Tunable Parameters & Penny-Item Filter:**
   - Set `VERSION = "2.7"`.
   - Set default thresholds:
     - `MIN_DVS_SCORE = 55.0`
     - `MIN_MARGIN_PILLAR = 8.0`
     - `MIN_DEMAND_PILLAR = 5.0`
     - `MIN_EST_PROFIT = 80_000`
     - `MAX_SLOT_CAPITAL = 100_000_000`
     - `MIN_RAW_DROP_PCT = 2.5`
     - `MIN_SUPPORT_BREAK_PCT = 0.5`
     - `MIN_1H_BUY_VOLUME = 4`
     - `MIN_ITEM_PRICE = 50` (new parameter to block penny items like elemental runes/feathers locked in 1-gp integer spreads)
     - `RETRACEMENT_TARGET = 0.75`
     - `MAX_RECOVERY_FRACTION = 0.65`
     - `LIVE_DUMP_MAX_AGE_SECONDS = 180`
     - `NEAR_MISS_COUNT = 5`
     - `KNIFE_BATCH_SIZE = 8`

2. **Uncontaminated 1-Hour Support & Baseline Reconstruction:**
   - Replace `get_pre_dump_baseline()` with `get_pre_dump_baseline_and_support(high_1h, low_1h, buy_1h, sell_1h, p_dump, sell_5m) -> Tuple[int, int]` that returns `(p_base, prior_low)`.
   - In `scan_dumps()`, check the 1h support break against `prior_low` instead of `low_1h` (since `low_1h` is contaminated by the 5m dump volume):
     `support_drop_pct = ((prior_low - p_dump) / prior_low) * 100.0` (reject if `< MIN_SUPPORT_BREAK_PCT` with `funnel["rej: no 1h support break"] += 1`).

3. **Live `/latest` + `/5m` Dump Detection & Penny-Item Guard:**
   - Scan `all_item_ids = set(data_5m.keys()) | set(data_1h.keys())`.
   - Allow an item if it has 5m instasell activity (`avg_low_5m > 0 and sell_5m > 0`) OR a fresh live dump on `/latest` (`latest_low_very_fresh` within `LIVE_DUMP_MAX_AGE_SECONDS = 180`).
   - Compute `provisional_dump = min(avg_low_5m, latest_low)` if both exist, else whichever is present.
   - Compute `effective_sell_5m = sell_5m if has_5m_sell else max(1, int(0.15 * buy_limit))`.
   - Immediately after setting `p_dump = provisional_dump`, filter out penny items and 1-gp spreads:

     ```python
     if p_dump < MIN_ITEM_PRICE or (p_base - p_dump) < 2:
         funnel["rej: penny item / 1-gp spread"] += 1
         continue
     ```

   - Only reject for `rej: dump already recovered on /latest` when `has_5m_sell and latest_low and latest_low > avg_low_5m` and `latest_low > (avg_low_5m + MAX_RECOVERY_FRACTION * (p_base - avg_low_5m))`.

4. **Strict `est_fill` Cap:**
   - Ensure `est_fill` is strictly capped by the 5-minute dump volume, buy limit, and 30m buyer absorption:

     ```python
     half_hour_buyer_cap = max(1, int(math.floor(0.5 * buy_1h)))
     est_fill = min(buy_limit, effective_sell_5m, half_hour_buyer_cap)
     ```

   - Then cap `est_fill` by slot/bankroll capital (`slot_cap_gp // p_dump`).

5. **Consecutive Falling-Knife Check & Short-Circuit Batching:**
   - In `check_trend(session, item_id)`, sort the `/timeseries` `data` list by `timestamp`. Take the last `KNIFE_LOOKBACK_BUCKETS` points and verify that adjacent points are strictly `WATCH_BUCKET_SECONDS` (300s) apart (`recent_points[i]["timestamp"] - recent_points[i - 1]["timestamp"] == WATCH_BUCKET_SECONDS`) and all have a valid `avgLowPrice`. If not, return `("unavailable", 0.0)`.
   - In `apply_trend_filter()`, process `to_check` in batches of `KNIFE_BATCH_SIZE = 8` using `ThreadPoolExecutor` and short-circuit the loop as soon as `len(survivors) >= target_count`.

6. **Near-Miss Watchlist, Portfolio Allocator, CLI Flags & Deduplicated Logging:**
   - Collect `near_misses` (items that pass baseline/drop/tax filters with `est_profit >= 25_000` and `dvs >= 40.0` but fail `MIN_MARGIN_PILLAR`, `MIN_DEMAND_PILLAR`, `MIN_DVS_SCORE`, or `MIN_EST_PROFIT`). Print the top `NEAR_MISS_COUNT = 5` in `print_report()` whenever `candidates` is empty.
   - Add `allocate_portfolio(candidates, max_slots, bankroll, funnel)` to size fills across `--slots` and `--bankroll`.
   - Add CLI flags: `--relaxed` (sets `MIN_DVS_SCORE=45.0`, `MIN_EST_PROFIT=40_000`, `MIN_RAW_DROP_PCT=2.0`, `MIN_MARGIN_PILLAR=5.0`), `--slots`, `--bankroll`, and `--webhook-url` (defaults to `os.environ.get("DVS_WEBHOOK_URL")`).
   - In `log_candidates()`, deduplicate against existing rows in `dvs_candidate_log.csv` using `(timestamp_unix // WATCH_BUCKET_SECONDS, item_id)` so multiple scans in the same 5-minute tick do not write duplicate rows.
   - Use a cross-platform `copy_to_clipboard(text)` helper (`clip`, `pbcopy`, `wl-copy`, `xclip`) via `shutil.which`.

---

## TASK 2: Fix and Upgrade `dvs_backtest.py`

1. **Fix Syntax Errors:**
   - Close the top module docstring with `"""` before `import argparse`.
   - Add the missing closing `)` to `REQUIRED_COLUMNS` and `SUMMARY_HEADER`.

2. **5-Minute Bucket Alignment & Deduplication:**
   - In `load_signals()`, deduplicate rows using `bucket_key = (sig.timestamp // BUCKET_SECONDS, sig.item_id)`.
   - In `evaluate_signal()` and `score_signals()`, floor-align the signal timestamp to the 5-minute bucket boundary:
     `start = (sig.timestamp // BUCKET_SECONDS) * BUCKET_SECONDS`
     so the active 5-minute bucket in which the signal fired is included in the evaluation window.

3. **Fast Window Slicing & Cache Freshness:**
   - Pre-compute `timestamps_by_item` and use `bisect.bisect_left` in `evaluate_signal()` instead of linear list comprehensions.
   - In `fetch_series()`, check `mtime = cache_path.stat().st_mtime` and return the cached JSON if `(min_timestamp and newest >= min_timestamp) or (now - mtime < BUCKET_SECONDS)`.

4. **Consistent Sweep Ceiling Caps & Volume-Constrained Fills:**
   - In `target_price(sig, retrace, logged_target_retrace)`, preserve any scan-time ceiling cap on `sig.p_exit`: if `sig.p_exit < expected_uncapped`, cap the swept target at `min(raw, sig.p_exit)`.
   - Add a `--volume-fills` CLI flag (`volume_fills: bool`) that caps `filled_qty` (added to `Outcome`) by the sum of `lowPriceVolume` in buckets where `avgLowPrice <= sig.p_dump`, and scales `realized_profit` by `filled_qty`.

---

## TASK 3: Create/Update `dvs_webapp.py`

Create or update `dvs_webapp.py` using Python's built-in `http.server.ThreadingHTTPServer` (serving on `http://127.0.0.1:8080`) that dynamically imports `dump DVS scanner.py` and `dvs_backtest.py`:

1. **Endpoints:**
   - `GET /`: Serves a dark-mode Tailwind CSS single-page dashboard.
   - `GET /api/scan`: Accepts query parameters (`min_dvs`, `min_profit`, `max_capital`, `min_price`, `retrace`, `slots`, `bankroll`, `relaxed`, `knife_check`, `log`), runs `scan_dumps()`, logs candidates if enabled, and returns JSON containing `candidates`, `near_misses`, `funnel`, and `next_bucket_wait_sec`.
   - `GET /api/backtest`: Runs `dvs_backtest.py` logic across requested `horizons`, `scratch_mode`, and `volume_fills` and returns JSON results.
2. **UI Features:**
   - A **Relaxed Mode** checkbox/toggle that switches the UI inputs between Strict (`DVS 55`, `Profit 80k`) and Relaxed (`DVS 45`, `Profit 40k`) presets, plus a **Min Price (gp)** input defaulted to `50`.
   - LiveOpportunities table with sortable columns, OSRS Wiki price links, one-click "Copy" button for GE search, and an automatic **Near-Miss Watchlist** section when strict opportunities are empty.
   - Auto-Watch mode synced to `next_bucket_wait_sec` with a live countdown timer and Web Audio API chime when new `STRONG BUY` signals appear.
   - Tabs for **Live Opportunities**, **Rejection Funnel**, and **Backtest Logged Signals**.

---

## TASK 4: Verification

1. Run `python "dump DVS scanner.py" --relaxed --debug` and verify that:
   - Penny items (< 50 gp, such as `Water rune`) are filtered out by `rej: penny item / 1-gp spread`.
   - `Est Fill` is properly capped by 5-minute dump volume (`effective_sell_5m`).
2. Run `python dvs_backtest.py --volume-fills` and confirm it executes cleanly without syntax errors.
