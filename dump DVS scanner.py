import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Counter as CounterType, Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "2.7"
API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
HEADERS = {"User-Agent": f"DVS-Dump-Scanner/{VERSION} (Custom GE Market Flipping Tool)"}

# Blacklist untradeable-on-buy or manipulated items
EXCLUDED_ITEM_IDS: Set[int] = {13190}  # Old school bond
EXCLUDED_ITEM_NAMES: Set[str] = {"old school bond"}

# =====================================================================
# TUNABLE SCANNER PARAMETERS
# =====================================================================
MIN_DVS_SCORE = 55.0            # 55+ = CAUTION / BUY, 75+ = STRONG BUY
MIN_MARGIN_PILLAR = 8.0         # Requires ~1.4% post-tax Net ROI (~3.4% gross bounce)
MIN_DEMAND_PILLAR = 5.0         # Blocks dumps that vastly overwhelm 1h buyer absorption
MIN_EST_PROFIT = 80_000         # Minimum estimated GP profit per GE slot (80k gp)
MAX_SLOT_CAPITAL = 100_000_000  # Max GP to tie up in a single slot (None = unlimited)
MIN_RAW_DROP_PCT = 2.5          # Minimum % drop below 55m pre-dump baseline
MIN_SUPPORT_BREAK_PCT = 0.5     # Must drop at least 0.5% below reconstructed 1h pre-dump instasell floor
MIN_1H_BUY_VOLUME = 4           # Minimum 1h instabuy volume (proves buyer absorption)
MIN_ITEM_PRICE = 50             # Block penny items (runes/feathers) locked in 1-gp integer spreads
RETRACEMENT_TARGET = 0.75       # 75% bounce target toward pre-dump baseline
MAX_RECOVERY_FRACTION = 0.65    # Allow up to 65% bounce on /latest before marking recovered
TOP_N_RESULTS = 25
NEAR_MISS_COUNT = 5             # Number of runner-up dumps to show when 0 pass strict filters

# Falling-knife check (uses /timeseries 5m buckets on finalists only)
KNIFE_CHECK_ENABLED = True
KNIFE_LOOKBACK_BUCKETS = 3              # Consecutive 5m buckets that must each print a lower low
KNIFE_MIN_CONTINUED_DROP_PCT = 2.0      # ...and fall at least this % across that span to be rejected
KNIFE_MAX_CHECKS = 50                   # Cap on /timeseries calls per scan
KNIFE_BATCH_SIZE = 8                    # Concurrent batch size for short-circuiting /timeseries checks

# Infrastructure parameters
MAX_LATEST_AGE_SECONDS = 600    # Ignore /latest high/low trades older than 10 minutes
LIVE_DUMP_MAX_AGE_SECONDS = 180 # Allow /latest low to trigger a live dump if < 3 minutes old
MAPPING_CACHE_TTL_SECONDS = 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 15
WATCH_BUCKET_SECONDS = 300      # The wiki /5m endpoint rolls over every 5 minutes
WATCH_BOUNDARY_DELAY_SECONDS = 25  # Wait after each boundary so the new bucket is published

SCRIPT_DIR = Path(__file__).resolve().parent
MAPPING_CACHE_PATH = SCRIPT_DIR / "dvs_mapping_cache.json"
CANDIDATE_LOG_PATH = SCRIPT_DIR / "dvs_candidate_log.csv"


@dataclass
class DumpCandidate:
    item_id: int
    name: str
    p_dump: int
    p_exit: int
    net_roi_pct: float
    dvs: float
    m_score: float
    s_score: float
    d_score: float
    est_fill: int
    est_profit: int
    status: str
    p_base: int = 0
    raw_drop_pct: float = 0.0
    trend_check: str = "skipped"
    alch_bonus: float = 0.0


def calculate_ge_tax(sell_price: int) -> int:
    """Calculates exact OSRS GE 2% tax (exempt under 50 gp, capped at 5M gp)."""
    if sell_price < 50:
        return 0
    return min(int(math.floor(sell_price * 0.02)), 5_000_000)


