import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
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
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pylint: disable=broad-exception-caught
        pass

VERSION = "2.7-hybrid"
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

# Infrastructure & Hybrid Polling Parameters
MAX_LATEST_AGE_SECONDS = 600       # Ignore /latest high/low trades older than 10 minutes
LIVE_DUMP_MAX_AGE_SECONDS = 180    # Allow /latest low to trigger a live dump if < 3 minutes old
MAPPING_CACHE_TTL_SECONDS = 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 15
WATCH_BUCKET_SECONDS = 300         # The wiki /5m endpoint rolls over every 5 minutes (300s)
SLOW_TICK_BOUNDARY_DELAY = 12      # Wiki publishes 5m bucket ~12s after the boundary
DEFAULT_FAST_INTERVAL_SECONDS = 30 # Default interval for fast-polling /latest loop

# Potion Pattern for Decant Arbitrage Detection
POTION_PATTERN = re.compile(r"^(.*?)\s*\(([1-4])\)$", re.IGNORECASE)

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
    # Hybrid & Decant Extensions
    low_time: int = 0
    fail_reason: str = ""
    is_potion: bool = False
    potion_dose: Optional[int] = None
    decant_target_name: Optional[str] = None
    decant_target_price: int = 0
    decant_roi_pct: float = 0.0
    decant_profit: int = 0


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
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


# ---------------------------------------------------------------------
# Typed API client + transient-error logging for the OSRS GE endpoints.
# ---------------------------------------------------------------------
LEARNINGS_ERRORS_PATH = SCRIPT_DIR.parent / ".learnings" / "ERRORS.md"


