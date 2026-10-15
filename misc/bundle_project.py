import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
ZIP_NAME = "dump_finder_v2.5.zip"

SCANNER_CODE = r'''import argparse
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

VERSION = "2.5"
API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
HEADERS = {"User-Agent": f"DVS-Dump-Scanner/{VERSION} (Custom GE Market Flipping Tool)"}

# Blacklist untradeable-on-buy or manipulated items
EXCLUDED_ITEM_IDS: Set[int] = {13190}  # Old school bond
EXCLUDED_ITEM_NAMES: Set[str] = {"old school bond"}

# =====================================================================
# TUNABLE SCANNER PARAMETERS
# =====================================================================
MIN_DVS_SCORE = 55.0            # 55+ = CAUTION / BUY, 75+ = STRONG BUY
MIN_MARGIN_PILLAR = 10.0        # Blocks items with <1.6% Net ROI coasting on volume
MIN_DEMAND_PILLAR = 6.0         # Blocks dumps that vastly overwhelm 1h buyer absorption (t_clear > ~1.5)
MIN_EST_PROFIT = 80_000         # Minimum estimated GP profit per GE slot (80k gp)
MAX_SLOT_CAPITAL = 100_000_000  # Max GP to tie up in a single slot (None = unlimited)
MIN_RAW_DROP_PCT = 3.0          # Minimum % drop below 55m pre-dump baseline
MIN_SUPPORT_BREAK_PCT = 1.0     # Must drop at least 1.0% below 1h instasell floor
MIN_1H_BUY_VOLUME = 4           # Minimum 1h instabuy volume (proves buyer absorption)
RETRACEMENT_TARGET = 0.75       # 75% bounce target toward pre-dump baseline
TOP_N_RESULTS = 25

# Falling-knife check (uses /timeseries 5m buckets on finalists only)
KNIFE_CHECK_ENABLED = True
KNIFE_LOOKBACK_BUCKETS = 3              # Consecutive 5m buckets that must each print a lower low
KNIFE_MIN_CONTINUED_DROP_PCT = 2.0      # ...and fall at least this % across that span to be rejected
KNIFE_MAX_CHECKS = 50                   # Cap on /timeseries calls per scan
KNIFE_BATCH_SIZE = 8                    # Concurrent batch size for short-circuiting /timeseries checks

# Infrastructure parameters
MAX_LATEST_AGE_SECONDS = 600    # Ignore /latest high/low trades older than 10 minutes
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
        pass  # Corrupt or unreadable cache: fall through to a fresh fetch

    mapping = fetch_endpoint(session, "mapping")
    try:
        tmp_path = MAPPING_CACHE_PATH.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(mapping, fh)
        tmp_path.replace(MAPPING_CACHE_PATH)
    except OSError:
        pass  # Cache is best-effort only
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
def get_pre_dump_baseline(
    high_1h: int, low_1h: int, buy_1h: int, sell_1h: int, p_dump: int, sell_5m: int
) -> int:
    """Subtracts the 5m dump from 1h data to reconstruct the 55m pre-dump baseline."""
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
            return int(round(0.60 * high_1h + 0.40 * prior_low))
        effective_sell_vol = min(prior_sell_vol, 2 * buy_1h)
        total_vol = buy_1h + effective_sell_vol
        return int(round(((high_1h * buy_1h) + (prior_low * effective_sell_vol)) / total_vol))
    return high_1h if high_1h > 0 else prior_low


def fresh_price(price: Optional[int], timestamp: Optional[int], now: float) -> Optional[int]:
    """Returns a /latest price only if its trade timestamp is within MAX_LATEST_AGE_SECONDS."""
    if not price or price <= 0 or not timestamp:
        return None
    if now - timestamp > MAX_LATEST_AGE_SECONDS:
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
    """
    Given the most recent 5m avgLowPrice values (oldest -> newest, no gaps), returns
    (is_falling_knife, total_drop_pct).
    """
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
    """
    Fetches 5m /timeseries for one item and classifies its trend across consecutive buckets.
    Returns (state, drop_pct) where state is 'ok', 'falling', or 'unavailable'.
    """
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
    """
    Drops candidates whose price is still making consecutive lower lows.
    Batches requests and short-circuits once `target_count` survivors are found.
    """
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
) -> List[DumpCandidate]:
    """
    Sizes and filters top candidates to fit within available GE slots and total GP bankroll.
    """
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
        if fill <= 0 or est_profit < MIN_EST_PROFIT:
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
) -> Tuple[List[DumpCandidate], CounterType[str]]:
    mapping_raw, data_5m, data_1h, data_24h, data_latest = fetch_snapshots(session)
    item_map = build_item_map(mapping_raw)
    now = time.time()

    funnel: CounterType[str] = Counter()
    candidates: List[DumpCandidate] = []

    for item_id, tick_5m in data_5m.items():
        funnel["scanned"] += 1
        if item_id not in item_map:
            funnel["rej: not in mapping / no limit / excluded"] += 1
            continue
        if item_id not in data_1h:
            funnel["rej: no 1h data"] += 1
            continue

        tick_1h = data_1h[item_id]
        tick_24h = data_24h.get(item_id, {})
        latest = data_latest.get(item_id, {})
        meta = item_map[item_id]
        buy_limit = meta["limit"]

        high_24h = tick_24h.get("avgHighPrice") or 0
        buy_24h = tick_24h.get("highPriceVolume") or 0

        avg_low_5m = tick_5m.get("avgLowPrice")
        sell_5m = tick_5m.get("lowPriceVolume") or 0
        buy_5m = tick_5m.get("highPriceVolume") or 0

        if not avg_low_5m or avg_low_5m <= 0 or sell_5m <= 0:
            funnel["rej: no 5m instasell activity"] += 1
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

        p_base = get_pre_dump_baseline(high_1h, low_1h, buy_1h, sell_1h, avg_low_5m, sell_5m)
        if p_base <= 0 or avg_low_5m >= p_base:
            funnel["rej: 5m price not below baseline"] += 1
            continue

        latest_low = fresh_price(latest.get("low"), latest.get("lowTime"), now)
        latest_high = fresh_price(latest.get("high"), latest.get("highTime"), now)
        if latest.get("low") and latest_low is None:
            funnel["info: stale /latest low ignored"] += 1
        if latest.get("high") and latest_high is None:
            funnel["info: stale /latest high ignored"] += 1

        if latest_low and latest_low > (avg_low_5m + 0.5 * (p_base - avg_low_5m)):
            funnel["rej: dump already recovered on /latest"] += 1
            continue

        p_dump = min(avg_low_5m, latest_low) if latest_low else avg_low_5m

        if high_24h > 0 and buy_24h >= 20 and p_dump > int(high_24h * 1.05):
            funnel["rej: dump price exceeds 24h high (unwinding pump)"] += 1
            continue

        if low_1h > 0 and sell_1h > sell_5m:
            support_drop_pct = ((low_1h - p_dump) / low_1h) * 100.0
            if support_drop_pct < MIN_SUPPORT_BREAK_PCT:
                funnel["rej: no 1h support break"] += 1
                continue

        raw_drop_pct = ((p_base - p_dump) / p_base) * 100.0
        if raw_drop_pct < MIN_RAW_DROP_PCT:
            funnel["rej: raw drop too small"] += 1
            continue

        raw_exit = int(round(p_dump + RETRACEMENT_TARGET * (p_base - p_dump)))
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
        if m_score < MIN_MARGIN_PILLAR:
            funnel["rej: margin pillar below minimum"] += 1
            continue

        l_sat = sell_5m / buy_limit
        if l_sat < 0.15:
            s_score = 15.0 + (20.0 * (l_sat / 0.15))
        elif l_sat <= 2.0:
            s_score = 35.0
        else:
            s_score = 35.0 * math.exp(-0.35 * (l_sat - 2.0))

        t_clear = sell_5m / max(1, buy_1h)
        b_live = buy_5m / (sell_5m + buy_5m)
        d_score = (20.0 / (1.0 + 1.5 * t_clear)) + (5.0 * b_live)
        if d_score < MIN_DEMAND_PILLAR:
            funnel["rej: demand pillar below minimum"] += 1
            continue

        highalch = meta.get("highalch", 0)
        alch_net = max(0, highalch - 100)
        alch_bonus = 10.0 if (alch_net > 0 and p_dump <= int(round(alch_net * 1.01))) else 0.0

        dvs = m_score + s_score + d_score + alch_bonus
        if dvs < MIN_DVS_SCORE:
            funnel["rej: DVS below minimum"] += 1
            continue

        half_hour_buyer_cap = max(1, int(math.floor(0.5 * buy_1h)))
        est_fill = min(buy_limit, sell_5m, half_hour_buyer_cap)
        slot_cap_gp = min(MAX_SLOT_CAPITAL, bankroll) if (MAX_SLOT_CAPITAL and bankroll) else (bankroll or MAX_SLOT_CAPITAL)
        if slot_cap_gp and p_dump > 0:
            capital_cap = max(0, slot_cap_gp // p_dump)
            est_fill = min(est_fill, capital_cap)
        if est_fill <= 0:
            funnel["rej: zero est fill (capital cap)"] += 1
            continue

        est_profit = unit_profit * est_fill
        if est_profit < MIN_EST_PROFIT:
            funnel["rej: est profit below minimum"] += 1
            continue

        status = "STRONG BUY" if (dvs >= 75.0 and d_score >= 8.0) else "CAUTION / BUY"
        funnel["passed primary filters"] += 1
        candidates.append(
            DumpCandidate(
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
                status=status,
                p_base=p_base,
                raw_drop_pct=raw_drop_pct,
                alch_bonus=alch_bonus,
            )
        )

    candidates.sort(key=lambda c: (c.dvs, c.est_profit), reverse=True)
    desired_count = min(TOP_N_RESULTS, max_slots) if max_slots else TOP_N_RESULTS
    candidates = apply_trend_filter(
        session, candidates, funnel, target_count=desired_count, enabled=knife_check_enabled
    )
    final = candidates[:TOP_N_RESULTS]
    final = allocate_portfolio(final, max_slots=max_slots, bankroll=bankroll, funnel=funnel)
    funnel["accepted"] = len(final)
    return final, funnel


# =====================================================================
# OUTPUT & ALERTS
# =====================================================================
def copy_to_clipboard(text: str) -> bool:
    """Copies text to the system clipboard across Windows, macOS, and Linux."""
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
    """Posts a Discord-compatible webhook embed for STRONG BUY candidates."""
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


def print_report(candidates: List[DumpCandidate], new_ids: Optional[Set[int]] = None) -> None:
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
        print("No qualifying dumps found on this 5-minute tick.".center(width))
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
    """
    Appends accepted candidates to a CSV for later backtesting/calibration.
    Skips candidates already logged in the current 5-minute bucket.
    """
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
    """Runs one scan + report. Returns candidates, or None if the scan failed."""
    knife_check = KNIFE_CHECK_ENABLED and not args.no_knife_check
    try:
        candidates, funnel = scan_dumps(
            session,
            knife_check_enabled=knife_check,
            max_slots=args.slots,
            bankroll=args.bankroll,
        )
    except requests.RequestException as exc:
        print(f"Network error while querying the OSRS Wiki API: {exc}", file=sys.stderr)
        return None

    current_ids = {c.item_id for c in candidates}
    new_ids = (current_ids - previous_ids) if previous_ids is not None else None

    print_report(candidates, new_ids)
    if not args.no_log:
        logged = log_candidates(candidates)
        if logged:
            print(f"Logged candidate(s) to {logged}")

    if args.clip and candidates:
        if copy_to_clipboard(candidates[0].name):
            print(f"Copied '{candidates[0].name}' to clipboard.")
        else:
            print("Warning: no supported clipboard utility found.", file=sys.stderr)

    if args.debug:
        print_funnel(funnel)
    elif not candidates:
        print("Tip: run with --debug to see which filters rejected the most items.")

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
'''