# =====================================================================
# NETWORKING (retrying session, cached mapping, concurrent fetch)
# =====================================================================
def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def fetch_endpoint(session: requests.Session, endpoint: str, params: Optional[Dict] = None) -> Dict:
    resp = session.get(f"{API_BASE}/{endpoint}", params=params, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def load_mapping(session: requests.Session) -> List[Dict]:
    """Returns /mapping, served from a local cache while it is younger than the TTL."""
    try:
        if MAPPING_CACHE_PATH.exists():
            age = time.time() - MAPPING_CACHE_PATH.stat().st_mtime
            if age < MAPPING_CACHE_TTL_SECONDS:
                with MAPPING_CACHE_PATH.open("r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if isinstance(cached, list) and cached:
                    return cached
    except (OSError, ValueError):
        pass

    mapping = fetch_endpoint(session, "mapping")
    try:
        tmp_path = MAPPING_CACHE_PATH.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(mapping, fh)
        tmp_path.replace(MAPPING_CACHE_PATH)
    except OSError:
        pass
    return mapping


def fetch_snapshots(session: requests.Session) -> Tuple[List[Dict], Dict, Dict, Dict, Dict]:
    """Fetches mapping (cached) plus /5m, /1h, /24h, /latest concurrently."""
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_mapping = pool.submit(load_mapping, session)
        f_5m = pool.submit(fetch_endpoint, session, "5m")
        f_1h = pool.submit(fetch_endpoint, session, "1h")
        f_24h = pool.submit(fetch_endpoint, session, "24h")
        f_latest = pool.submit(fetch_endpoint, session, "latest")

        mapping = f_mapping.result()
        data_5m = f_5m.result().get("data", {})
        data_1h = f_1h.result().get("data", {})
        data_24h = f_24h.result().get("data", {})
        data_latest = f_latest.result().get("data", {})
    return mapping, data_5m, data_1h, data_24h, data_latest


# =====================================================================
# CORE LOGIC
# =====================================================================
def get_pre_dump_baseline_and_support(
    high_1h: int, low_1h: int, buy_1h: int, sell_1h: int, p_dump: int, sell_5m: int
) -> Tuple[int, int]:
    """
    Subtracts the 5m dump from 1h data to reconstruct both:
      - p_base: the 55m pre-dump fair baseline price
      - prior_low: the 55m pre-dump instasell support floor (uncontaminated by the 5m dump)
    """
    prior_sell_vol = max(0, sell_1h - sell_5m)
    if prior_sell_vol > 0 and low_1h > 0:
        prior_low = max(
            low_1h,
            int(round(((low_1h * sell_1h) - (p_dump * sell_5m)) / prior_sell_vol)),
        )
    else:
        prior_low = high_1h if high_1h > 0 else low_1h

    if high_1h > 0 and prior_low > high_1h:
        prior_low = high_1h

    if buy_1h > 0 and prior_sell_vol > 0 and high_1h > 0:
        if buy_1h >= MIN_1H_BUY_VOLUME and prior_low > 0:
            p_base = int(round(0.60 * high_1h + 0.40 * prior_low))
            return p_base, prior_low
        effective_sell_vol = min(prior_sell_vol, 2 * buy_1h)
        total_vol = buy_1h + effective_sell_vol
        p_base = int(round(((high_1h * buy_1h) + (prior_low * effective_sell_vol)) / total_vol))
        return p_base, prior_low

    p_base = high_1h if high_1h > 0 else prior_low
    return p_base, prior_low


def fresh_price(
    price: Optional[int], timestamp: Optional[int], now: float, max_age: float = MAX_LATEST_AGE_SECONDS
) -> Optional[int]:
    """Returns a /latest price only if its trade timestamp is within max_age seconds."""
    if not price or price <= 0 or not timestamp:
        return None
    if now - timestamp > max_age:
        return None
    return int(price)


def build_item_map(mapping_raw: List[Dict]) -> Dict[str, Dict]:
    item_map: Dict[str, Dict] = {}
    for item in mapping_raw:
        item_id = item.get("id")
        name = item.get("name", f"Item {item_id}")
        limit = item.get("limit")
        if not item_id or not limit or limit <= 0:
            continue
        if int(item_id) in EXCLUDED_ITEM_IDS or name.strip().lower() in EXCLUDED_ITEM_NAMES:
            continue
        item_map[str(item_id)] = {
            "name": name,
            "limit": int(limit),
            "highalch": int(item.get("highalch") or 0),
        }
    return item_map


def evaluate_still_falling(lows: List[int]) -> Tuple[bool, float]:
    needed = KNIFE_LOOKBACK_BUCKETS
    if len(lows) < needed:
        return False, 0.0
    window = lows[-needed:]
    if window[0] <= 0:
        return False, 0.0
    strictly_lower = all(window[i] < window[i - 1] for i in range(1, needed))
    drop_pct = ((window[0] - window[-1]) / window[0]) * 100.0
    return (strictly_lower and drop_pct >= KNIFE_MIN_CONTINUED_DROP_PCT), drop_pct


def check_trend(session: requests.Session, item_id: int) -> Tuple[str, float]:
    try:
        payload = fetch_endpoint(session, "timeseries", {"timestep": "5m", "id": item_id})
    except (requests.RequestException, ValueError):
        return "unavailable", 0.0

    raw_series = payload.get("data") or []
    series = sorted((pt for pt in raw_series if pt.get("timestamp")), key=lambda pt: pt["timestamp"])
    if len(series) < KNIFE_LOOKBACK_BUCKETS:
        return "unavailable", 0.0

    recent_points = series[-KNIFE_LOOKBACK_BUCKETS:]
    for i in range(1, len(recent_points)):
        if recent_points[i]["timestamp"] - recent_points[i - 1]["timestamp"] != WATCH_BUCKET_SECONDS:
            return "unavailable", 0.0

    lows = [int(pt["avgLowPrice"]) for pt in recent_points if pt.get("avgLowPrice")]
    if len(lows) < KNIFE_LOOKBACK_BUCKETS:
        return "unavailable", 0.0

    falling, drop_pct = evaluate_still_falling(lows)
    return ("falling" if falling else "ok"), drop_pct


def apply_trend_filter(
    session: requests.Session,
    candidates: List[DumpCandidate],
    funnel: CounterType[str],
    target_count: int = TOP_N_RESULTS,
    enabled: bool = KNIFE_CHECK_ENABLED,
) -> List[DumpCandidate]:
    if not enabled or not candidates:
        return candidates

    to_check = candidates[:KNIFE_MAX_CHECKS]
    untouched = candidates[KNIFE_MAX_CHECKS:]
    survivors: List[DumpCandidate] = []
    idx = 0

    with ThreadPoolExecutor(max_workers=KNIFE_BATCH_SIZE) as pool:
        while idx < len(to_check) and len(survivors) < target_count:
            batch = to_check[idx : idx + KNIFE_BATCH_SIZE]
            idx += len(batch)
            results = list(pool.map(lambda c: check_trend(session, c.item_id), batch))
            for cand, (state, _drop) in zip(batch, results):
                if state == "falling":
                    funnel["rej: still falling (timeseries)"] += 1
                    continue
                if state == "unavailable":
                    funnel["info: timeseries unavailable, kept"] += 1
                cand.trend_check = state
                survivors.append(cand)

    remaining = to_check[idx:] + untouched
    for cand in remaining:
        cand.trend_check = "skipped"

    return survivors + remaining


def allocate_portfolio(
    candidates: List[DumpCandidate],
    max_slots: Optional[int] = None,
    bankroll: Optional[int] = None,
    funnel: Optional[CounterType[str]] = None,
    min_est_profit: int = MIN_EST_PROFIT,
) -> List[DumpCandidate]:
    if not candidates or (max_slots is None and bankroll is None):
        return candidates

    allocated: List[DumpCandidate] = []
    remaining_gp = bankroll

    for cand in candidates:
        if max_slots is not None and len(allocated) >= max_slots:
            break

        unit_profit = cand.est_profit // max(1, cand.est_fill)
        fill = cand.est_fill

        if remaining_gp is not None and cand.p_dump > 0:
            affordable_fill = remaining_gp // cand.p_dump
            fill = min(fill, affordable_fill)

        est_profit = unit_profit * fill
        if fill <= 0 or est_profit < min_est_profit:
            if funnel is not None:
                funnel["rej: insufficient bankroll for min profit"] += 1
            continue

        cand.est_fill = fill
        cand.est_profit = est_profit
        if remaining_gp is not None:
            remaining_gp -= fill * cand.p_dump
        allocated.append(cand)

    return allocated


def scan_dumps(
    session: requests.Session,
    knife_check_enabled: bool = KNIFE_CHECK_ENABLED,
    max_slots: Optional[int] = None,
    bankroll: Optional[int] = None,
    near_misses_out: Optional[List[DumpCandidate]] = None,
    min_dvs_score: float = MIN_DVS_SCORE,
    min_margin_pillar: float = MIN_MARGIN_PILLAR,
    min_demand_pillar: float = MIN_DEMAND_PILLAR,
    min_est_profit: int = MIN_EST_PROFIT,
    max_slot_capital: Optional[int] = MAX_SLOT_CAPITAL,
    min_raw_drop_pct: float = MIN_RAW_DROP_PCT,
    min_support_break_pct: float = MIN_SUPPORT_BREAK_PCT,
    min_item_price: int = MIN_ITEM_PRICE,
    retracement_target: float = RETRACEMENT_TARGET,
    max_recovery_fraction: float = MAX_RECOVERY_FRACTION,
) -> Tuple[List[DumpCandidate], CounterType[str]]:
    mapping_raw, data_5m, data_1h, data_24h, data_latest = fetch_snapshots(session)
    item_map = build_item_map(mapping_raw)
    now = time.time()

    funnel: CounterType[str] = Counter()
    candidates: List[DumpCandidate] = []
    near_misses: List[DumpCandidate] = []

    all_item_ids = set(data_5m.keys()) | set(data_1h.keys())

    for item_id in all_item_ids:
        funnel["scanned"] += 1
        if item_id not in item_map:
            funnel["rej: not in mapping / no limit / excluded"] += 1
            continue
        if item_id not in data_1h:
            funnel["rej: no 1h data"] += 1
            continue

        tick_5m = data_5m.get(item_id, {})
        tick_1h = data_1h[item_id]
        tick_24h = data_24h.get(item_id, {})
        latest = data_latest.get(item_id, {})
        meta = item_map[item_id]
        buy_limit = meta["limit"]

        high_24h = tick_24h.get("avgHighPrice") or 0
        buy_24h = tick_24h.get("highPriceVolume") or 0

        avg_low_5m = tick_5m.get("avgLowPrice") or 0
        sell_5m = tick_5m.get("lowPriceVolume") or 0
        buy_5m = tick_5m.get("highPriceVolume") or 0

        latest_low = fresh_price(latest.get("low"), latest.get("lowTime"), now)
        latest_low_very_fresh = fresh_price(
            latest.get("low"), latest.get("lowTime"), now, max_age=LIVE_DUMP_MAX_AGE_SECONDS
        )
        latest_high = fresh_price(latest.get("high"), latest.get("highTime"), now)

        # Allow item if it has 5m instasell volume OR a brand-new live dump on /latest
        has_5m_sell = avg_low_5m > 0 and sell_5m > 0
        if not has_5m_sell and not latest_low_very_fresh:
            funnel["rej: no 5m or live instasell activity"] += 1
            continue

        high_1h = tick_1h.get("avgHighPrice") or 0
        low_1h = tick_1h.get("avgLowPrice") or 0
        buy_1h = tick_1h.get("highPriceVolume") or 0
        sell_1h = tick_1h.get("lowPriceVolume") or 0

        if buy_1h < MIN_1H_BUY_VOLUME:
            funnel["rej: 1h instabuy volume too low"] += 1
            continue

        if buy_limit <= 8 and buy_1h < 15:
            funnel["rej: low-liquidity trap (limit <= 8, buy_1h < 15)"] += 1
            continue

        # Determine provisional dump price to reconstruct baseline
        provisional_dump = (
            min(avg_low_5m, latest_low)
            if (has_5m_sell and latest_low)
            else (avg_low_5m if has_5m_sell else latest_low_very_fresh)
        )
        effective_sell_5m = sell_5m if has_5m_sell else max(1, int(0.15 * buy_limit))

        p_base, prior_low = get_pre_dump_baseline_and_support(
            high_1h, low_1h, buy_1h, sell_1h, provisional_dump, effective_sell_5m
        )
        if p_base <= 0 or provisional_dump >= p_base:
            funnel["rej: price not below baseline"] += 1
            continue

        # Check if 5m dump already bounced on /latest (only when latest_low is NOT itself a fresh dump)
        if has_5m_sell and latest_low and latest_low > avg_low_5m:
            if latest_low > (avg_low_5m + max_recovery_fraction * (p_base - avg_low_5m)):
                funnel["rej: dump already recovered on /latest"] += 1
                continue

        p_dump = provisional_dump

        if p_dump < min_item_price or (p_base - p_dump) < 2:
            funnel["rej: penny item / 1-gp spread"] += 1
            continue

        if high_24h > 0 and buy_24h >= 20 and p_dump > int(high_24h * 1.05):
            funnel["rej: dump price exceeds 24h high (unwinding pump)"] += 1
            continue

        # Compare against reconstructed prior_low so big dumps don't contaminate their own support check
        if prior_low > 0:
            support_drop_pct = ((prior_low - p_dump) / prior_low) * 100.0
            if support_drop_pct < min_support_break_pct:
                funnel["rej: no 1h support break"] += 1
                continue

        raw_drop_pct = ((p_base - p_dump) / p_base) * 100.0
        if raw_drop_pct < min_raw_drop_pct:
            funnel["rej: raw drop too small"] += 1
            continue

        raw_exit = int(round(p_dump + retracement_target * (p_base - p_dump)))
        if latest_high:
            if latest_high <= p_dump:
                funnel["rej: instabuy ceiling collapsed"] += 1
                continue
            p_exit = min(raw_exit, latest_high - 1)
        else:
            p_exit = raw_exit

        if high_24h > 0 and buy_24h >= 20:
            p_exit = min(p_exit, max(p_dump + 1, int(round(high_24h * 1.02))))

        tax = calculate_ge_tax(p_exit)
        unit_profit = p_exit - tax - p_dump
        net_roi_pct = (unit_profit / p_dump) * 100.0
        if net_roi_pct <= 0.5 or unit_profit <= 0:
            funnel["rej: net ROI <= 0.5%"] += 1
            continue

        m_score = min(40.0, 40.0 * ((net_roi_pct - 0.5) / 4.5))

        l_sat = effective_sell_5m / buy_limit
        if l_sat < 0.15:
            s_score = 15.0 + (20.0 * (l_sat / 0.15))
        elif l_sat <= 2.0:
            s_score = 35.0
        else:
            s_score = 35.0 * math.exp(-0.35 * (l_sat - 2.0))

        t_clear = effective_sell_5m / max(1, buy_1h)
        b_live = buy_5m / max(1, (effective_sell_5m + buy_5m))
        d_score = (20.0 / (1.0 + 1.5 * t_clear)) + (5.0 * b_live)

        highalch = meta.get("highalch", 0)
        alch_net = max(0, highalch - 100)
        alch_bonus = 10.0 if (alch_net > 0 and p_dump <= int(round(alch_net * 1.01))) else 0.0

        dvs = m_score + s_score + d_score + alch_bonus

        half_hour_buyer_cap = max(1, int(math.floor(0.5 * buy_1h)))
        est_fill = min(buy_limit, effective_sell_5m, half_hour_buyer_cap)
        slot_cap_gp = (
            min(max_slot_capital, bankroll)
            if (max_slot_capital and bankroll)
            else (bankroll or max_slot_capital)
        )
        if slot_cap_gp and p_dump > 0:
            capital_cap = max(0, slot_cap_gp // p_dump)
            est_fill = min(est_fill, capital_cap)
        if est_fill <= 0:
            funnel["rej: zero est fill (capital cap)"] += 1
            continue

        est_profit = unit_profit * est_fill

        cand_obj = DumpCandidate(
            item_id=int(item_id),
            name=meta["name"],
            p_dump=p_dump,
            p_exit=p_exit,
            net_roi_pct=net_roi_pct,
            dvs=dvs,
            m_score=m_score,
            s_score=s_score,
            d_score=d_score,
            est_fill=est_fill,
            est_profit=est_profit,
            status="NEAR MISS",
            p_base=p_base,
            raw_drop_pct=raw_drop_pct,
            alch_bonus=alch_bonus,
        )

        # Track near misses (profitable after tax with >20k est profit) before strict pillar/DVS cuts
        if m_score < min_margin_pillar:
            funnel["rej: margin pillar below minimum"] += 1
            if est_profit >= 25_000 and dvs >= 40.0:
                near_misses.append(cand_obj)
            continue

        if d_score < min_demand_pillar:
            funnel["rej: demand pillar below minimum"] += 1
            if est_profit >= 25_000 and dvs >= 40.0:
                near_misses.append(cand_obj)
            continue

        if dvs < min_dvs_score:
            funnel["rej: DVS below minimum"] += 1
            if est_profit >= 25_000:
                near_misses.append(cand_obj)
            continue

        if est_profit < min_est_profit:
            funnel["rej: est profit below minimum"] += 1
            if est_profit >= 25_000:
                near_misses.append(cand_obj)
            continue

        cand_obj.status = "STRONG BUY" if (dvs >= 75.0 and d_score >= 8.0) else "CAUTION / BUY"
        funnel["passed primary filters"] += 1
        candidates.append(cand_obj)

    candidates.sort(key=lambda c: (c.dvs, c.est_profit), reverse=True)
    desired_count = min(TOP_N_RESULTS, max_slots) if max_slots else TOP_N_RESULTS
    candidates = apply_trend_filter(
        session, candidates, funnel, target_count=desired_count, enabled=knife_check_enabled
    )
    final = candidates[:TOP_N_RESULTS]
    final = allocate_portfolio(
        final, max_slots=max_slots, bankroll=bankroll, funnel=funnel, min_est_profit=min_est_profit
    )
    funnel["accepted"] = len(final)

    if near_misses_out is not None:
        near_misses.sort(key=lambda c: (c.dvs, c.est_profit), reverse=True)
        near_misses_out.extend(near_misses[:NEAR_MISS_COUNT])

    return final, funnel


# =====================================================================
# OUTPUT & ALERTS
# =====================================================================
def copy_to_clipboard(text: str) -> bool:
    for cmd in (["clip"], ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def send_webhook_alert(
    session: requests.Session, webhook_url: str, candidates: List[DumpCandidate]
) -> None:
    if not webhook_url or not candidates:
        return
    lines = [
        f"**{c.name}** (ID `{c.item_id}`) — Buy `{c.p_dump:,}` -> Exit `{c.p_exit:,}` "
        f"| ROI `{c.net_roi_pct:.2f}%` | DVS `{c.dvs:.1f}` | Fill `{c.est_fill:,}` (`{c.est_profit:,} gp`)"
        for c in candidates[:10]
    ]
    payload = {
        "username": f"DVS Scanner v{VERSION}",
        "content": f"**{len(candidates)} STRONG BUY Dump Signal(s) Detected**\n" + "\n".join(lines),
    }
    try:
        resp = session.post(webhook_url, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Warning: webhook alert failed ({exc}).", file=sys.stderr)


def print_report(
    candidates: List[DumpCandidate],
    new_ids: Optional[Set[int]] = None,
    near_misses: Optional[List[DumpCandidate]] = None,
) -> None:
    width = 120
    print("=" * width)
    print(f"OSRS DUMP VULNERABILITY SCORE (DVS) SCANNER v{VERSION}".center(width))
    print("=" * width)
    header = (
        f"{'Item Name':<24} | {'Dump Price':>10} | {'Exit (Cap)':>10} | "
        f"{'Net ROI':>7} | {'DVS':>5} | {'Pillars (M/S/D)':>15} | "
        f"{'Est Fill':>8} | {'Est Profit':>13} | {'Status'}"
    )
    print(header)
    print("-" * width)

    if not candidates:
        print("No strict qualifying dumps found on this 5-minute tick.".center(width))
        if near_misses:
            print("-" * width)
            print("TOP NEAR-MISS WATCHLIST (slightly below strict DVS / profit thresholds):".center(width))
            print("-" * width)
            for c in near_misses:
                pillars = f"{c.m_score:.1f}/{c.s_score:.1f}/{c.d_score:.1f}"
                profit_str = f"{c.est_profit:,} gp"
                print(
                    f"{c.name[:24]:<24} | {c.p_dump:>10,} | {c.p_exit:>10,} | "
                    f"{c.net_roi_pct:>6.2f}% | {c.dvs:>5.1f} | {pillars:>15} | "
                    f"{c.est_fill:>8,} | {profit_str:>13} | {c.status}"
                )
    else:
        for c in candidates:
            pillars = f"{c.m_score:.1f}/{c.s_score:.1f}/{c.d_score:.1f}"
            profit_str = f"{c.est_profit:,} gp"
            marker = " *NEW*" if new_ids and c.item_id in new_ids else ""
            status_str = f"{c.status} [ALCH]" if c.alch_bonus > 0 else c.status
            print(
                f"{c.name[:24]:<24} | {c.p_dump:>10,} | {c.p_exit:>10,} | "
                f"{c.net_roi_pct:>6.2f}% | {c.dvs:>5.1f} | {pillars:>15} | "
                f"{c.est_fill:>8,} | {profit_str:>13} | {status_str}{marker}"
            )

    print("=" * width)
    total_capital = sum(c.p_dump * c.est_fill for c in candidates)
    total_profit = sum(c.est_profit for c in candidates)
    print(
        f"Showing top {len(candidates)} opportunities (Capital: {total_capital:,} gp | "
        f"Est Profit: {total_profit:,} gp). All prices post-tax (2% capped at 5M)."
    )


def print_funnel(funnel: CounterType[str]) -> None:
    print("\nREJECTION FUNNEL (why items were filtered out)")
    print("-" * 60)
    scanned = funnel.get("scanned", 0)
    print(f"{'scanned':<48} {scanned:>8,}")
    for key, count in sorted(funnel.items(), key=lambda kv: kv[1], reverse=True):
        if key == "scanned":
            continue
        print(f"{key:<48} {count:>8,}")


def log_candidates(candidates: List[DumpCandidate]) -> Optional[Path]:
    if not candidates:
        return None
    field_names = ["timestamp_utc", "timestamp_unix"] + [f.name for f in fields(DumpCandidate)]
    now = time.time()
    current_bucket = int(now) // WATCH_BUCKET_SECONDS
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing_bucket_items: Set[Tuple[int, int]] = set()

    try:
        if CANDIDATE_LOG_PATH.exists() and CANDIDATE_LOG_PATH.stat().st_size > 0:
            with CANDIDATE_LOG_PATH.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                existing_header = next(reader, [])
                if existing_header == field_names:
                    ts_idx = existing_header.index("timestamp_unix")
                    id_idx = existing_header.index("item_id")
                    for row in reader:
                        try:
                            row_bucket = int(row[ts_idx]) // WATCH_BUCKET_SECONDS
                            if row_bucket == current_bucket:
                                existing_bucket_items.add((row_bucket, int(row[id_idx])))
                        except (IndexError, ValueError):
                            continue
            if existing_header != field_names:
                archive = CANDIDATE_LOG_PATH.with_name(
                    f"{CANDIDATE_LOG_PATH.stem}.{int(now)}.old{CANDIDATE_LOG_PATH.suffix}"
                )
                CANDIDATE_LOG_PATH.replace(archive)

        to_write = [
            c for c in candidates if (current_bucket, c.item_id) not in existing_bucket_items
        ]
        if not to_write:
            return None

        write_header = not CANDIDATE_LOG_PATH.exists() or CANDIDATE_LOG_PATH.stat().st_size == 0
        with CANDIDATE_LOG_PATH.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=field_names)
            if write_header:
                writer.writeheader()
            for c in to_write:
                row = asdict(c)
                row["timestamp_utc"] = stamp
                row["timestamp_unix"] = int(now)
                writer.writerow(row)
        return CANDIDATE_LOG_PATH
    except OSError as exc:
        print(f"Warning: could not write candidate log ({exc}).", file=sys.stderr)
        return None


# =====================================================================
# CLI
# =====================================================================
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSRS Dump Vulnerability Score (DVS) scanner")
    parser.add_argument(
        "--debug", action="store_true", help="Print the rejection funnel showing why items were filtered"
    )
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Use looser thresholds (DVS >= 45, Est Profit >= 40k, Drop >= 2.0%%) for slow market hours",
    )
    parser.add_argument(
        "--no-log", action="store_true", help="Do not append candidates to the CSV log"
    )
    parser.add_argument(
        "--no-knife-check",
        action="store_true",
        help="Skip the /timeseries falling-knife check on finalists",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Re-scan after every 5-minute bucket boundary until interrupted (Ctrl+C)",
    )
    parser.add_argument(
        "--clip",
        action="store_true",
        help="Copy the top candidate item name to clipboard",
    )
    parser.add_argument(
        "--slots",
        type=int,
        default=None,
        help="Cap results to available GE slots (e.g. --slots 8)",
    )
    parser.add_argument(
        "--bankroll",
        type=int,
        default=None,
        help="Total GP capital available to allocate across all slots",
    )
    parser.add_argument(
        "--min-price",
        type=int,
        default=MIN_ITEM_PRICE,
        help=f"Minimum item price in GP to consider (default: {MIN_ITEM_PRICE})",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=os.environ.get("DVS_WEBHOOK_URL"),
        help="Discord/webhook URL for STRONG BUY alerts (or set DVS_WEBHOOK_URL env var)",
    )
    return parser.parse_args(argv)


def run_scan_once(
    session: requests.Session,
    args: argparse.Namespace,
    previous_ids: Optional[Set[int]] = None,
) -> Optional[List[DumpCandidate]]:
    min_dvs = 45.0 if getattr(args, "relaxed", False) else MIN_DVS_SCORE
    min_profit = 40_000 if getattr(args, "relaxed", False) else MIN_EST_PROFIT
    min_drop = 2.0 if getattr(args, "relaxed", False) else MIN_RAW_DROP_PCT
    min_margin = 5.0 if getattr(args, "relaxed", False) else MIN_MARGIN_PILLAR
    min_price = getattr(args, "min_price", MIN_ITEM_PRICE)

    knife_check = KNIFE_CHECK_ENABLED and not args.no_knife_check
    near_misses: List[DumpCandidate] = []
    try:
        candidates, funnel = scan_dumps(
            session,
            knife_check_enabled=knife_check,
            max_slots=args.slots,
            bankroll=args.bankroll,
            near_misses_out=near_misses,
            min_dvs_score=min_dvs,
            min_margin_pillar=min_margin,
            min_est_profit=min_profit,
            min_raw_drop_pct=min_drop,
            min_item_price=min_price,
        )
    except requests.RequestException as exc:
        print(f"Network error while querying the OSRS Wiki API: {exc}", file=sys.stderr)
        return None

    current_ids = {c.item_id for c in candidates}
    new_ids = (current_ids - previous_ids) if previous_ids is not None else None

    print_report(candidates, new_ids, near_misses=near_misses)
    if not args.no_log:
        logged = log_candidates(candidates)
        if logged:
            print(f"Logged candidate(s) to {logged}")

    if args.clip and (candidates or near_misses):
        top_name = (candidates[0] if candidates else near_misses[0]).name
        if copy_to_clipboard(top_name):
            print(f"Copied '{top_name}' to clipboard.")
        else:
            print("Warning: no supported clipboard utility found.", file=sys.stderr)

    if args.debug:
        print_funnel(funnel)
    elif not candidates:
        print("Tip: run with --relaxed for looser filters, or --watch to scan every 5m tick automatically.")

    if new_ids:
        strong_new = [c for c in candidates if c.item_id in new_ids and c.status == "STRONG BUY"]
        names = ", ".join(c.name for c in candidates if c.item_id in new_ids)
        print(f"NEW since last scan: {names}")
        if strong_new:
            print("\a", end="", flush=True)
            if args.webhook_url:
                send_webhook_alert(session, args.webhook_url, strong_new)
    elif previous_ids is None and args.webhook_url:
        strong_initial = [c for c in candidates if c.status == "STRONG BUY"]
        if strong_initial:
            send_webhook_alert(session, args.webhook_url, strong_initial)

    return candidates


def seconds_until_next_bucket(now: Optional[float] = None) -> float:
    now = time.time() if now is None else now
    next_boundary = (math.floor(now / WATCH_BUCKET_SECONDS) + 1) * WATCH_BUCKET_SECONDS
    return max(1.0, next_boundary + WATCH_BOUNDARY_DELAY_SECONDS - now)


def watch_loop(session: requests.Session, args: argparse.Namespace) -> int:
    previous_ids: Optional[Set[int]] = None
    print(
        f"Watch mode: scanning now, then {WATCH_BOUNDARY_DELAY_SECONDS}s after each "
        f"5-minute boundary. Press Ctrl+C to stop."
    )
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{stamp}] Scanning...")
        candidates = run_scan_once(session, args, previous_ids)
        if candidates is not None:
            previous_ids = {c.item_id for c in candidates}
        wait = seconds_until_next_bucket()
        print(f"Next scan in {int(wait)}s.")
        time.sleep(wait)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    session = build_session()
    try:
        if args.watch:
            return watch_loop(session, args)
        result = run_scan_once(session, args)
        return 0 if result is not None else 1
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())