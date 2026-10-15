---
description: Run dump scanner with funnel output
allowed-tools: Bash(python:*), Read
argument-hint: [--debug|--relaxed|--watch]
---

# Run Dump Scanner

Run the OSRS dump scanner: `python "backend/dump DVS scanner.py" $ARGUMENTS`.

If $ARGUMENTS is empty, default to `--debug` one-shot + rejection funnel.
Quote the path — filename contains spaces and is not importable.

Rules:

- Logging is on by default (CSV writes). Only add `--no-log` if user asks.
- `--relaxed` relaxes exactly four things (DVS 45, profit 40k, drop 2.0%, margin pillar 5.0).
- Webhook: only use `DVS_WEBHOOK_URL` env or `--webhook-url` flag; fires on new `STRONG BUY` only. Never print secrets.
- Verification is reading the funnel output, not assuming success.

Report: funnel counts, top candidates with DVS/profit, any GE API errors by type (429/5xx vs data bug).
