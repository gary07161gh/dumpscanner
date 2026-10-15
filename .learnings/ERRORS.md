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
