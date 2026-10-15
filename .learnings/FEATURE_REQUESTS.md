# Feature Requests Log

This file tracks user-requested capabilities and future enhancements for the OSRS DVS Scanner.

---

## [FEAT-20261015-001] hybrid_fast_polling_loop

**Logged**: 2026-10-15T13:21:45Z  
**Priority**: high  
**Status**: resolved  
**Area**: backend  

### Requested Capability
A 30-to-60-second fast-polling loop using the `/latest` endpoint for real-time dump detection without waiting the full 5 minutes for GE tick rollovers.

### User Context
Waiting 5 minutes to spot market panic means missing rapid dump-and-bounce spikes where items recover within 60–90 seconds.

### Complexity Estimate
medium

### Suggested Implementation
Build a stateful `MarketScannerEngine` class maintaining a cached 5m baseline (`/5m`, `/1h`, `/24h`) refreshed ~12s after GE rollover, with a fast loop evaluating `/latest` every 30s.

### Metadata
- Frequency: recurring
- Related Features: auto_watch, timeseries_knife_check

### Resolution
- **Resolved**: 2026-10-15T13:36:25Z
- **Commit/PR**: Local patch
- **Notes**: Implemented in `dump DVS scanner.py` and `dvs_webapp.py` with 0.15s fast-scan cycle time.

---

## [FEAT-20261015-002] potion_decant_arbitrage

**Logged**: 2026-10-15T13:21:45Z  
**Priority**: medium  
**Status**: resolved  
**Area**: backend  

### Requested Capability
Logic to flag or filter non-4-dose potions ((1), (2), (3) doses) or calculate whether decanting them at Bob Barter into (4)-dose vials yields a profitable arbitrage flip.

### User Context
Partial-dose potions are frequently dumped below their dose-equivalent value, creating risk-free decanting arbitrage, but can also clutter tables if illiquid.

### Complexity Estimate
medium

### Suggested Implementation
Map base potion names with regex, look up the 4-dose counterpart exit price, compute post-tax net proceeds per (4)-dose equivalent, and display a `🧪 Decant Arb` badge with ROI and profit. Provide a toggle to hide partial potions.

### Metadata
- Frequency: first_time
- Related Features: dump_candidate_dataclass, webapp_filter_bar

### Resolution
- **Resolved**: 2026-10-15T13:35:27Z
- **Commit/PR**: Local patch
- **Notes**: Integrated into `dump DVS scanner.py` and `dvs_webapp.py` with `--filter-potions` flag and UI checkbox.

---
