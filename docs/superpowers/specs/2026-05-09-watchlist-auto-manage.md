# Watchlist Auto-Management Design

**Date:** 2026-05-09
**Goals:** Maximize profit, control risks, maintain 短线交易 (1-5 day) focus
**Scope:** Two features — ARK auto-add and 5-day staleness cleanup

---

## Problem

The watchlist is manually managed. Two gaps:

1. **Coverage gap:** ARK funds buy stocks that never appear in the watchlist, so the AI never analyzes them. The user spots these manually but has no way to inject them automatically.
2. **Bloat gap:** Over time, stale stocks accumulate in the watchlist, diluting AI focus and increasing token usage per session.

No new trading sessions are added. The 4-session structure (premarket / opening / mid / closing) is unchanged.

---

## Feature 1 — ARK Auto-Add

### Trigger
Runs whenever signals are refreshed: daily cron (`/api/cron/signals`, 13:00 UTC Mon–Fri) and manual refresh via the UI.

### Logic
`fetch_ark_trades()` in `signals.py` already returns `untracked_list` — ARK signals for tickers NOT currently in the watchlist. Each signal has `action: "buy" | "sell"`.

After `_refresh_signals()` completes in `app.py`:
1. Collect all tickers from `untracked_signals` where `type == "ark"` and `action == "buy"`.
2. Deduplicate against the current watchlist (case-insensitive).
3. Append new tickers to the watchlist and call `save_watchlist()`.

No user confirmation required. No UI notification.

### Constraints
- Only ARK **buy** signals trigger an add. ARK sells are ignored (no auto-remove from ARK sells — Rule 1 was explicitly excluded).
- Only ARK funds (ARKK, ARKW, ARKQ, ARKG, ARKF, ARKX). Fund manager 13F signals do not trigger auto-add (they remain manually managed).
- If the ticker is already in the watchlist, skip silently.
- If the user's configured `ark_funds` list in signal config is empty, this feature does nothing.

---

## Feature 2 — 5-Day Staleness Cleanup

### Trigger
Runs immediately after Feature 1 completes, as part of the same signal refresh flow.

### Logic
After signals are refreshed and ARK auto-adds are applied:

1. Build a `{sym: latest_signal_date}` map from the full signal cache — covering all signal types (ARK, fund manager, insider, politician).
2. Load current holdings across all providers to determine which symbols have open positions.
3. For each watchlist stock:
   - If the stock has **never appeared in any signal** → skip (leave it; manually-added stocks with no signal history are not eligible for auto-removal).
   - If `latest_signal_date` is older than 5 calendar days from today **AND** the stock has no open position → remove it silently.
4. Save the updated watchlist via `save_watchlist()`.

No log entry, no UI notification, no user confirmation.

### Safety override
A stock is **never removed** if any provider currently holds an open position in it, regardless of signal age.

### Signal date interpretation
- ARK signals: `date` field is the trade date (daily cadence) — reliable for 5-day freshness check.
- Fund manager 13F signals: `date` field is the filing date (quarterly cadence) — these will look stale quickly under a 5-day rule. This is intentional: manually-added stocks that no longer have fresh activity are cleaned up. The user can re-add them at any time.
- Insider / politician signals: `date` field is the transaction date.

---

## Execution Order (per signal refresh)

```
_refresh_signals()          # fetch all signals, returns updated cache
  └─ ARK auto-add           # add new ARK buys to watchlist
  └─ staleness cleanup      # remove stale stocks with no open position
save_watchlist()            # persist final watchlist state
save_signal_cache()         # persist signal cache
```

Both features run in the same request/cron invocation. The staleness cleanup runs after auto-add so that a stock added this cycle isn't immediately evaluated for removal.

---

## What This Does NOT Change

- Trading sessions (premarket / opening / mid / closing) — unchanged.
- Guardian stop-loss / take-profit — unchanged.
- Fund manager 13F signals — still fetched and displayed, but do not trigger auto-add.
- Manual watchlist management — user can still add/remove stocks via the UI at any time.
- Signal display in the UI — existing signal cards are unaffected.

---

## Open Questions (resolved)

| Question | Decision |
|---|---|
| Require user confirmation for adds? | No |
| ARK-sell triggers auto-remove? | No (Rule 1 excluded) |
| Staleness window | 5 calendar days |
| Remove silently or log? | Silently |
| Fund manager buys trigger auto-add? | No, ARK only |
