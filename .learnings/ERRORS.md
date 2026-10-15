# Errors Log

This file tracks technical exceptions, unexpected CLI failures, and tool errors encountered during execution.

---

## [ERR-20261015-001] windows_console_cp1252_encoding

**Logged**: 2026-10-15T13:32:32Z  
**Priority**: high  
**Status**: resolved  
**Area**: backend  

### Summary
`UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f504'` when printing emojis in standard Windows cmd/PowerShell terminal.

### Error
```text
Traceback (most recent call last):
  File "C:\Users\Gary0\Downloads\dump finder\backend\dump DVS scanner.py", line 873, in print_report
    print(f"OSRS DUMP VULNERABILITY SCORE (DVS) SCANNER v{VERSION} [{mode_tag}]".center(width))
  File "C:\Users\Gary0\AppData\Local\Python\pythoncore-3.14-64\Lib\encodings\cp1252.py", line 19, in encode
    return codecs.charmap_encode(input,self.errors,encoding_table)[0]
UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f504' in position 83: character maps to <undefined>
```

### Context
- Execution of `dump DVS scanner.py` with terminal header containing emojis (`⚡`, `🔄`) on Windows PowerShell.
- Default Windows console encoding defaults to `cp1252`.

### Suggested Fix
1. Explicitly reconfigure stdout encoding at module startup:
   ```python
   if hasattr(sys.stdout, "reconfigure"):
       try:
           sys.stdout.reconfigure(encoding="utf-8", errors="replace")
       except Exception:
           pass
   ```
2. Use standard ASCII brackets `[FAST /LATEST TICK]` and `[5M GE BASELINE TICK]` in terminal print statements.

### Metadata
- Reproducible: yes
- Related Files: [dump DVS scanner.py](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dump%20DVS%20scanner.py)
- See Also: LRN-20261015-001

### Resolution
- **Resolved**: 2026-10-15T13:32:54Z
- **Commit/PR**: Local patch
- **Notes**: Added `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` and replaced terminal header emojis with clean ASCII bracket tags.

---

## [ERR-20261015-002] pylint_ci_workflow_and_type_narrowing_failures

**Logged**: 2026-10-15T22:35:00Z  
**Priority**: high  
**Status**: resolved  
**Area**: backend  

### Summary
Pylint failed with `E1136: Value 'best' is unsubscriptable` in `dvs_backtest.py`, unquoted file paths (`backend/dump DVS scanner.py`) word-split in CI bash script, and missing `requests` dependency caused `E0401` import errors.

### Error
```text
************* Module dvs_backtest
backend\dvs_backtest.py:471:35: E1136: Value 'best' is unsubscriptable (unsubscriptable-object)
pylint: error: Cannot find factor 'backend/dump'
```

### Context
- GitHub Actions CI workflow executed `pylint $(git ls-files '*.py')` across Python matrix without installing `requests`.
- Filename with space `backend/dump DVS scanner.py` expanded unquoted into separate tokens (`backend/dump`, `DVS`, `scanner.py`).
- Pylint did not narrow `best: Optional[Tuple[float, float]]` in `if best is None or total > best[1]:`.

### Suggested Fix
1. Explicitly narrow `best` using `if best is None: best = ... elif total > best[1]: best = ...`.
2. Configure `.pylintrc` ignoring non-source paths (`misc/`, `.venv/`) and targeting `backend/` and `website/` directly.
3. Update GitHub Actions workflow to install `requests` alongside `pylint` and invoke `pylint backend/ website/`.

### Metadata
- Reproducible: yes
- Related Files: [dvs_backtest.py](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dvs_backtest.py), [.github/workflows/pylint.yml](file:///c:/Users/Gary0/Downloads/dump%20finder/.github/workflows/pylint.yml), [.pylintrc](file:///c:/Users/Gary0/Downloads/dump%20finder/.pylintrc)
- See Also: LRN-20261015-001

### Resolution
- **Resolved**: 2026-10-15T22:35:00Z
- **Commit/PR**: Local patch
- **Notes**: Rewrote type narrowing in `dvs_backtest.py`, cleaned unused imports in `dvs_webapp.py`, removed redundant `json` reimport in `dump DVS scanner.py`, created project `.pylintrc`, and updated `.github/workflows/pylint.yml`. Code rated 10.00/10.

---
