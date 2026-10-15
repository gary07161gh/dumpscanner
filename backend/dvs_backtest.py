"""
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
    """Calculates exact OSRS GE 2% tax (exempt under 50 gp, capped at 5M gp)."""
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
        if best is None:
            best = (retrace, total)
        elif total > best[1]:
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
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated integers (minutes)") from exc
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


def run_backtest(
    log_path: Path = DEFAULT_LOG_PATH,
    horizons: Sequence[int] = (30, 60),
    target: float = 0.75,
    scratch_mode: str = "mid",
    volume_fills: bool = False,
) -> Dict:
    signals, skipped = load_signals(log_path)
    if not signals:
        return {
            "signals_count": 0,
            "skipped": skipped,
            "horizons": {},
            "all_outcomes": [],
        }

    now = time.time()
    session = build_session()
    try:
        item_ids = sorted({s.item_id for s in signals})
        min_required_ts = max(s.timestamp for s in signals) + max(horizons) * 60
        series_by_item = fetch_all_series(session, item_ids, min_required_ts)
    finally:
        session.close()

    timestamps_by_item: Dict[int, List[int]] = {
        item_id: [b["timestamp"] for b in series]
        for item_id, series in series_by_item.items()
        if series
    }

    horizons_data = {}
    all_outcomes: List[Outcome] = []

    for horizon in horizons:
        outcomes, excluded = score_signals(
            signals,
            series_by_item,
            timestamps_by_item,
            horizon,
            now,
            logged_target_retrace=target,
            scratch_mode=scratch_mode,
            volume_fills=volume_fills,
        )

        sweeps = []
        for retrace in TARGET_SWEEP:
            sw_outcomes, _ = score_signals(
                signals,
                series_by_item,
                timestamps_by_item,
                horizon,
                now,
                retrace,
                logged_target_retrace=target,
                scratch_mode=scratch_mode,
                volume_fills=volume_fills,
            )
            sw_filled = [o for o in sw_outcomes if o.filled]
            if sw_filled:
                hit_rate = sum(1 for o in sw_filled if o.hit_target) / len(sw_filled) * 100.0
                avg_roi = statistics.mean(o.realized_roi_pct for o in sw_filled)
                total = sum(o.realized_profit for o in sw_filled)
                sweeps.append({
                    "target": retrace,
                    "signals": len(sw_filled),
                    "hit_rate": hit_rate,
                    "avg_roi": avg_roi,
                    "total_profit": total,
                    "is_current": abs(retrace - target) < 1e-9,
                })

        by_status = {}
        for status in sorted({o.status for o in outcomes}):
            by_status[status] = summarize([o for o in outcomes if o.status == status])

        by_dvs = {}
        for lo, hi, label in DVS_BANDS:
            subset = [o for o in outcomes if lo <= o.dvs < hi]
            if subset:
                by_dvs[label] = summarize(subset)

        horizons_data[str(horizon)] = {
            "summary": summarize(outcomes),
            "by_status": by_status,
            "by_dvs": by_dvs,
            "excluded": excluded,
            "sweeps": sweeps,
            "outcomes": [asdict(o) for o in outcomes],
        }
        all_outcomes.extend(outcomes)

    return {
        "signals_count": len(signals),
        "skipped": skipped,
        "first_timestamp": min(s.timestamp for s in signals),
        "last_timestamp": max(s.timestamp for s in signals),
        "horizons": horizons_data,
        "all_outcomes": [asdict(o) for o in all_outcomes],
    }


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
