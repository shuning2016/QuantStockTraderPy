# Signal Tracking Feature — Design Spec
**Date:** 2026-05-06  
**Status:** Approved  

---

## Overview

Add a signal tracking layer that monitors insider trades (SEC Form 4), politician trades, and ARK fund trades for stocks in the watchlist. Signals appear as colored dots in the sidebar and a detail panel in each stock card. A new "信号追踪" tab provides settings management and a list of signals for stocks not yet in the watchlist.

**Hard constraint:** No changes to `strategy_v6.py`, `build_prompt_v6`, `execute_decisions`, `run_trade_session`, or any AI prompt logic. Signal data is display-only and never injected into trading prompts.

---

## Data Sources

| Signal Type | Source | Auth |
|-------------|--------|------|
| Insider trades (Form 4) | OpenInsider.com CSV endpoint | None — free, no API key |
| Politician trades | Quiver Quantitative API | Free tier — requires free registration for API key; fallback: Capitol Trades public website scrape |
| ARK fund trades | ARK Invest daily holdings CSV | None — direct download |

**OpenInsider filter criteria** (applied server-side before caching):
- Open-market purchases/sales only (exclude option exercises, ESPP, gifts)
- Transaction value > $50K
- Exclude 10b5-1 scheduled plans (`is10b5=0`)
- Roles: CEO, CFO, COO, President, Director, 10%+ owner

**Politician filter:** Only trades by names present in `signal_config.politicians`.

**ARK filter:** Compare today's CSV vs yesterday's cached CSV to compute net share change per ticker. Only include tickers with |net change| > 10,000 shares.

---

## Data Model

### `signal_config` (KV key: `signal_config`)
```json
{
  "politicians": ["Nancy Pelosi", "Michael Burgess"],
  "ark_funds": ["ARKK", "ARKW"],
  "updated_at": "2026-05-06"
}
```
Persisted via same `_kv()` / local-file pattern as `watchlist`.

### `signal_cache` (KV key: `signal_cache`)
```json
{
  "fetched_at": "2026-05-06T09:00:00",
  "partial": false,
  "watchlist_signals": {
    "NVDA": [
      {
        "type": "insider",
        "who": "Jensen Huang",
        "role": "CEO",
        "action": "buy",
        "amount": 2500000,
        "shares": 20000,
        "date": "2026-05-01",
        "filing_date": "2026-05-03",
        "is_plan": false
      }
    ],
    "AAPL": [
      {
        "type": "ark",
        "fund": "ARKK",
        "action": "buy",
        "shares": 150000,
        "date": "2026-05-05"
      }
    ]
  },
  "untracked_signals": [
    {
      "sym": "TSM",
      "type": "politician",
      "who": "Nancy Pelosi",
      "role": "D-CA",
      "action": "buy",
      "amount": 1000000,
      "date": "2026-05-03"
    }
  ]
}
```

`watchlist_signals` is keyed by symbol for O(1) frontend lookup. `untracked_signals` is a flat list for the "未追踪" panel.

---

## Backend — New File: `signals.py`

Six functions, zero dependencies on `strategy_v6.py`:

```
fetch_insider_trades(symbols: list[str]) -> dict[str, list[dict]]
  - Calls OpenInsider CSV endpoint per symbol (batched where possible)
  - Returns {sym: [signal, ...]}

fetch_politician_trades(politicians: list[str], watchlist: list[str]) -> tuple[dict, list]
  - Calls Quiver Quantitative free API
  - Returns (watchlist_matches, untracked_list)

fetch_ark_trades(funds: list[str], watchlist: list[str], prev_cache: dict) -> tuple[dict, list]
  - Downloads ARK daily CSVs for each fund
  - Diffs vs yesterday's ARK holdings stored in prev_cache["ark_holdings"]
  - prev_cache is the result of load_signal_cache() passed in by refresh_signals()
  - Returns (watchlist_matches, untracked_list)

refresh_signals(watchlist: list[str], config: dict) -> dict
  - Calls the three fetch functions above
  - Merges results into signal_cache shape
  - Saves to KV + local file, returns the cache object

load_signal_config() -> dict
save_signal_config(config: dict) -> None
load_signal_cache() -> dict
save_signal_cache(cache: dict) -> None
  - Same KV + local-file fallback pattern as load_watchlist() / save_watchlist()
```

