# Learnings Log

This file tracks corrections, architecture discoveries, and best practice patterns across the project.

---

## [LRN-20261015-001] best_practice

**Logged**: 2026-10-15T13:36:25Z  
**Priority**: high  
**Status**: promoted  
**Area**: backend  

### Summary
Fast-polling optimization on `/latest` must retain and re-evaluate active candidates from prior ticks while skipping untraded non-candidates.

### Details
In a two-speed hybrid polling engine where the fast loop checks `/latest` every 30s:
1. Skipping items purely because `lowTime` has not updated causes previously found active candidates to disappear from results if they have not printed another trade within the 30s window.
2. In-memory dictionary lookups for 3,000 items take only ~15ms in Python. The bottleneck is solely outbound network calls (`/timeseries` and snapshot downloads).
3. The correct fast filtering approach tracks `prev_active_ids = {c.item_id for c in self.cached_candidates} | {c.item_id for c in self.cached_near_misses}`:
   - Always re-evaluate items in `prev_active_ids` to test whether they bounced past recovery thresholds or had their ceiling collapse.
   - Always evaluate items where `low_time > last_evaluated_low_time` (new trade registered).
   - Only skip non-active items where `low_time` has not changed.

### Suggested Action
Preserve `prev_active_ids` set during `/latest` fast passes to maintain active candidate lifecycles and accurate Dump Age ticker data.

### Metadata
- Source: error
- Related Files: [dump DVS scanner.py](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dump%20DVS%20scanner.py), [dvs_webapp.py](file:///c:/Users/Gary0/Downloads/dump%20finder/website/dvs_webapp.py)
- Tags: polling, fast-loop, caching, state-management
- See Also: ERR-20261015-001
- Pattern-Key: backend.fast_loop_candidate_retention
- Recurrence-Count: 1
- First-Seen: 2026-10-15
- Last-Seen: 2026-10-15

### Resolution
- **Resolved**: 2026-10-15T13:36:10Z
- **Commit/PR**: Local patch
- **Notes**: Implemented `prev_active_ids` preservation and live Dump Age updates, reducing fast scan cycle time to ~0.15s while keeping candidates alive until bounce or expiration.

---

## [LRN-20261015-002] best_practice

**Logged**: 2026-10-15T13:40:00Z  
**Priority**: medium  
**Status**: promoted  
**Area**: frontend  

### Summary
Decouple live Dump Age elapsed ticker from network polling cycles using client-side intervals.

### Details
Rather than re-fetching data every second to update "X seconds ago" displays:
1. Server returns static Unix timestamp `low_time` on each candidate object.
2. Table cells render with `data-lowtime="${c.low_time}"` attributes.
3. A 1000ms client `setInterval` iterates over `.dump-age-cell` DOM elements and computes `Math.max(0, Math.floor(Date.now() / 1000 - lowTime))` locally.
4. Prevents unnecessary network chatter and keeps UI buttery smooth at 60fps.

### Suggested Action
Use `data-lowtime` DOM attributes and a client-side ticker interval for all relative-age displays in real-time dashboards.

### Metadata
- Source: conversation
- Related Files: [dvs_webapp.py](file:///c:/Users/Gary0/Downloads/dump%20finder/website/dvs_webapp.py)
- Tags: frontend, performance, ux, real-time
- Pattern-Key: frontend.client_side_relative_timestamps
- Recurrence-Count: 1
- First-Seen: 2026-10-15
- Last-Seen: 2026-10-15

---
