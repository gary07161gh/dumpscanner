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

## [LRN-20261015-003] knowledge_gap

**Logged**: 2026-10-15T15:32:00Z  
**Priority**: medium  
**Status**: new  
**Area**: config  

### Summary
OpenCode v2.0.29 silently drops an agent file whose frontmatter contains `model: inherit`, a named `color`, or a `tools:` list — no log warning; the agent simply never appears in `opencode debug agents`.

### Details
While configuring the `scanner-maintainer` agent (Claude-Code-style frontmatter originally at `.opencode/agent/scanner-maintainer.md`), it was absent from `opencode debug agents` while all builtins listed. Isolated with one-field-at-a-time probe files (otherwise identical native frontmatter):
1. **Toxic fields** — any one of `model: inherit`, `color: yellow` (named color; V2 wants a six-digit hex), or `tools: ["Read", ...]` causes the whole agent file to be ignored. 3/3 probes with those fields failed to load; no WARN/ERROR appears in `~/.local/share/opencode/log/opencode.log`.
2. **Not the directory** — `.opencode/agent/` (singular) IS discovered in v2.0.29: a native-frontmatter probe placed in the singular directory loaded fine.
3. **Not `name:`** — `name:` alone is tolerated; the id stays filename-derived.
4. **Reload race** — `opencode reload` returns before agent re-discovery finishes. Querying `opencode debug agents` immediately after reload showed a stale list twice (missing `scanner-maintainer` right after reload, present seconds later). Sleep ~5s before verifying.
5. **Working native frontmatter** — `description: |` block, `mode: subagent`, `color: "#ffd600"`, ordered `permissions` allowlist; omit `model` (a subagent inherits the parent session's model); the body after `---` becomes `system`.

### Suggested Action
After editing any `.opencode/agents/*.md` file run `opencode reload; Start-Sleep -Seconds 5; opencode debug agents` and confirm the id appears. Translate Claude-Code-style frontmatter (`name`, `model: inherit`, named `color`, `tools:`) to native V2 fields instead of copying it verbatim.

### Metadata
- Source: conversation
- Related Files: [.opencode/agents/scanner-maintainer.md](file:///c:/Users/Gary0/Downloads/dump%20finder/.opencode/agents/scanner-maintainer.md)
- Tags: opencode, agents, config, frontmatter, silent-failure
- Pattern-Key: opencode.agent_silent_drop_legacy_frontmatter
- Recurrence-Count: 1
- First-Seen: 2026-10-15
- Last-Seen: 2026-10-15

---

## [LRN-20261015-004] knowledge_gap

**Logged**: 2026-10-15T15:42:00Z  
**Priority**: low  
**Status**: new  
**Area**: docs  

### Summary
Exact line-number citations inside the scanner-maintainer agent prompt silently drifted after later code changes to `backend/dump DVS scanner.py`; cite symbols, not line numbers.

### Details
The scanner-maintainer agent's own operational test (dispatched read-only) refused to trust the line numbers embedded in its prompt and re-read the source, finding them stale. Re-verified by grep on 2026-10-15:
- Cited 945 → actual **1151**: `field_names = ["timestamp_utc", ...] + [f.name for f in fields(DumpCandidate)]`
- Cited 966–970 → actual **1176**: `CANDIDATE_LOG_PATH.replace(archive)`
- Cited 512 → actual **717**: `prev_active_ids = ...`
- Cited 515 → actual **720**: `if was_fast and ... not in prev_active_ids ...`
- Cited 437 → actual **642**: `self.last_evaluated_low_times.clear()`
- Still exact: `website/dvs_webapp.py:51` (`spec_from_file_location`), `:61` (`ENGINE =`), `backend/dvs_backtest.py:51` (`REQUIRED_COLUMNS`)
- Not re-verified: `asdict(c)` (cited 984), `parse_args` (cited 1085–1088), `ThreadingHTTPServer` (cited 1236), webapp `sys.path` insert (cited 41)

The traps themselves still hold — only the pointers rotted. The likely cause is code growth in the scanner (e.g., the GE-client addition) after the prompt was authored.

### Suggested Action
Anchor behavioral claims to symbol names (`field_names =`, `CANDIDATE_LOG_PATH.replace`) rather than line numbers, and re-grep before quoting any `.py` line reference. Refresh the stale references in `.opencode/agents/scanner-maintainer.md` (and any doc citing scanner line numbers).

### Metadata
- Source: conversation
- Related Files: [.opencode/agents/scanner-maintainer.md](file:///c:/Users/Gary0/Downloads/dump%20finder/.opencode/agents/scanner-maintainer.md), [dump DVS scanner.py](file:///c:/Users/Gary0/Downloads/dump%20finder/backend/dump%20DVS%20scanner.py)
- Tags: documentation, line-references, drift, silent-failure, agent-prompt
- Pattern-Key: docs.line_refs_drift_after_code_growth
- Recurrence-Count: 1
- First-Seen: 2026-10-15
- Last-Seen: 2026-10-15

---
