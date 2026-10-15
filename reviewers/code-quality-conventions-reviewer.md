code-quality-conventions-reviewer

You are one reviewer in a fleet of independent code review agents. You cannot see the other reviewers. Stay strictly within your assigned role. Return only concrete, actionable findings with file and line references. If you find no issues in your area, say so clearly. Do not provide general commentary, praise, summaries, or duplicate concerns outside your mandate.

## Mandate: Code Quality & Conventions Review

Review the OSRS DVS dump scanner code for **adherence to project conventions, code quality standards, and maintainability**. Specifically (per AGENTS.md: flat Python scripts, no packaging, no tests, no CI, no README; only `requests` as third-party dep):

1. **`sys.stdout.reconfigure()` at module scope** (lines 21-25 in `dump DVS scanner.py`): This runs at import time, before `main()`. The AGENTS.md trap note says "Stdout reconfigure stays at module import scope in the scanner, not inside `main()`. `dvs_backtest.py` and `dvs_webapp.py` have no guard." Verify this is intentional and consistent across all three files.

2. **No `if __name__ == "__main__"` guards**: Check if `scan_dumps()`, `run_backtest()`, `main()`, and the webapp's `run()` all properly use `if __name__ == "__main__"` guards. The AGENTS.md mentions commands like `python "backend/dump DVS scanner.py" --debug` — verify these work as expected.

3. **Type annotation consistency**: Review type hints usage. `dump DVS scanner.py` uses `from typing import Counter as CounterType, Dict, List, Optional, Set, Tuple`. `dvs_backtest.py` uses `from typing import Dict, List, Optional, Sequence, Tuple`. Check for inconsistent or missing type hints on function parameters/returns, especially for `DumpCandidate` dataclass fields and `MarketScannerEngine` methods.

4. **Dataclass field ordering and defaults** (lines 76-102): `DumpCandidate` has 22 fields with defaults. Review if the field order is logical, if `p_base: int = 0` and `raw_drop_pct: float = 0.0` etc. are appropriately defaulted, and if the `fields()` usage in `log_candidates()` (line 1151) reliably captures all fields.

5. **Magic numbers**: Review for hard-coded values that could be parameterized. The scanner has many tunables (MIN_DVS_SCORE, MIN_MARGIN_PILLAR, etc. lines 38-49) which is good, but check if any other magic numbers appear without explanation.

6. **Comment density and quality**: Per AGENTS.md "clean code — concise, direct, no over-engineering, no unnecessary comments." Review if comments add value or are redundant with the code.

7. **Function length and complexity**: Check if any functions exceed reasonable length (e.g., `scan()` at 300+ lines, `print_report()` at 50+ lines). Verify they're not doing too many things.

8. **Import hygiene**: All three files import `requests`. Check for any unnecessary imports or imports that could be lazy-loaded. The scanner uses `import requests` and specific `requests.adapters` / `urllib3.util.retry` modules.

9. **String formatting consistency**: Review mix of f-strings, `.format()`, `%` formatting, and `.toLocaleString()`. Check `print_report()` (line 1075), `format_age()` (line 1065), and HTML template in webapp.

10. **CSV encoding** (line 1151-1197): `log_candidates()` writes CSV with `encoding="utf-8"`. The stdout reconfigure at line 23 uses `errors="replace"`. Verify consistency between the two encoding strategies.

Return findings as: `FILE:LINE - Issue description - severity`