**Error handling:** Each fetch function catches exceptions independently. If one source fails (e.g. OpenInsider rate-limits), the others still run. Partial results are cached with a `"partial": true` flag so the UI can show a warning.

---

## Backend — Changes to `app.py`

**Four new `dispatch()` cases** (added at end of dispatch function, no existing cases touched):

```python
if action == "getSignalConfig":
    return load_signal_config()
if action == "saveSignalConfig":
    save_signal_config(data["config"])
    return {"saved": True}
if action == "getSignalCache":
    return load_signal_cache()
if action == "refreshSignals":
    return refresh_signals(load_watchlist(), load_signal_config())
```

**One new cron route** (added after existing cron routes):

```python
@app.route("/api/cron/signals", methods=["GET"])
def cron_signals():
    # Runs daily at 09:00 ET, after market open data is available
    ...
```

**`vercel.json`** — add one cron entry:
```json
{"path": "/api/cron/signals", "schedule": "0 13 * * 1-5"}
```
(09:00 ET = 13:00 UTC on weekdays)

---

## Frontend — Changes to `index.html`

### 1. Sidebar signal dots

In the `renderStockList()` function, after rendering the symbol, inject colored dots if `signalCache.watchlist_signals[sym]` has entries:

- 🟡 `#f59e0b` — insider signal present
- 🟣 `#a855f7` — politician signal present  
- 🟢 `#22c55e` — ARK signal present

Dots appear between the symbol and the delete button. Tooltip on hover shows signal count.

### 2. Stock card signal panel

In `buildCard()`, after the existing price/news section, append a "📡 信号追踪" section if signals exist for the symbol. Each signal renders as a color-coded left-border block:

- Amber border (`#f59e0b`) for insider
- Purple border (`#a855f7`) for politician
- Green border (`#22c55e`) for ARK

Each block shows: who, role, action (BUY/SELL badge), amount/shares, date.

If no signals exist for the symbol, the section is omitted entirely (no empty state shown).

### 3. New "信号追踪" tab

Added as the 5th tab (before 日检), with three sub-panels:

**⚙️ 设置 sub-panel:**
- Politicians: tag-input (type name + Enter to add, × to remove)
- ARK funds: fixed toggle chips for ARKK / ARKW / ARKQ / ARKG / ARKF / ARKX
- Save button → calls `saveSignalConfig`
- "立即刷新信号" button → calls `refreshSignals`, shows spinner
- Last-updated timestamp from `signal_cache.fetched_at`

**🔔 未追踪信号 sub-panel:**
- Lists `signal_cache.untracked_signals`
- Each row: symbol, signal type badge, who/fund, action, amount, date
- "＋ 加入 Watchlist" button → calls existing `addItem()` then `saveStocks()`
- Badge count on sub-tab label (red pill) showing untracked count

**📋 全部信号 sub-panel:**
- Flat list of all signals across all watchlist symbols
- Sorted by date descending
- Same row format as untracked panel

### Signal data lifecycle in frontend

```
app load → api("getSignalCache") → store in window.signalCache
         → api("getSignalConfig") → store in window.signalConfig

renderStockList() reads signalCache.watchlist_signals for dots
buildCard() reads signalCache.watchlist_signals[sym] for panel
"信号追踪" tab reads both signalCache and signalConfig

"立即刷新" button → api("refreshSignals") → update window.signalCache → re-render
```

---

## Cron Schedule

| Cron | Time (ET) | Days | Purpose |
|------|-----------|------|---------|
| `/api/cron/signals` | 09:00 | Mon–Fri | Fetch all signal sources after open |

Signals older than 30 days are dropped from the cache during each refresh.

---

## What Is Explicitly Out of Scope

- No changes to `strategy_v6.py`, `build_prompt_v6`, `execute_decisions`, or `run_trade_session`
- Signal data is never injected into AI trading prompts
- No automatic watchlist modification (user clicks "＋ 加入 Watchlist" manually)
- No crypto signal tracking (insider/politician data only covers equities)
- No historical signal replay or backtesting
