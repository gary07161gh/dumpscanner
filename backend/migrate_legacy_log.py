"""
Migrate the pre-refactor 18-column candidate log into a versioned, 26-column log
that dvs_backtest.py can read, without touching the live log.

Context
-------
A stale webapp process (launched from the repo root before the backend/
relocation) wrote signals to the repo-root dvs_candidate_log.csv using the
pre-v2.7 scanner. Those 82 rows use an 18-column schema; the current scanner
emits 26 columns. dvs_backtest.py requires 9 core columns and ignores the
rest, so the legacy rows are already backtest-compatible -- but they cannot be
appended into the live log, because doing so would either change the live
header (which makes log_candidates() archive the file) or silently mix
pre-v2.7 scores with v2.7-hybrid scores.

This script writes a SEPARATE file instead, and stamps every row with the
scanner version that produced it so backtest runs can be segmented by vintage.

Safety
------
- Reads the legacy log read-only. Never modifies it.
- Never writes to the live log. Refuses to run if the output path resolves to
  backend/dvs_candidate_log.csv -- changing the live header would trigger the
  self-archiving behaviour in log_candidates().
- Idempotent: re-running overwrites the output deterministically.

Usage
-----
    python backend/migrate_legacy_log.py
    python backend/migrate_legacy_log.py --version-tag 2.5-legacy
    python backend/migrate_legacy_log.py --out backend/dvs_candidate_log_legacy.csv

Then compare backtest runs across vintages:

    python backend/dvs_backtest.py --log backend/dvs_candidate_log_legacy.csv
"""

import argparse
import csv
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

LEGACY_LOG_PATH = PROJECT_ROOT / "dvs_candidate_log.csv"
LIVE_LOG_PATH = SCRIPT_DIR / "dvs_candidate_log.csv"
DEFAULT_OUT_PATH = SCRIPT_DIR / "dvs_candidate_log_legacy.csv"

# Exact header the current scanner writes. log_candidates() rebuilds this from
# the DumpCandidate dataclass, so any drift here means the live log will be
# archived on the next scan.
LIVE_HEADER = [
    "timestamp_utc", "timestamp_unix", "item_id", "name", "p_dump", "p_exit",
    "net_roi_pct", "dvs", "m_score", "s_score", "d_score", "est_fill",
    "est_profit", "status", "p_base", "raw_drop_pct", "trend_check",
    "alch_bonus", "low_time", "fail_reason", "is_potion", "potion_dose",
    "decant_target_name", "decant_target_price", "decant_roi_pct",
    "decant_profit",
]

# Columns the pre-v2.7 schema did not emit. Values must match the dataclass
# defaults in backend/dump DVS scanner.py, so a migrated row is
# indistinguishable from a row the current scanner would have written with
# those fields unset.
LEGACY_DEFAULTS = {
    "low_time": "0",
    "fail_reason": "",
    "is_potion": "False",
    "potion_dose": "",
    "decant_target_name": "",
    "decant_target_price": "0",
    "decant_roi_pct": "0.0",
    "decant_profit": "0",
}

VINTAGE_COLUMN = "scanner_version"
DEFAULT_VERSION_TAG = "2.5-legacy"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    p.add_argument("--legacy", type=Path, default=LEGACY_LOG_PATH,
                   help="Path to the pre-refactor 18-column log (read-only).")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH,
                   help="Output path for the versioned 26-column log.")
    p.add_argument("--version-tag", default=DEFAULT_VERSION_TAG,
                   help=f"Vintage stamp written to each row (default: {DEFAULT_VERSION_TAG}).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.out.resolve() == LIVE_LOG_PATH.resolve():
        print(f"Refusing to write to the live log: {LIVE_LOG_PATH}\n"
              "Changing its header would make log_candidates() archive it on the "
              "next scan. Write somewhere else and merge deliberately.", file=sys.stderr)
        return 1

    if not args.legacy.exists() or args.legacy.stat().st_size == 0:
        print(f"No legacy log at {args.legacy}", file=sys.stderr)
        return 1

    with args.legacy.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        legacy_header = list(reader.fieldnames or [])
        rows = list(reader)

    unknown = [c for c in legacy_header if c not in LIVE_HEADER]
    if unknown:
        print(f"Legacy log has columns the current scanner does not know: {unknown}\n"
              "Refusing to guess how to carry them forward.", file=sys.stderr)
        return 1

    out_header = LIVE_HEADER + [VINTAGE_COLUMN]
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=out_header, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            out = {col: row.get(col, "").strip() or LEGACY_DEFAULTS.get(col, "")
                   for col in LIVE_HEADER}
            out[VINTAGE_COLUMN] = args.version_tag
            writer.writerow(out)

    print(f"Migrated {len(rows)} rows: {args.legacy} -> {args.out}")
    print(f"Vintage stamp: {VINTAGE_COLUMN}={args.version_tag}")
    print(f"Columns: {len(legacy_header)} -> {len(out_header)}")
    print(f"Backtest it with:\n  python backend/dvs_backtest.py --log \"{args.out}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())