class GEApiError(Exception):
    """Typed error raised by the OSRS GE API client. Carries enough context
    to distinguish a rate limit, a server outage, a malformed response, and
    a transport failure without re-parsing the original exception text."""

    def __init__(
        self,
        status: int,
        endpoint: str,
        message: str,
        body: Optional[object] = None,
        retry_after: Optional[int] = None,
        retryable: bool = False,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.message = message
        self.body = body
        self.retry_after = retry_after
        self.retryable = retryable
        self.__cause__ = cause

    def is_rate_limited(self) -> bool:
        return self.status == 429

    def is_server_error(self) -> bool:
        return 500 <= self.status < 600 or self.status == 0

    def is_client_error(self) -> bool:
        return 400 <= self.status < 500 and self.status != 429


def _safe_json(resp: requests.Response) -> Optional[object]:
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _parse_retry_after(resp: requests.Response) -> Optional[int]:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    try:
        # also accept an HTTP date
        dt = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S GMT")
        delay = int(dt.replace(tzinfo=timezone.utc).timestamp() - time.time())
        return max(0, delay)
    except ValueError:
        return None


_logged_transient_errors: Set[Tuple[str, str]] = set()
_error_seq: int = 0


def _log_transient_error(endpoint: str, kind: str, detail: str) -> None:
    """Append one ERROR- entry to .learnings/ERRORS.md on a given
    (endpoint, kind) combination. De-duplicates within the process so a watch
    loop does not flood the log with one row per poll."""
    global _error_seq  # pylint: disable=global-statement
    signature = (endpoint, kind)
    if signature in _logged_transient_errors:
        return
    _logged_transient_errors.add(signature)

    try:
        if not LEARNINGS_ERRORS_PATH.exists():
            LEARNINGS_ERRORS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _error_seq += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        entry_id = f"ERR-{date}-{_error_seq:03d}"
        # sanitize detail for markdown
        detail_clean = str(detail).replace("```", "'''")[:1000]
        entry = (
            f"\n## [{entry_id}] fetch_endpoint\n\n"
            f"**Logged**: {ts}\n"
            f"**Priority**: high\n"
            f"**Status**: pending\n"
            f"**Area**: backend\n\n"
            f"### Summary\n"
            f"Transient failure fetching `{endpoint}` ({kind})\n\n"
            f"### Error\n"
            f"```\n{detail_clean}\n```\n\n"
            f"### Context\n"
            f"- Operation: requests.get `{API_BASE}/{endpoint}`\n"
            f"- Endpoint: {endpoint}\n"
            f"- Failure kind: {kind}\n"
            f"- Reached .learnings logging path via the GE API hardening client\n\n"
            f"### Suggested Fix\n"
            f"- Retries exhausted before success; verify GE endpoint health and "
            f"treat cached mapping/timeframes as suspect if this recurs\n\n"
            f"### Metadata\n"
            f"- Reproducible: unknown\n"
            f"- Related Files: backend/dump DVS scanner.py\n"
            f"- See Also: LRN-20261015-001\n\n"
            f"---\n"
        )
        with LEARNINGS_ERRORS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError:
        # Logging must never break the scan itself.
        pass


def fetch_endpoint(
    session: requests.Session,
    endpoint: str,
    params: Optional[Dict] = None,
    max_retries: int = 3,
) -> Dict:
    """Fetch a JSON endpoint from the OSRS prices wiki, returning the parsed
    body. Wraps transport/server/rate-limit errors in :class:`GEApiError` and
    retries transient failures with exponential backoff. Delegates ultimate
    transport retries to the session's mounted urllib3 adapter as well."""
    url = f"{API_BASE}/{endpoint}"
    last_exc: Optional[GEApiError] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = GEApiError(
                status=0,
                endpoint=endpoint,
                message=f"connection failure: {exc}",
                retryable=True,
                cause=exc,
            )
            _log_transient_error(endpoint, "connection_failure", str(exc))
            if attempt < max_retries:
                time.sleep(min(2 ** (attempt - 1), 5))
                continue
            raise last_exc from exc

        body = _safe_json(resp)

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            delay = retry_after if retry_after is not None else min(2 ** (attempt - 1), 8)
            last_exc = GEApiError(
                status=429,
                endpoint=endpoint,
                message="rate limited (429)",
                body=body,
                retry_after=retry_after,
                retryable=True,
            )
            _log_transient_error(endpoint, "rate_limited_429", f"HTTP 429 (Retry-After={retry_after})")
            if attempt < max_retries:
                time.sleep(delay)
                continue
            raise last_exc

        if 500 <= resp.status_code < 600:
            last_exc = GEApiError(
                status=resp.status_code,
                endpoint=endpoint,
                message=f"server error HTTP {resp.status_code}",
                body=body,
                retryable=True,
            )
            _log_transient_error(endpoint, f"server_error_{resp.status_code}", f"HTTP {resp.status_code}")
            if attempt < max_retries:
                time.sleep(min(2 ** (attempt - 1), 5))
                continue
            raise last_exc

        if not resp.ok:
            # other 4xx: do not retry
            raise GEApiError(
                status=resp.status_code,
                endpoint=endpoint,
                message=f"HTTP error {resp.status_code}",
                body=body,
                retryable=False,
            )

        if not isinstance(body, (dict, list)):
            raise GEApiError(
                status=resp.status_code,
                endpoint=endpoint,
                message="empty/invalid JSON response",
                body=None,
                retryable=True,
            )

        return body

    if last_exc is not None:
        raise last_exc
    raise GEApiError(
        status=0,
        endpoint=endpoint,
        message="fetch failed without a captured error",
        retryable=False,
    )


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
# CORE LOGIC & RECONSTRUCTION
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

    if 0 < high_1h < prior_low:
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


def build_potion_registry(mapping_raw: List[Dict]) -> Dict[str, Dict[int, int]]:
    """
    Builds a registry mapping lower-cased potion base names to their dose variants:
    e.g. 'prayer potion' -> {1: 143, 2: 141, 3: 139, 4: 2434}
    """
    registry: Dict[str, Dict[int, int]] = {}
    for item in mapping_raw:
        item_id = item.get("id")
        name = item.get("name", "")
        if not item_id or not name:
            continue
        m = POTION_PATTERN.match(name.strip())
        if m:
            base_name = m.group(1).strip().lower()
            dose = int(m.group(2))
            if base_name not in registry:
                registry[base_name] = {}
            registry[base_name][dose] = int(item_id)
    return registry


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
    except (GEApiError, requests.RequestException, ValueError):
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


# =====================================================================
# TWO-SPEED HYBRID SCANNER ENGINE
# =====================================================================
class MarketScannerEngine:
    """
    Stateful engine maintaining the two-speed hybrid polling loop:
      - Slow Loop: Refreshes /5m, /1h, /24h, /mapping every 5 minutes (synced ~12s after GE boundary).
      - Fast Loop: Runs every 30-45s polling ONLY /latest, evaluating items with fresh trades,
        recalculating DVS instantly against the cached 5m baseline, and triggering knife checks
        strictly for threshold-crossing candidates.
    """

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or build_session()
        self.mapping_raw: List[Dict] = []
        self.item_map: Dict[str, Dict] = {}
        self.potion_registry: Dict[str, Dict[int, int]] = {}
        self.data_5m: Dict[str, Dict] = {}
        self.data_1h: Dict[str, Dict] = {}
        self.data_24h: Dict[str, Dict] = {}
        self.last_baseline_time: float = 0.0
        self.last_baseline_bucket: int = 0
        self.last_evaluated_low_times: Dict[str, int] = {}
        self.cached_candidates: List[DumpCandidate] = []
        self.cached_near_misses: List[DumpCandidate] = []
        self.cached_funnel: CounterType[str] = Counter()
        self._lock = threading.Lock()

    def is_baseline_stale(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if not self.data_5m or not self.data_1h or not self.item_map:
            return True
        current_bucket = int(now) // WATCH_BUCKET_SECONDS
        bucket_elapsed = now - (current_bucket * WATCH_BUCKET_SECONDS)
        # If we entered a new 5-minute bucket and reached the Wiki publishing delay (~12s)
        if current_bucket > self.last_baseline_bucket and bucket_elapsed >= SLOW_TICK_BOUNDARY_DELAY:
            return True
        # Or if baseline is older than 5 minutes
        if now - self.last_baseline_time >= WATCH_BUCKET_SECONDS:
            return True
        return False

    def refresh_baseline(self) -> None:
        """Slow Loop: Fetches /mapping, /5m, /1h, /24h concurrently and reconstructs cache."""
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_mapping = pool.submit(load_mapping, self.session)
            f_5m = pool.submit(fetch_endpoint, self.session, "5m")
            f_1h = pool.submit(fetch_endpoint, self.session, "1h")
            f_24h = pool.submit(fetch_endpoint, self.session, "24h")

            self.mapping_raw = f_mapping.result()
            self.data_5m = f_5m.result().get("data", {})
            self.data_1h = f_1h.result().get("data", {})
            self.data_24h = f_24h.result().get("data", {})

        self.item_map = build_item_map(self.mapping_raw)
        self.potion_registry = build_potion_registry(self.mapping_raw)
        now = time.time()
        self.last_baseline_time = now
        self.last_baseline_bucket = int(now) // WATCH_BUCKET_SECONDS
        self.last_evaluated_low_times.clear()

    def scan(
        self,
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
        filter_partial_potions: bool = False,
        decant_check: bool = True,
        force_slow: bool = False,
        fast_mode: bool = True,
    ) -> Tuple[List[DumpCandidate], List[DumpCandidate], CounterType[str], bool]:
        """
        Executes a scan pass.
        Returns: (candidates, near_misses, funnel, was_fast_scan)
        """
        now = time.time()
        was_fast = False

        if force_slow or self.is_baseline_stale(now) or not fast_mode:
            self.refresh_baseline()
            was_fast = False
        else:
            was_fast = True

        data_latest = fetch_endpoint(self.session, "latest").get("data", {})
        now = time.time()

        funnel: CounterType[str] = Counter()
        candidates: List[DumpCandidate] = []
        near_misses: List[DumpCandidate] = []

        all_item_ids = set(self.data_5m.keys()) | set(self.data_1h.keys())

        for item_id in all_item_ids:
            funnel["scanned"] += 1
            if item_id not in self.item_map:
                funnel["rej: not in mapping / no limit / excluded"] += 1
                continue
            if item_id not in self.data_1h:
                funnel["rej: no 1h data"] += 1
                continue

            meta = self.item_map[item_id]
            item_name = meta["name"]

            # Optional Partial-Dose Potion Check / Filtering
            m_pot = POTION_PATTERN.match(item_name)
            is_potion = bool(m_pot)
            potion_dose = int(m_pot.group(2)) if m_pot else None

            if filter_partial_potions and is_potion and potion_dose in (1, 2, 3):
                funnel["rej: partial-dose potion ((1)-(3))"] += 1
                continue

            tick_5m = self.data_5m.get(item_id, {})
            tick_1h = self.data_1h[item_id]
            tick_24h = self.data_24h.get(item_id, {})
            latest = data_latest.get(item_id, {})
            buy_limit = meta["limit"]

            low_time = latest.get("lowTime") or 0
            high_time = latest.get("highTime") or 0

            prev_active_ids = {c.item_id for c in self.cached_candidates} | {c.item_id for c in self.cached_near_misses}
            # In fast mode: only evaluate items with new trades OR currently active candidates/near-misses
            has_new_low = low_time > 0 and self.last_evaluated_low_times.get(item_id) != low_time
            if was_fast and (int(item_id) not in prev_active_ids) and not has_new_low:
                continue

            self.last_evaluated_low_times[item_id] = low_time

            high_24h = tick_24h.get("avgHighPrice") or 0
            buy_24h = tick_24h.get("highPriceVolume") or 0

            avg_low_5m = tick_5m.get("avgLowPrice") or 0
            sell_5m = tick_5m.get("lowPriceVolume") or 0
            buy_5m = tick_5m.get("highPriceVolume") or 0

            latest_low = fresh_price(latest.get("low"), low_time, now)
            latest_low_very_fresh = fresh_price(
                latest.get("low"), low_time, now, max_age=LIVE_DUMP_MAX_AGE_SECONDS
            )
            latest_high = fresh_price(latest.get("high"), high_time, now)

            # Allow item if it has 5m instasell volume OR a fresh live dump on /latest
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

            # Check if 5m dump already bounced on /latest
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

            # Optional Decant Arbitrage Analysis
            decant_target_name = None
            decant_target_price = 0
            decant_roi_pct = 0.0
            decant_profit = 0

            if decant_check and is_potion and potion_dose in (1, 2, 3):
                base_name = m_pot.group(1).strip().lower()
                four_id = self.potion_registry.get(base_name, {}).get(4)
                if four_id and str(four_id) in self.item_map:
                    target_meta = self.item_map[str(four_id)]
                    decant_target_name = target_meta["name"]
                    t_latest = data_latest.get(str(four_id), {})
                    t_5m = self.data_5m.get(str(four_id), {})
                    t_1h = self.data_1h.get(str(four_id), {})
                    p_exit_4 = (
                        t_latest.get("high")
                        or t_5m.get("avgHighPrice")
                        or t_1h.get("avgHighPrice")
                        or 0
                    )
                    if p_exit_4 > 0 and p_dump > 0:
                        decant_target_price = int(p_exit_4)
                        cost_per_4 = (p_dump / potion_dose) * 4.0
                        tax_4 = calculate_ge_tax(decant_target_price)
                        net_4 = decant_target_price - tax_4
                        profit_per_4 = net_4 - cost_per_4
                        if profit_per_4 > 0:
                            decant_roi_pct = (profit_per_4 / cost_per_4) * 100.0
                            equiv_4 = int((est_fill * potion_dose) // 4)
                            decant_profit = int(profit_per_4 * equiv_4)

            cand_obj = DumpCandidate(
                item_id=int(item_id),
                name=item_name,
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
                low_time=int(low_time or now),
                fail_reason="",
                is_potion=is_potion,
                potion_dose=potion_dose,
                decant_target_name=decant_target_name,
                decant_target_price=decant_target_price,
                decant_roi_pct=round(decant_roi_pct, 2),
                decant_profit=decant_profit,
            )

            # Strict Filter Evaluation with Specific Cell Failure Tagging
            if m_score < min_margin_pillar:
                funnel["rej: margin pillar below minimum"] += 1
                if est_profit >= 25_000 and dvs >= 40.0:
                    cand_obj.fail_reason = "MARGIN"
                    near_misses.append(cand_obj)
                continue

            if d_score < min_demand_pillar:
                funnel["rej: demand pillar below minimum"] += 1
                if est_profit >= 25_000 and dvs >= 40.0:
                    cand_obj.fail_reason = "DEMAND"
                    near_misses.append(cand_obj)
                continue

            if dvs < min_dvs_score:
                funnel["rej: DVS below minimum"] += 1
                if est_profit >= 25_000:
                    cand_obj.fail_reason = "DVS"
                    near_misses.append(cand_obj)
                continue

            if est_profit < min_est_profit:
                funnel["rej: est profit below minimum"] += 1
                if est_profit >= 25_000:
                    cand_obj.fail_reason = "PROFIT"
                    near_misses.append(cand_obj)
                continue

            status_str = "STRONG BUY" if (dvs >= 75.0 and d_score >= 8.0) else "CAUTION / BUY"
            if decant_profit >= 40_000 and decant_roi_pct >= 3.0:
                status_str += " [DECANT]"
            cand_obj.status = status_str
            funnel["passed primary filters"] += 1
            candidates.append(cand_obj)

        candidates.sort(key=lambda c: (c.dvs, c.est_profit), reverse=True)
        desired_count = min(TOP_N_RESULTS, max_slots) if max_slots else TOP_N_RESULTS
        candidates = apply_trend_filter(
            self.session, candidates, funnel, target_count=desired_count, enabled=knife_check_enabled
        )
        final = candidates[:TOP_N_RESULTS]
        final = allocate_portfolio(
            final, max_slots=max_slots, bankroll=bankroll, funnel=funnel, min_est_profit=min_est_profit
        )
        funnel["accepted"] = len(final)

        near_misses.sort(key=lambda c: (c.dvs, c.est_profit), reverse=True)
        trimmed_near_misses = near_misses[:NEAR_MISS_COUNT]

        if near_misses_out is not None:
            near_misses_out.extend(trimmed_near_misses)

        self.cached_candidates = final
        self.cached_near_misses = trimmed_near_misses
        self.cached_funnel = funnel

        return final, trimmed_near_misses, funnel, was_fast


# Shared default engine instance
DEFAULT_ENGINE: Optional[MarketScannerEngine] = None


def get_default_engine(session: Optional[requests.Session] = None) -> MarketScannerEngine:
    global DEFAULT_ENGINE  # pylint: disable=global-statement
    if DEFAULT_ENGINE is None or (session and DEFAULT_ENGINE.session != session):
        DEFAULT_ENGINE = MarketScannerEngine(session)
    return DEFAULT_ENGINE


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
    filter_partial_potions: bool = False,
    decant_check: bool = True,
    engine: Optional[MarketScannerEngine] = None,
    fast_mode: bool = False,
) -> Tuple[List[DumpCandidate], CounterType[str]]:
    """Backward-compatible entry point that delegates to the MarketScannerEngine."""
    eng = engine or get_default_engine(session)
    cands, _nms, funnel, _was_fast = eng.scan(
        knife_check_enabled=knife_check_enabled,
        max_slots=max_slots,
        bankroll=bankroll,
        near_misses_out=near_misses_out,
        min_dvs_score=min_dvs_score,
        min_margin_pillar=min_margin_pillar,
        min_demand_pillar=min_demand_pillar,
        min_est_profit=min_est_profit,
        max_slot_capital=max_slot_capital,
        min_raw_drop_pct=min_raw_drop_pct,
        min_support_break_pct=min_support_break_pct,
        min_item_price=min_item_price,
        retracement_target=retracement_target,
        max_recovery_fraction=max_recovery_fraction,
        filter_partial_potions=filter_partial_potions,
        decant_check=decant_check,
        force_slow=not fast_mode,
        fast_mode=fast_mode,
    )
    return cands, funnel


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
        _log_transient_error("webhook", "post_failed", str(exc))


def format_age(timestamp_unix: int, now: float) -> str:
    if not timestamp_unix or timestamp_unix <= 0:
        return "--"
    age = max(0, int(now - timestamp_unix))
    if age < 60:
        return f"{age}s ago"
    minutes = age // 60
    return f"{minutes}m ago"


def print_report(
    candidates: List[DumpCandidate],
    new_ids: Optional[Set[int]] = None,
    near_misses: Optional[List[DumpCandidate]] = None,
    was_fast: bool = False,
) -> None:
    now = time.time()
    width = 132
    print("=" * width)
    mode_tag = "[FAST /LATEST TICK]" if was_fast else "[5M GE BASELINE TICK]"
    print(f"OSRS DUMP VULNERABILITY SCORE (DVS) SCANNER v{VERSION} {mode_tag}".center(width))
    print("=" * width)
    header = (
        f"{'Item Name':<24} | {'Age':>7} | {'Dump Price':>10} | {'Exit (Cap)':>10} | "
        f"{'Net ROI':>7} | {'DVS':>5} | {'Pillars (M/S/D)':>15} | "
        f"{'Est Fill':>8} | {'Est Profit':>13} | {'Status'}"
    )
    print(header)
    print("-" * width)

    if not candidates:
        print("No strict qualifying dumps found on this tick.".center(width))
        if near_misses:
            print("-" * width)
            print("TOP NEAR-MISS WATCHLIST (highlighting specific failure metric):".center(width))
            print("-" * width)
            for c in near_misses:
                pillars = f"{c.m_score:.1f}/{c.s_score:.1f}/{c.d_score:.1f}"
                profit_str = f"{c.est_profit:,} gp"
                age_str = format_age(c.low_time, now)
                fail_tag = f" [FAIL: {c.fail_reason}]" if c.fail_reason else ""
                print(
                    f"{c.name[:24]:<24} | {age_str:>7} | {c.p_dump:>10,} | {c.p_exit:>10,} | "
                    f"{c.net_roi_pct:>6.2f}% | {c.dvs:>5.1f} | {pillars:>15} | "
                    f"{c.est_fill:>8,} | {profit_str:>13} | {c.status}{fail_tag}"
                )
    else:
        for c in candidates:
            pillars = f"{c.m_score:.1f}/{c.s_score:.1f}/{c.d_score:.1f}"
            profit_str = f"{c.est_profit:,} gp"
            age_str = format_age(c.low_time, now)
            marker = " *NEW*" if new_ids and c.item_id in new_ids else ""
            status_str = c.status
            if c.alch_bonus > 0 and "[ALCH]" not in status_str:
                status_str += " [ALCH]"
            if c.decant_profit > 0 and "[DECANT]" not in status_str:
                status_str += f" [DECANT +{c.decant_roi_pct:.1f}%]"
            print(
                f"{c.name[:24]:<24} | {age_str:>7} | {c.p_dump:>10,} | {c.p_exit:>10,} | "
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
# CLI & WATCH LOOPS
# =====================================================================
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSRS Dump Vulnerability Score (DVS) scanner")
    parser.add_argument(
        "--debug", action="store_true", help="Print the rejection funnel showing why items were filtered"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output the rejection funnel as JSON for machine parsing"
    )
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Use looser thresholds (DVS >= 45, Est Profit >= 40k, Drop >= 2.0%%) for slow market hours",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Enable fast-polling on /latest against cached 5m baseline",
    )
    parser.add_argument(
        "--fast-interval",
        type=int,
        default=DEFAULT_FAST_INTERVAL_SECONDS,
        help=f"Seconds between fast /latest polls in watch mode (default: {DEFAULT_FAST_INTERVAL_SECONDS}s)",
    )
    parser.add_argument(
        "--filter-potions",
        action="store_true",
        help="Filter out non-4-dose potions ((1), (2), (3) dose potions)",
    )
    parser.add_argument(
        "--no-decant",
        action="store_true",
        help="Disable decant arbitrage checks for partial-dose potions",
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
        help="Run continuous two-speed hybrid loop (fast ticks on /latest, slow ticks on 5m GE rollover)",
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


def seconds_until_next_bucket(now: Optional[float] = None) -> float:
    now = time.time() if now is None else now
    next_boundary = (math.floor(now / WATCH_BUCKET_SECONDS) + 1) * WATCH_BUCKET_SECONDS
    return max(1.0, next_boundary + SLOW_TICK_BOUNDARY_DELAY - now)


def run_scan_once(
    engine: MarketScannerEngine,
    args: argparse.Namespace,
    previous_ids: Optional[Set[int]] = None,
    fast_mode: bool = False,
) -> Optional[List[DumpCandidate]]:
    min_dvs = 45.0 if getattr(args, "relaxed", False) else MIN_DVS_SCORE
    min_profit = 40_000 if getattr(args, "relaxed", False) else MIN_EST_PROFIT
    min_drop = 2.0 if getattr(args, "relaxed", False) else MIN_RAW_DROP_PCT
    min_margin = 5.0 if getattr(args, "relaxed", False) else MIN_MARGIN_PILLAR
    min_price = getattr(args, "min_price", MIN_ITEM_PRICE)
    knife_check = KNIFE_CHECK_ENABLED and not args.no_knife_check
    filter_potions = getattr(args, "filter_potions", False)
    decant_check = not getattr(args, "no_decant", False)

    near_misses: List[DumpCandidate] = []
    try:
        candidates, near_misses, funnel, was_fast = engine.scan(
            knife_check_enabled=knife_check,
            max_slots=args.slots,
            bankroll=args.bankroll,
            near_misses_out=near_misses,
            min_dvs_score=min_dvs,
            min_margin_pillar=min_margin,
            min_demand_pillar=MIN_DEMAND_PILLAR,
            min_est_profit=min_profit,
            max_slot_capital=MAX_SLOT_CAPITAL,
            min_raw_drop_pct=min_drop,
            min_support_break_pct=MIN_SUPPORT_BREAK_PCT,
            min_item_price=min_price,
            filter_partial_potions=filter_potions,
            decant_check=decant_check,
            fast_mode=fast_mode,
        )
    except (requests.RequestException, GEApiError) as exc:
        print(f"Network error while querying the OSRS Wiki API: {exc}", file=sys.stderr)
        return None

    current_ids = {c.item_id for c in candidates}
    new_ids = (current_ids - previous_ids) if previous_ids is not None else None

    print_report(candidates, new_ids, near_misses=near_misses, was_fast=was_fast)
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
        if args.json:
            print(json.dumps(dict(funnel)))
        else:
            print_funnel(funnel)
    elif not candidates:
        print("Tip: run with --relaxed for looser filters, or --watch for the hybrid fast-polling loop.")

    if new_ids:
        strong_new = [c for c in candidates if c.item_id in new_ids and c.status.startswith("STRONG BUY")]
        names = ", ".join(c.name for c in candidates if c.item_id in new_ids)
        print(f"NEW since last scan: {names}")
        if strong_new:
            print("\a", end="", flush=True)
            if args.webhook_url:
                send_webhook_alert(engine.session, args.webhook_url, strong_new)
    elif previous_ids is None and args.webhook_url:
        strong_initial = [c for c in candidates if c.status.startswith("STRONG BUY")]
        if strong_initial:
            send_webhook_alert(engine.session, args.webhook_url, strong_initial)

    return candidates


def watch_loop(engine: MarketScannerEngine, args: argparse.Namespace) -> int:
    previous_ids: Optional[Set[int]] = None
    fast_interval = max(10, getattr(args, "fast_interval", DEFAULT_FAST_INTERVAL_SECONDS))
    print(
        f"Hybrid Watch Mode: Slow loop refreshes baseline every 5m tick (+{SLOW_TICK_BOUNDARY_DELAY}s delay).\n"
        f"Fast loop polls /latest every {fast_interval}s for real-time market panic. Press Ctrl+C to stop."
    )
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        is_stale = engine.is_baseline_stale()
        mode_str = "Slow Baseline Refresh" if is_stale else "Fast /latest Poll"
        print(f"\n[{stamp}] [{mode_str}] Scanning...")
        start_t = time.time()
        candidates = run_scan_once(engine, args, previous_ids, fast_mode=True)
        elapsed = time.time() - start_t
        if candidates is not None:
            previous_ids = {c.item_id for c in candidates}
        next_5m = int(seconds_until_next_bucket())
        print(f"Scan completed in {elapsed:.2f}s. Next fast poll in {fast_interval}s (Next 5m GE tick in {next_5m}s).")
        time.sleep(fast_interval)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    session = build_session()
    engine = MarketScannerEngine(session)
    try:
        if args.watch:
            return watch_loop(engine, args)
        result = run_scan_once(engine, args, fast_mode=args.fast)
        return 0 if result is not None else 1
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