BACKTEST_CODE = r'''"""
DVS Backtester
==============
Reads the candidate log written by "dump DVS scanner.py" (dvs_candidate_log.csv),
pulls the 5-minute price history that followed each signal from the OSRS Wiki
/timeseries endpoint, and reports how the signals actually performed (fill rate,
target hit rate, drawdown, realised profit) plus a sweep of alternative
RETRACEMENT_TARGET values.

Usage:
    python dvs_backtest.py                       # default log next to this script, 30m + 60m horizons
    python dvs_backtest.py --horizons 15,30,60   # custom horizons in minutes
    python dvs_backtest.py --volume-fills        # scale fills by actual 5m lowPriceVolume
    python dvs_backtest.py --out results.csv     # also export per-signal outcomes
"""
import argparse
import bisect
import csv
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
HEADERS = {"User-Agent": "DVS-Backtester/1.1 (Custom GE Market Flipping Tool)"}
HTTP_TIMEOUT_SECONDS = 15
BUCKET_SECONDS = 300
COMPLETION_GRACE_SECONDS = 2 * BUCKET_SECONDS

TARGET_SWEEP = (0.25, 0.40, 0.50, 0.60, 0.75, 0.90, 1.00)
DVS_BANDS = (
    (0.0, 65.0, "DVS <65"),
    (65.0, 75.0, "DVS 65-75"),
    (75.0, 85.0, "DVS 75-85"),
    (85.0, 1e9, "DVS 85+"),
)

DEFAULT_LOG_PATH = Path(__file__).resolve().parent / "dvs_candidate_log.csv"
TIMESERIES_CACHE_DIR = Path(__file__).resolve().parent / "timeseries_cache"

REQUIRED_COLUMNS = (
    "timestamp_unix",
    "item_id",
    "name",
    "p_dump",
    "p_exit",
    "p_base",
    "est_fill",
    "dvs",
    "status",
)


@dataclass
class Signal:
    timestamp: int
    item_id: int
    name: str
    p_dump: int
    p_exit: int
    p_base: int
    est_fill: int
    dvs: float
    status: str


@dataclass
class Outcome:
    timestamp: int
    item_id: int
    name: str
    horizon_min: int
    dvs: float
    status: str
    filled: bool
    filled_qty: int
    hit_target: bool
    max_retrace: float
    drawdown_pct: float
    realized_roi_pct: float
    realized_profit: int
    fill_minutes: Optional[int] = None
    hold_minutes: Optional[int] = None


def calculate_ge_tax(sell_price: int) -> int:
    if sell_price < 50:
        return 0
    return min(int(math.floor(sell_price * 0.02)), 5_000_000)


def load_signals(path: Path) -> Tuple[List[Signal], int]:
    if not path.exists():
        raise FileNotFoundError(f"Candidate log not found: {path}")
    signals: List[Signal] = []
    seen = set()
    skipped = 0
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Log is missing required columns: {', '.join(missing)}")
        for row in reader:
            try:
                sig = Signal(
                    timestamp=int(row["timestamp_unix"]),
                    item_id=int(row["item_id"]),
                    name=row["name"],
                    p_dump=int(float(row["p_dump"])),
                    p_exit=int(float(row["p_exit"])),
                    p_base=int(float(row["p_base"])),
                    est_fill=int(float(row["est_fill"])),
                    dvs=float(row["dvs"]),
                    status=row["status"],
                )
            except (TypeError, ValueError):
                skipped += 1
                continue
            if sig.p_dump <= 0 or sig.p_base <= sig.p_dump or sig.est_fill <= 0:
                skipped += 1
                continue
            bucket_key = (sig.timestamp // BUCKET_SECONDS, sig.item_id)
            if bucket_key in seen:
                continue
            seen.add(bucket_key)
            signals.append(sig)
    return signals, skipped


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


def fetch_series(
    session: requests.Session, item_id: int, min_timestamp: Optional[int] = None
) -> Optional[List[Dict]]:
    cache_path = TIMESERIES_CACHE_DIR / f"{item_id}.json"
    now = time.time()
    if cache_path.exists():
        try:
            mtime = cache_path.stat().st_mtime
            with cache_path.open("r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached and isinstance(cached, list):
                newest = cached[-1].get("timestamp", 0)
                if (min_timestamp and newest >= min_timestamp) or (now - mtime < BUCKET_SECONDS):
                    return cached
        except (OSError, ValueError):
            pass

    try:
        resp = session.get(
            f"{API_BASE}/timeseries",
            params={"timestep": "5m", "id": item_id},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except (requests.RequestException, ValueError):
        return None

    series = sorted((b for b in data if b.get("timestamp")), key=lambda b: b["timestamp"])
    if series:
        try:
            TIMESERIES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(series, fh)
            tmp_path.replace(cache_path)
        except OSError:
            pass
    return series


def fetch_all_series(
    session: requests.Session, item_ids: Sequence[int], min_timestamp: Optional[int] = None
) -> Dict[int, Optional[List[Dict]]]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: fetch_series(session, i, min_timestamp), item_ids))
    return dict(zip(item_ids, results))


def target_price(
    sig: Signal, retrace: Optional[float], logged_target_retrace: Optional[float] = None
) -> int:
    if retrace is None:
        return sig.p_exit
    raw = int(round(sig.p_dump + retrace * (sig.p_base - sig.p_dump)))
    if logged_target_retrace is not None:
        expected_uncapped = int(round(sig.p_dump + logged_target_retrace * (sig.p_base - sig.p_dump)))
        if sig.p_exit < expected_uncapped:
            return min(raw, sig.p_exit)
    return raw


def evaluate_signal(
    sig: Signal,
    series: List[Dict],
    timestamps: List[int],
    horizon_min: int,
    retrace: Optional[float] = None,
    logged_target_retrace: Optional[float] = None,
    scratch_mode: str = "mid",
    volume_fills: bool = False,
) -> Optional[Outcome]:
    start = (sig.timestamp // BUCKET_SECONDS) * BUCKET_SECONDS
    end = start + horizon_min * 60
    left = bisect.bisect_left(timestamps, start)
    right = bisect.bisect_left(timestamps, end)
    window = series[left:right]
    if not window:
        return None

    target = target_price(sig, retrace, logged_target_retrace)
    fill_idx: Optional[int] = None
    accumulated_vol = 0
    for i, bucket in enumerate(window):
        low = bucket.get("avgLowPrice")
        if low and low <= sig.p_dump:
            if fill_idx is None:
                fill_idx = i
            accumulated_vol += int(bucket.get("lowPriceVolume") or 0)
            if not volume_fills or accumulated_vol >= sig.est_fill:
                break

    def outcome(
        filled: bool,
        filled_qty: int,
        hit: bool,
        max_retrace: float,
        dd: float,
        net: int,
        fill_min: Optional[int] = None,
        hold_min: Optional[int] = None,
    ) -> Outcome:
        roi = (net / sig.p_dump) * 100.0 if filled else 0.0
        return Outcome(
            timestamp=sig.timestamp,
            item_id=sig.item_id,
            name=sig.name,
            horizon_min=horizon_min,
            dvs=sig.dvs,
            status=sig.status,
            filled=filled,
            filled_qty=filled_qty,
            hit_target=hit,
            max_retrace=max_retrace,
            drawdown_pct=dd,
            realized_roi_pct=roi,
            realized_profit=int(net * filled_qty) if filled else 0,
            fill_minutes=fill_min,
            hold_minutes=hold_min,
        )

    if fill_idx is None:
        return outcome(False, 0, False, 0.0, 0.0, 0)

    filled_qty = min(sig.est_fill, max(1, accumulated_vol)) if volume_fills else sig.est_fill
    fill_minutes = max(0, (window[fill_idx]["timestamp"] - start) // 60)
    held = window[fill_idx:]
    highs = [b["avgHighPrice"] for b in held if b.get("avgHighPrice")]
    lows = [b["avgLowPrice"] for b in held if b.get("avgLowPrice")]
    spread = sig.p_base - sig.p_dump
    max_retrace = ((max(highs) - sig.p_dump) / spread) if highs else 0.0
    drawdown = max(0.0, ((sig.p_dump - min(lows)) / sig.p_dump) * 100.0) if lows else 0.0

    hit_offset: Optional[int] = None
    for j, b in enumerate(held[1:], start=1):
        h = b.get("avgHighPrice")
        if h and h >= target:
            hit_offset = j
            break

    hit = hit_offset is not None
    hold_minutes = (
        (held[hit_offset]["timestamp"] - held[0]["timestamp"]) // 60
        if hit and hit_offset is not None
        else None
    )

    if hit:
        exit_price = target
    else:
        close_bucket = held[-1]
        hi = close_bucket.get("avgHighPrice")
        lo = close_bucket.get("avgLowPrice")
        if scratch_mode == "low":
            exit_price = lo or hi or sig.p_dump
        elif scratch_mode == "high":
            exit_price = hi or lo or sig.p_dump
        else:
            if hi and lo:
                exit_price = int(round(0.5 * (hi + lo)))
            else:
                exit_price = hi or lo or sig.p_dump

    net = exit_price - calculate_ge_tax(exit_price) - sig.p_dump
    return outcome(True, filled_qty, hit, max_retrace, drawdown, net, fill_minutes, hold_minutes)


def score_signals(
    signals: List[Signal],
    series_by_item: Dict[int, Optional[List[Dict]]],
    timestamps_by_item: Dict[int, List[int]],
    horizon_min: int,
    now: float,
    retrace: Optional[float] = None,
    logged_target_retrace: Optional[float] = None,
    scratch_mode: str = "mid",
    volume_fills: bool = False,
) -> Tuple[List[Outcome], Dict[str, int]]:
    outcomes: List[Outcome] = []
    excluded: Dict[str, int] = defaultdict(int)
    for sig in signals:
        start_bucket = (sig.timestamp // BUCKET_SECONDS) * BUCKET_SECONDS
        if now < start_bucket + horizon_min * 60 + COMPLETION_GRACE_SECONDS:
            excluded["horizon not yet complete"] += 1
            continue
        series = series_by_item.get(sig.item_id)
        timestamps = timestamps_by_item.get(sig.item_id, [])
        if not series or not timestamps:
            excluded["price history unavailable"] += 1
            continue
        if timestamps[0] > start_bucket:
            excluded["older than available history"] += 1
            continue
        result = evaluate_signal(
            sig,
            series,
            timestamps,
            horizon_min,
            retrace,
            logged_target_retrace=logged_target_retrace,
            scratch_mode=scratch_mode,
            volume_fills=volume_fills,
        )
        if result is None:
            excluded["no buckets in window"] += 1
            continue
        outcomes.append(result)
    return outcomes, dict(excluded)


def summarize(outcomes: List[Outcome]) -> Dict[str, float]:
    n = len(outcomes)
    filled = [o for o in outcomes if o.filled]
    hits = [o for o in filled if o.hit_target]
    nf = len(filled)
    fill_times = [o.fill_minutes for o in filled if o.fill_minutes is not None]
    hold_times = [o.hold_minutes for o in hits if o.hold_minutes is not None]
    return {
        "signals": n,
        "fill_rate": (nf / n * 100.0) if n else 0.0,
        "hit_rate": (len(hits) / nf * 100.0) if nf else 0.0,
        "avg_max_retrace": (statistics.mean(o.max_retrace for o in filled) * 100.0) if nf else 0.0,
        "median_max_retrace": (statistics.median(o.max_retrace for o in filled) * 100.0) if nf else 0.0,
        "avg_drawdown": statistics.mean(o.drawdown_pct for o in filled) if nf else 0.0,
        "avg_roi": statistics.mean(o.realized_roi_pct for o in filled) if nf else 0.0,
        "win_rate": (sum(1 for o in filled if o.realized_profit > 0) / nf * 100.0) if nf else 0.0,
        "total_profit": sum(o.realized_profit for o in filled),
        "median_fill_min": statistics.median(fill_times) if fill_times else 0.0,
        "median_hold_min": statistics.median(hold_times) if hold_times else 0.0,
    }


def print_summary_row(label: str, stats: Dict[str, float]) -> None:
    print(
        f"{label:<14} | {int(stats['signals']):>7} | {stats['fill_rate']:>6.1f}% | "
        f"{stats['hit_rate']:>6.1f}% | {stats['win_rate']:>6.1f}% | {stats['avg_max_retrace']:>8.1f}% | "
        f"{stats['avg_drawdown']:>7.2f}% | {stats['avg_roi']:>+7.2f}% | {int(stats['total_profit']):>14,}"
    )


SUMMARY_HEADER = (
    f"{'Group':<14} | {'Signals':>7} | {'Fill':>7} | {'Hit':>7} | {'Win':>7} | "
    f"{'AvgRetr':>9} | {'AvgDD':>8} | {'AvgROI':>8} | {'Total profit':>14}"
)


def print_horizon_report(horizon_min: int, outcomes: List[Outcome], excluded: Dict[str, int]) -> None:
    print("\n" + "=" * 112)
    print(f"HORIZON {horizon_min} MIN".center(112))
    print("=" * 112)
    if excluded:
        print("Excluded: " + ", ".join(f"{k}={v}" for k, v in sorted(excluded.items())))
    if not outcomes:
        print("No scoreable signals for this horizon yet.")
        return

    print(SUMMARY_HEADER)
    print("-" * len(SUMMARY_HEADER))
    print_summary_row("ALL", summarize(outcomes))
    for status in sorted({o.status for o in outcomes}):
        print_summary_row(status[:14], summarize([o for o in outcomes if o.status == status]))
    for lo, hi, label in DVS_BANDS:
        subset = [o for o in outcomes if lo <= o.dvs < hi]
        if subset:
            print_summary_row(label, summarize(subset))
    print(
        "Fill = bid at dump price filled | Hit = logged exit reached after fill (of filled) | "
        "Win = profitable after tax (of filled)"
    )
    all_summary = summarize(outcomes)
    if any(o.filled for o in outcomes):
        print(
            f"Velocity: Median fill = {all_summary['median_fill_min']:.0f}m | "
            f"Median hold to target = {all_summary['median_hold_min']:.0f}m"
        )


def print_target_sweep(
    signals: List[Signal],
    series_by_item: Dict[int, Optional[List[Dict]]],
    timestamps_by_item: Dict[int, List[int]],
    horizon_min: int,
    now: float,
    current_target: Optional[float],
    scratch_mode: str = "mid",
    volume_fills: bool = False,
) -> None:
    print(f"\nRETRACEMENT_TARGET sweep @ {horizon_min} min (post-tax, filled signals only)")
    print("-" * 64)
    print(f"{'Target':>7} | {'Signals':>7} | {'Hit':>7} | {'AvgROI':>8} | {'Total profit':>14}")
    best: Optional[Tuple[float, float]] = None
    for retrace in TARGET_SWEEP:
        outcomes, _ = score_signals(
            signals,
            series_by_item,
            timestamps_by_item,
            horizon_min,
            now,
            retrace,
            logged_target_retrace=current_target,
            scratch_mode=scratch_mode,
            volume_fills=volume_fills,
        )
        filled = [o for o in outcomes if o.filled]
        if not filled:
            continue
        hit_rate = sum(1 for o in filled if o.hit_target) / len(filled) * 100.0
        avg_roi = statistics.mean(o.realized_roi_pct for o in filled)
        total = sum(o.realized_profit for o in filled)
        marker = "  <- current" if current_target is not None and abs(retrace - current_target) < 1e-9 else ""
        print(f"{retrace:>7.2f} | {len(filled):>7} | {hit_rate:>6.1f}% | {avg_roi:>+7.2f}% | {total:>14,}{marker}")
        if best is None or total > best[1]:
            best = (retrace, total)
    if best:
        print(f"Highest total profit in this sample: target {best[0]:.2f}")


def export_outcomes(path: Path, outcomes: List[Outcome]) -> None:
    if not outcomes:
        return
    field_names = list(asdict(outcomes[0]).keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=field_names)
        writer.writeheader()
        for o in outcomes:
            writer.writerow(asdict(o))
    print(f"\nPer-signal outcomes written to {path}")


def parse_horizons(raw: str) -> List[int]:
    try:
        horizons = sorted({int(part) for part in raw.split(",") if part.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError("horizons must be comma-separated integers (minutes)")
    if not horizons or any(h <= 0 for h in horizons):
        raise argparse.ArgumentTypeError("horizons must be positive integers (minutes)")
    return horizons


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest logged OSRS DVS dump signals")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH, help="Path to dvs_candidate_log.csv")
    parser.add_argument("--horizons", type=parse_horizons, default=[30, 60], help="Minutes, comma-separated")
    parser.add_argument(
        "--target", type=float, default=0.75, help="Your current RETRACEMENT_TARGET (for sweep marker)"
    )
    parser.add_argument("--out", type=Path, default=None, help="Write per-signal outcomes across all horizons to CSV")
    parser.add_argument(
        "--scratch-mode",
        choices=["high", "mid", "low"],
        default="mid",
        help="Unhit exit pricing: high (instabuy), mid (spread midpoint), low (instasell)",
    )
    parser.add_argument(
        "--volume-fills",
        action="store_true",
        help="Cap filled quantity by actual 5m lowPriceVolume traded at or below p_dump",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        signals, skipped = load_signals(args.log)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not signals:
        print(f"No usable signals in {args.log}. Run the scanner (without --no-log) first.")
        return 0

    first_ts = min(s.timestamp for s in signals)
    last_ts = max(s.timestamp for s in signals)
    print(f"Loaded {len(signals)} unique 5m signals ({skipped} malformed rows skipped) from {args.log}")
    print(
        f"Signal span: {time.strftime('%Y-%m-%d %H:%M', time.localtime(first_ts))} -> "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last_ts))}"
    )

    now = time.time()
    session = build_session()
    try:
        item_ids = sorted({s.item_id for s in signals})
        min_required_ts = max(s.timestamp for s in signals) + max(args.horizons) * 60
        print(f"Fetching 5m price history for {len(item_ids)} item(s)...")
        series_by_item = fetch_all_series(session, item_ids, min_required_ts)
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130
    finally:
        session.close()

    timestamps_by_item: Dict[int, List[int]] = {
        item_id: [b["timestamp"] for b in series]
        for item_id, series in series_by_item.items()
        if series
    }

    all_outcomes: List[Outcome] = []
    for horizon in args.horizons:
        outcomes, excluded = score_signals(
            signals,
            series_by_item,
            timestamps_by_item,
            horizon,
            now,
            logged_target_retrace=args.target,
            scratch_mode=args.scratch_mode,
            volume_fills=args.volume_fills,
        )
        print_horizon_report(horizon, outcomes, excluded)
        if outcomes:
            print_target_sweep(
                signals,
                series_by_item,
                timestamps_by_item,
                horizon,
                now,
                args.target,
                scratch_mode=args.scratch_mode,
                volume_fills=args.volume_fills,
            )
            all_outcomes.extend(outcomes)

    if args.out and all_outcomes:
        export_outcomes(args.out, all_outcomes)

    print("\nNote: results reflect only the signals in your log; treat small samples (<30) as anecdotal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# Write the updated files to the root directory
(ROOT_DIR / "dump DVS scanner.py").write_text(SCANNER_CODE, encoding="utf-8")
(ROOT_DIR / "dvs_backtest.py").write_text(BACKTEST_CODE, encoding="utf-8")

# Create the zip archive of the root directory
zip_path = ROOT_DIR / ZIP_NAME
excluded_names = {ZIP_NAME, "bundle_project.py", "dump finder.zip"}

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for item in sorted(ROOT_DIR.rglob("*")):
        if "__pycache__" in item.parts or item.name.endswith(".pyc"):
            continue
        if item.name in excluded_names:
            continue
        if item.is_file():
            zf.write(item, arcname=item.relative_to(ROOT_DIR))
            print(f"Added: {item.relative_to(ROOT_DIR)}")

print(f"\nCreated archive: {zip_path}")