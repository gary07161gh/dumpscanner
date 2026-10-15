# OSRS DVS Scanner Improvements and Features \- Google Gemini

**Date**: October 15, 2026 at 02:28 AM
**Turns**: 2
**Source**: [Gemini Chat](https://gemini.google.com/app/950827c8af1f6141)

---

## Turn 1

### 👤 User

*[This turn includes uploaded images]*

![Code folder icon](assets/img-001.png)

📎 dump finder

what improvements/features could be added

### 🤖 Assistant

Both `dump DVS scanner.py` and `dvs_backtest.py` are already well-structured—concurrent API fetches, atomic cache writes, exact GE tax floors, and strict post-bucket backtest assumptions are all solid.
Here are the highest-impact improvements, bug fixes, and features you can add across the math, backtester, and workflow.

### 1. Mathematical & Scoring Improvements (`dump DVS scanner.py`)

- **Fix the Pre-Dump Baseline Volume Weighting (`get_pre_dump_baseline`)** Currently, when reconstructing `p_base`, the script weights `high_1h` by `buy_1h` and `prior_low` by `prior_sell_vol`. If an item had 500 instasells and only 10 instabuys in the preceding 55 minutes, `p_base` gets dragged all the way down to `prior_low`. Because `avg_low_5m >= p_base` is rejected, legitimate dumps on items experiencing heavy sell pressure over the hour get filtered out.
  - *Fix:* Anchor `p_base` to the 55m midpoint `0.5 * (high_1h + prior_low)` or weight `high_1h` more heavily when `high_1h` is supported by fresh `/latest` trades, rather than letting 1h sell volume depress the reference ceiling.
- **Remove the Penalty on Oversized Dumps (Pillar B `s_score`)** Right now, `l_sat = sell_5m / buy_limit` applies an exponential decay (`math.exp(-0.45 * max(0.0, l_sat - 1.0))`) whenever a 5m dump exceeds 1x the 4-hour buy limit. In practice, a whale dumping 3x the buy limit creates the deepest, most lucrative temporary price dislocations, yet Pillar B penalizes it while `l_sat = 0.01` (a tiny trickle) gets a perfect `35.0`.
  - *Fix:* Invert or bell-curve this logic. Reward `l_sat` ramping up from `0.15` to `1.5` (proving there is enough liquidity at the dump price to actually fill your buy limit), and only penalize extreme values (`l_sat > 4.0`) where supply might overwhelm the market for hours.
- **High-Alch Floor Bonus / Protection** `dvs_mapping_cache.json` already contains `highalch` for every item, but `build_item_map()` ignores it. If `p_dump` drops near or below `highalch - nature_rune_price`, downside risk is virtually zero. Adding a DVS bonus (or bypassing the falling-knife filter) when an item is within 1–2% of its alch floor will catch free-money bulk dumps.
- **Spread-Stability Check (Anti-Illiquid Spike Filter)** If a low-volume item has a normal instasell of 1M and a single person instabuys 4 items at 1.5M over the hour, `high_1h` is artificially inflated to 1.5M. When the next person instasells at the normal 1M price, the scanner sees a "33% dump" from baseline.
  - *Fix:* Cross-check the 1h spread against `/timeseries` 1h or 24h median prices, or reject items where `(high_1h - low_1h) / low_1h` exceeds a reasonable historical volatility ratio unless volume is high.

### 2. Backtester Upgrades (`dvs_backtest.py`)

- **Realistic Stop-Loss / Time-Decay Exit Simulation** Currently, if `hit_target` is `False`, the backtester assumes you exit at `close_bucket.get("avgHighPrice")` (an patient offer fill at the top of the book) at the very last minute of the horizon. If an item actually crashed, your offer sitting at `target` won't fill at `avgHighPrice`—you'd have to panic-liquidate at `avgLowPrice` (instasell) or lower your offer.
  - *Fix:* Add a `-- liquidation-mode [high|mid|low]` flag so you can stress-test what happens when unhit trades are scratched at `avgLowPrice` or the midpoint at the end of the 60m window.
- **Time-to-Fill & Time-to-Exit Metrics** Knowing *when* a signal fills and exits is critical for capital velocity. Track `bars_to_fill` (`fill_idx * 5` mins) and `bars_to_hit` in `Outcome`. If 90% of your winning fills happen in the first 10 minutes (`fill_idx <= 1`), you know to cancel unfilled GE bids after 10 minutes instead of tying up cash.
- **Local Timeseries Caching for Instant Parameter Sweeps** Every time you run `dvs_backtest.py`, it re-fetches `/timeseries` over the network for every unique item in your CSV. Caching those 5m series in a local SQLite database or JSON folder (keyed by `item_id` and `last_timestamp`) will make subsequent backtest runs instant and allow you to run multi-variable grid searches (sweeping `MIN_DVS_SCORE`, `MIN_RAW_DROP_PCT`, and `RETRACEMENT_TARGET` simultaneously).
- **Export All Horizons on `--out`** Currently, `if args.out and not exported:` only writes the *first* horizon in `--horizons` to the CSV and ignores the rest. Removing the `not exported` gate (or appending all horizons since `horizon_min` is already a field on `Outcome`) lets you analyze 15m, 30m, and 60m decay side-by-side in a single file.

### 3. Workflow & Quality-of-Life Features

- **4-Hour GE Buy-Limit Cooldown Tracker** In `--watch` mode, if an item dumps at 12:00, you buy the limit, and it dumps again at 12:35, the scanner will alert you again even though your account is on a 4-hour GE cooldown for that item. Adding a lightweight `active_limits.json` or CLI command to mark an item ID as "bought" (muting it for 4 hours) keeps the live feed actionable.
- **Automated Blacklist for Low-Liquidity / Manipulated Items** Right now `EXCLUDED_ITEM_IDS` only blocks Old School Bonds (`13190`). Deadman/Leagues cosmetics, obscure clue rewards (e.g., 3rd age, trimmed low-tier armor), and newly released items often have wild spreads that trigger false positives. Adding a `blacklist.json` file or filtering out items with `limit <= 5` (unless `buy_1h > 20`) will cut out manipulation traps.
- **Rich Terminal UI & Clipboard Integration**
  - Use the `rich` library (or ANSI color codes) to color-code `STRONG BUY` rows in green, highlight the weakest/strongest pillar (`M/S/D`), and show a live countdown progress bar during `watch_loop`.
  - Add an optional `--copy-top` flag (or hotkey) that copies the top candidate's name or `p_dump` price directly to your Windows clipboard so you can paste it straight into RuneLite's GE search.
- **Discord Webhook / Desktop Toast Notifications** Terminal bell (`\a`) is easy to miss if you're tabbed out or away from the PC. Adding an optional `--webhook <URL>` or Windows toast notification (`win11toast` / PowerShell snippet) that fires on new `STRONG BUY` signals with the item icon, `p_dump`, `p_exit`, and `est_profit` makes `--watch` mode much more hands-off.

## Turn 2

### 👤 User

review plan

### 🤖 Assistant

If you meant to paste or attach a specific implementation plan (such as output from an IDE agent), it didn't come through—drop it in and I will review it line-by-line.
If you want to review a structured **v2.4 Implementation Plan** based on the improvements discussed, here is the prioritized rollout order, exact code specifications, and trade-off analysis for `dump DVS scanner.py` and `dvs_backtest.py`.

## Phase 1: Core Math & Signal Quality (`dump DVS scanner.py`)

### 1. Fix Pre-Dump Baseline Volume Drag (`get_pre_dump_baseline`)

- **Current Behavior:** `get_pre_dump_baseline()` weights `high_1h` by `buy_1h` and `prior_low` by `prior_sell_vol` (`sell_1h - sell_5m`). When an item has heavy 1-hour instasell volume relative to instabuy volume, `p_base` gets dragged down near `prior_low`, causing valid dumps to fail the `avg_low_5m >= p_base` or `MIN_RAW_DROP_PCT` checks.
- **Planned Change:** Cap the weight of `prior_sell_vol` at `2 * buy_1h` when blending `high_1h` and `prior_low`, or use a fixed `0.60 * high_1h + 0.40 * prior_low` blend whenever `buy_1h >= MIN_1H_BUY_VOLUME`.
- **Risk & Trade-off Review:** Anchoring `p_base` closer to `high_1h` can make wide-spread, low-liquidity items look like bigger dumps than they are. However, your existing `MIN_SUPPORT_BREAK_PCT` check (requiring `p_dump` to break at least `1.0%` below `low_1h`) already acts as a guard against static wide spreads.

### 2. Re-curve Pillar B Limit Saturation (`s_score`)

- **Current Behavior:** `l_sat = sell_5m / buy_limit` is scored via `35.0 * math.exp(-0.45 * max(0.0, l_sat - 1.0))`. A tiny 1-unit instasell (`l_sat ≈ 0`) earns a perfect `35.0`, whereas a whale dumping 2x the buy limit (`l_sat = 2.0`) is penalized.
- **Planned Change:** Replace the monotonic decay with a three-zone curve:
  1. **Under-saturated (`l_sat < 0.15`):** Scale linearly from `15.0` to `35.0` (penalizing micro-dumps where you won't fill enough quantity).
  2. **Sweet Spot (`0.15 <= l_sat <= 2.0`):** Award the full `35.0`.
  3. **Over-saturated (`l_sat > 2.0`):** Apply exponential decay `35.0 * math.exp(-0.35 * (l_sat - 2.0))` for extreme supply shocks.
- **Risk & Trade-off Review:** Allowing `l_sat` up to `2.0` without penalty could increase exposure to genuine market crashes, so this should remain paired with Pillar C (`t_clear = sell_5m / max(1, buy_1h)`) and `apply_trend_filter()` to block multi-bucket waterfalls.

### 3. High-Alch Floor Integration (`build_item_map`)

- **Current Behavior:** `dvs_mapping_cache.json` stores each item's `highalch` value, but `build_item_map()` only extracts `name` and `limit`.
- **Planned Change:** Include `"highalch": int(item.get("highalch") or 0)` in `build_item_map()`. During `scan_dumps()`, if `p_dump` is within 1% of `highalch - 100` (approximate nature rune cost), add a `+10.0` floor-safety bonus to `dvs`.

## Phase 2: Backtester Accuracy & Speed (`dvs_backtest.py`)

### 1. Fix Multi-Horizon CSV Export (`main`)

- **Current Behavior:** In `main()`, the check `if args.out and not exported and outcomes:` writes only the first horizon (`30` min by default) to the `--out` CSV and ignores subsequent horizons (`60` min).
- **Planned Change:** Accumulate outcomes across all horizons into a single `all_outcomes: List[Outcome] = []` list inside the `for horizon in args.horizons:` loop, then call `export_outcomes(args.out, all_outcomes)` once after the loop completes.

### 2. Configurable Unhit Exit Pricing (`evaluate_signal`)

- **Current Behavior:** When `hit` is `False`, `evaluate_signal()` marks the position to market at `close_bucket.get("avgHighPrice") or close_bucket.get("avgLowPrice") or sig.p_dump`. Assuming an unhit flip exits at the instabuy ceiling (`avgHighPrice`) overstates profitability on stalled trades.
- **Planned Change:** Add a `--scratch-mode {high,mid,low}` CLI argument (defaulting to `mid`: `0.5 * (avgHighPrice + avgLowPrice)` when both exist, falling back to `avgLowPrice`) so backtest ROI reflects realistic liquidation costs.

### 3. Fill & Exit Velocity Metrics (`Outcome` & `summarize`)

- **Current Behavior:** `evaluate_signal()` locates `fill_idx` and checks `held[1:]` for target hits, but only records boolean `filled` and `hit_target` flags.
- **Planned Change:** Record `fill_minutes: Optional[int]` (`fill_idx * 5`) and `hold_minutes: Optional[int]` (`hit_offset * 5`) on `Outcome`, and display median fill/hold times in `print_horizon_report()`.

### 4. Disk Caching for `/timeseries` (`fetch_series`)

- **Current Behavior:** `fetch_all_series()` re-downloads `/timeseries?timestep=5m` for every item on every backtest execution.
- **Planned Change:** Cache timeseries responses in `SCRIPT_DIR / "timeseries_cache" / f"{item_id}.json"`, only querying the API if `max(sig.timestamp) + max(horizons) * 60` exceeds the newest cached bucket timestamp.

## Phase 3: Workflow & Execution Guards

| Feature | Target File | Implementation Detail |
| --- | --- | --- |
| **Low-Liquidity Trap Filter** | `dump DVS scanner.py` | In `scan_dumps()`, require `buy_1h >= 15` whenever `buy_limit <= 8` to block manipulation spikes on rare clue/cosmetic gear. |
| **4h Buy-Limit Tracker** | `dump DVS scanner.py` | Load a local `cooldowns.json` mapping `item_id -> expiry_unix` to suppress alerts for items already flipped within the last 4 hours. |
| **Clipboard Auto-Copy** | `dump DVS scanner.py` | Add a `--clip` flag to `parse_args()` that copies the #1 `STRONG BUY` item name to the Windows clipboard via `subprocess.run(["clip"], input=...)` when a new signal appears. |

Did you have a separate plan file you wanted me to critique, or should we go ahead and implement Phase 1 and Phase 2 into the scripts?

---

*Exported from [Voyager](https://github.com/voyager-crew/voyager)*  
*Generated on October 15, 2026 at 02:28 AM*