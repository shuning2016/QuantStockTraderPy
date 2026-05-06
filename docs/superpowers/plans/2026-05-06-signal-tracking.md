# Signal Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add insider / politician / ARK signal tracking — colored dots in the watchlist sidebar, a signal detail panel in each stock card, and a new "信号追踪" tab with settings and an untracked-signals list.

**Architecture:** New `signals.py` module with pure fetch functions (no storage, no imports from app.py). Storage functions (`load_signal_config`, etc.) and API endpoints live in `app.py` to match existing patterns and avoid circular imports. Frontend changes are limited to `index.html`.

**Tech Stack:** Python `requests` + `csv` (stdlib) for data fetching; Flask dispatch for API; Upstash KV / local JSON for persistence; vanilla JS in `index.html`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `signals.py` | **Create** | Fetch insider / politician / ARK data; `refresh_signals()` orchestrator |
| `config.py` | **Modify** | Add `QUIVER_KEY` env var |
| `app.py` | **Modify** | Add storage functions + 4 dispatch cases + 1 cron route |
| `vercel.json` | **Modify** | Add cron entry for `/api/cron/signals` at 09:00 ET weekdays |
| `templates/index.html` | **Modify** | Sidebar dots, card signal panel, new 信号追踪 tab |
| `tests/test_signals.py` | **Create** | Unit tests for all `signals.py` functions |

---

## Task 1: `signals.py` — fetch_insider_trades()

**Files:**
- Create: `signals.py`
- Create: `tests/test_signals.py`

- [ ] **Step 1: Create `signals.py` skeleton with `fetch_insider_trades`**

```python
"""
signals.py — Signal tracking: insider trades, politician trades, ARK fund trades.

Pure fetch/compute functions only.  No storage, no imports from app.py.
Storage (load_signal_config, save_signal_config, load_signal_cache, save_signal_cache)
lives in app.py to avoid circular imports.
"""

import csv
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger("quant.signals")

# ── ARK fund CSV URLs ─────────────────────────────────────────────────────────
ARK_CSV_URLS: dict[str, str] = {
    "ARKK": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKW": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKQ": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_AUTONOMOUS_TECHNOLOGY_&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKG": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKF": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
    "ARKX": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_SPACE_EXPLORATION_&_INNOVATION_ETF_ARKX_HOLDINGS.csv",
}

# OpenInsider screener endpoint — vl=500000 filters trades >= $500K value
# is10b5=0 excludes 10b5-1 scheduled-plan trades at source
_OPENINSIDER_URL = (
    "http://openinsider.com/screener?s={sym}"
    "&fd=-1&td=&tdr=&fdlyl=&fdlyh=&daysago=30"
    "&xs=1&vl=500000"
    "&isofficer=1&iscob=1&isceo=1&ispres=1&iscoo=1&iscfo=1"
    "&isgc=1&isvp=1&isdirector=1&is10b5=0&istenpc=1"
    "&isb=1&iss=1&isauto=1&csv=1"
)


def _parse_value(raw: str) -> int:
    """Parse OpenInsider value string like '$2,500,000' → 2500000."""
    return int(raw.replace("$", "").replace(",", "").replace("+", "").strip() or "0")


def fetch_insider_trades(symbols: list[str]) -> dict[str, list[dict]]:
    """
    Fetch recent insider trades for the given symbols from OpenInsider.

    Returns {sym: [signal, ...]} where each signal is:
      {"type": "insider", "who": str, "role": str, "action": "buy"|"sell",
       "amount": int, "shares": int, "date": str, "filing_date": str, "is_plan": False}

    Filters applied:
    - Transaction value >= $500K (enforced by URL param vl=500000)
    - 10b5-1 plans excluded (enforced by URL param is10b5=0)
    - Only P-Purchase and S-Sale trade types included
    """
    result: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            url = _OPENINSIDER_URL.format(sym=sym)
            resp = requests.get(url, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            signals = _parse_insider_csv(sym.upper(), resp.text)
            if signals:
                result[sym.upper()] = signals
            time.sleep(0.3)  # be polite to OpenInsider
        except Exception as e:
            logger.warning("fetch_insider_trades(%s) failed: %s", sym, e)
    return result


def _parse_insider_csv(sym: str, csv_text: str) -> list[dict]:
    """Parse OpenInsider CSV text for one symbol."""
    signals = []
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            # OpenInsider columns: X, Filing Date, Trade Date, Ticker,
            # Company Name, Insider Name, Title, Trade Type, Price, Qty,
            # Owned, ΔOwn, Value
            trade_type = row.get("Trade Type", "").strip()
            if "P - Purchase" in trade_type:
                action = "buy"
            elif "S - Sale" in trade_type:
                action = "sell"
            else:
                continue  # skip option exercises, gifts, etc.

            try:
                amount = _parse_value(row.get("Value", "0"))
            except ValueError:
                continue

            signals.append({
                "type":         "insider",
                "who":          row.get("Insider Name", "").strip(),
                "role":         row.get("Title", "").strip(),
                "action":       action,
                "amount":       amount,
                "shares":       int(row.get("Qty", "0").replace(",", "") or "0"),
                "date":         row.get("Trade Date", "").strip(),
                "filing_date":  row.get("Filing Date", "").strip(),
                "is_plan":      False,
            })
    except Exception as e:
        logger.warning("_parse_insider_csv(%s) failed: %s", sym, e)
    return signals
```

- [ ] **Step 2: Create `tests/test_signals.py` with insider fetch test**

```python
"""Tests for signals.py — insider, politician, and ARK signal fetching."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
import signals


# ── Helpers ───────────────────────────────────────────────────────────────────

INSIDER_CSV = """\
X,Filing Date,Trade Date,Ticker,Company Name,Insider Name,Title,Trade Type,Price,Qty,Owned,ΔOwn,Value
1,2026-05-03,2026-05-01,NVDA,NVIDIA Corp,Jensen Huang,CEO,P - Purchase,$1084.00,"20,000","1,000,000",+2%,"$21,680,000"
2,2026-05-02,2026-04-30,NVDA,NVIDIA Corp,Some CFO,CFO,S - Sale,$1090.00,"5,000","500,000",-1%,"$5,450,000"
3,2026-05-01,2026-04-29,NVDA,NVIDIA Corp,Some Dir,Director,A - Award,$100.00,"1,000","200,000",+0.5%,"$100,000"
"""


def test_parse_insider_csv_filters_buy_sell():
    result = signals._parse_insider_csv("NVDA", INSIDER_CSV)
    actions = [s["action"] for s in result]
    assert "buy" in actions
    assert "sell" in actions
    # Award type should be excluded
    assert len(result) == 2


def test_parse_insider_csv_fields():
    result = signals._parse_insider_csv("NVDA", INSIDER_CSV)
    buy = next(s for s in result if s["action"] == "buy")
    assert buy["who"] == "Jensen Huang"
    assert buy["role"] == "CEO"
    assert buy["amount"] == 21_680_000
    assert buy["shares"] == 20_000
    assert buy["date"] == "2026-05-01"
    assert buy["type"] == "insider"
    assert buy["is_plan"] is False


def test_fetch_insider_trades_uses_sleep(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(signals.time, "sleep", lambda x: sleep_calls.append(x))

    class FakeResp:
        text = INSIDER_CSV
        def raise_for_status(self): pass

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())
    result = signals.fetch_insider_trades(["NVDA", "AAPL"])
    assert "NVDA" in result
    assert len(sleep_calls) == 2  # one per symbol


def test_fetch_insider_trades_handles_http_error(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    def bad_get(*a, **kw):
        raise requests.RequestException("timeout")
    import requests as req
    monkeypatch.setattr(signals.requests, "get", bad_get)
    result = signals.fetch_insider_trades(["NVDA"])
    assert result == {}  # graceful failure, no crash
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
cd "/Users/shuning.wang/Library/CloudStorage/GoogleDrive-shuning2016@gmail.com/My Drive/My Projects/Python projects/StockTraderPy"
python -m pytest tests/test_signals.py::test_parse_insider_csv_filters_buy_sell tests/test_signals.py::test_parse_insider_csv_fields tests/test_signals.py::test_fetch_insider_trades_uses_sleep tests/test_signals.py::test_fetch_insider_trades_handles_http_error -v
```

Expected: 4 PASSED

- [ ] **Step 4: Commit**

```bash
git add signals.py tests/test_signals.py
git commit -m "feat: add signals.py with fetch_insider_trades"
```

---

## Task 2: `signals.py` — fetch_politician_trades()

**Files:**
- Modify: `config.py`
- Modify: `signals.py`
- Modify: `tests/test_signals.py`

- [ ] **Step 1: Add `QUIVER_KEY` to `config.py`**

In `config.py`, after the `SERPAPI_KEY` line (line 39), add:

```python
QUIVER_KEY   = _env("QUIVER_KEY")    # Quiver Quantitative — quiverquant.com (free tier)
```

Also add to `check_config()` warnings (after the `NEWSAPI_KEY` check):

```python
    if not QUIVER_KEY:
        warnings.append("QUIVER_KEY not set — politician trade signals will be unavailable")
```

- [ ] **Step 2: Add `fetch_politician_trades` to `signals.py`**

Add after `fetch_insider_trades` in `signals.py`:

```python
# Quiver Quantitative congressional trading bulk endpoint
# Returns last ~30 days of all congressional trades
_QUIVER_CONGRESS_URL = "https://api.quiverquant.com/beta/bulk/congresstrading"


def fetch_politician_trades(
    politicians: list[str],
    watchlist: list[str],
    quiver_key: str,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """
    Fetch recent congressional trades from Quiver Quantitative.

    Returns:
      watchlist_matches: {sym: [signal, ...]} for symbols already in watchlist
      untracked_list:    [signal with "sym" key] for symbols NOT in watchlist

    Each signal:
      {"type": "politician", "who": str, "role": str, "action": "buy"|"sell",
       "amount_range": str, "date": str, "sym": str}
    """
    if not quiver_key:
        logger.warning("fetch_politician_trades: QUIVER_KEY not set, skipping")
        return {}, []

    watchlist_upper = {s.upper() for s in watchlist}
    politicians_lower = {p.lower() for p in politicians}
    watchlist_matches: dict[str, list[dict]] = {}
    untracked_list: list[dict] = []

    try:
        resp = requests.get(
            _QUIVER_CONGRESS_URL,
            headers={"Authorization": f"Token {quiver_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        trades = resp.json()
    except Exception as e:
        logger.warning("fetch_politician_trades failed: %s", e)
        return {}, []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    for trade in trades:
        rep   = (trade.get("Representative") or "").strip()
        if rep.lower() not in politicians_lower:
            continue

        date  = (trade.get("Date") or "")[:10]
        if date < cutoff:
            continue

        sym   = (trade.get("Ticker") or "").upper()
        if not sym:
            continue

        tx    = (trade.get("Transaction") or "").lower()
        if "purchase" in tx or "buy" in tx:
            action = "buy"
        elif "sale" in tx or "sell" in tx:
            action = "sell"
        else:
            continue

        signal: dict = {
            "type":         "politician",
            "who":          rep,
            "role":         trade.get("Party", "") + "-" + trade.get("State", ""),
            "action":       action,
            "amount_range": trade.get("Range", ""),
            "date":         date,
            "sym":          sym,
        }

        if sym in watchlist_upper:
            watchlist_matches.setdefault(sym, []).append(signal)
        else:
            untracked_list.append(signal)

    return watchlist_matches, untracked_list
```

- [ ] **Step 3: Add tests to `tests/test_signals.py`**

```python
QUIVER_RESPONSE = [
    {"Date": "2026-05-03", "Ticker": "NVDA", "Representative": "Nancy Pelosi",
     "Transaction": "Purchase", "Range": "$500,001-$1,000,000",
     "Party": "D", "State": "CA"},
    {"Date": "2026-05-01", "Ticker": "TSM", "Representative": "Nancy Pelosi",
     "Transaction": "Purchase", "Range": "$250,001-$500,000",
     "Party": "D", "State": "CA"},
    {"Date": "2026-04-28", "Ticker": "AAPL", "Representative": "Other Person",
     "Transaction": "Sale", "Range": "$1,000,001-$5,000,000",
     "Party": "R", "State": "TX"},
    # Old trade — should be excluded (set date far in past)
    {"Date": "2025-01-01", "Ticker": "MSFT", "Representative": "Nancy Pelosi",
     "Transaction": "Purchase", "Range": "$100,001-$250,000",
     "Party": "D", "State": "CA"},
]


def test_fetch_politician_trades_splits_watchlist_vs_untracked(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return QUIVER_RESPONSE

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())

    wl_matches, untracked = signals.fetch_politician_trades(
        politicians=["Nancy Pelosi"],
        watchlist=["NVDA", "AAPL"],
        quiver_key="testkey",
    )
    # NVDA is in watchlist → watchlist_matches
    assert "NVDA" in wl_matches
    assert wl_matches["NVDA"][0]["action"] == "buy"
    # TSM is NOT in watchlist → untracked
    assert any(s["sym"] == "TSM" for s in untracked)
    # Other Person is not in politicians list → excluded
    assert "AAPL" not in wl_matches
    # Old trade (2025) → excluded
    assert not any(s["sym"] == "MSFT" for s in untracked)


def test_fetch_politician_trades_no_key_returns_empty(monkeypatch):
    wl, untracked = signals.fetch_politician_trades(
        politicians=["Nancy Pelosi"],
        watchlist=["NVDA"],
        quiver_key="",
    )
    assert wl == {}
    assert untracked == []
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_signals.py::test_fetch_politician_trades_splits_watchlist_vs_untracked tests/test_signals.py::test_fetch_politician_trades_no_key_returns_empty -v
```

Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add signals.py config.py tests/test_signals.py
git commit -m "feat: add fetch_politician_trades with Quiver API"
```

---

## Task 3: `signals.py` — fetch_ark_trades()

**Files:**
- Modify: `signals.py`
- Modify: `tests/test_signals.py`

- [ ] **Step 1: Add `fetch_ark_trades` to `signals.py`**

Add after `fetch_politician_trades` in `signals.py`:

```python
def _parse_ark_csv(csv_text: str) -> dict[str, int]:
    """
    Parse ARK holdings CSV into {ticker: shares} dict.

    ARK CSV columns: date, fund, company, ticker, cusip, shares, market value ($), weight (%)
    Returns empty dict on any parse error.
    """
    holdings: dict[str, int] = {}
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            ticker = (row.get("ticker") or row.get("Ticker") or "").strip().upper()
            if not ticker:
                continue
            raw_shares = (row.get("shares") or row.get("Shares") or "0").replace(",", "")
            try:
                holdings[ticker] = int(float(raw_shares))
            except ValueError:
                continue
    except Exception as e:
        logger.warning("_parse_ark_csv failed: %s", e)
    return holdings


def fetch_ark_trades(
    funds: list[str],
    watchlist: list[str],
    prev_ark_holdings: dict[str, dict[str, int]],
) -> tuple[dict[str, list[dict]], list[dict], dict[str, dict[str, int]]]:
    """
    Download today's ARK holdings CSVs, diff vs yesterday to find net trades.

    Args:
        funds: list of ARK fund tickers to check e.g. ["ARKK", "ARKW"]
        watchlist: list of stock symbols user is tracking
        prev_ark_holdings: {fund: {ticker: shares}} from yesterday's signal_cache["ark_holdings"]
                           Pass {} on first run.

    Returns:
        watchlist_matches: {sym: [signal, ...]} for watchlist symbols with net change > 10K shares
        untracked_list:    [signal] for non-watchlist symbols with net change > 10K shares
        today_holdings:    {fund: {ticker: shares}} — save this as signal_cache["ark_holdings"]

    Each signal:
        {"type": "ark", "fund": str, "action": "buy"|"sell",
         "shares": int, "date": str, "sym": str}
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    watchlist_upper = {s.upper() for s in watchlist}
    watchlist_matches: dict[str, list[dict]] = {}
    untracked_list: list[dict] = []
    today_holdings: dict[str, dict[str, int]] = {}

    for fund in funds:
        url = ARK_CSV_URLS.get(fund.upper())
        if not url:
            logger.warning("fetch_ark_trades: unknown fund %s", fund)
            continue
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            current = _parse_ark_csv(resp.text)
            today_holdings[fund.upper()] = current
        except Exception as e:
            logger.warning("fetch_ark_trades(%s) failed: %s", fund, e)
            continue

        prev = prev_ark_holdings.get(fund.upper(), {})
        all_tickers = set(current) | set(prev)

        for ticker in all_tickers:
            cur_shares = current.get(ticker, 0)
            prv_shares = prev.get(ticker, 0)
            net = cur_shares - prv_shares
            if abs(net) < 10_000:
                continue

            action = "buy" if net > 0 else "sell"
            signal: dict = {
                "type":   "ark",
                "fund":   fund.upper(),
                "action": action,
                "shares": abs(net),
                "date":   today,
                "sym":    ticker,
            }
            if ticker in watchlist_upper:
                watchlist_matches.setdefault(ticker, []).append(signal)
            else:
                untracked_list.append(signal)

        time.sleep(0.5)  # be polite between fund downloads

    return watchlist_matches, untracked_list, today_holdings
```

- [ ] **Step 2: Add tests to `tests/test_signals.py`**

```python
ARK_CSV_TODAY = """\
date,fund,company,ticker,cusip,shares,market value ($),weight (%)
05/06/2026,ARKK,NVIDIA Corp,NVDA,67066G104,"200000","$216000000",7.0%
05/06/2026,ARKK,Tesla Inc,TSLA,88160R101,"500000","$120000000",4.0%
05/06/2026,ARKK,Palantir,PLTR,69608A108,"300000","$15000000",0.5%
"""

ARK_CSV_PREV = {
    "ARKK": {
        "NVDA": 150_000,   # bought 50K more → buy signal
        "TSLA": 510_000,   # sold 10K → below 10K threshold, no signal
        # PLTR not present yesterday → new position of 300K → buy signal
    }
}


def test_fetch_ark_trades_detects_net_changes(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)

    class FakeResp:
        text = ARK_CSV_TODAY
        def raise_for_status(self): pass

    monkeypatch.setattr(signals.requests, "get", lambda *a, **kw: FakeResp())

    wl_matches, untracked, today_h = signals.fetch_ark_trades(
        funds=["ARKK"],
        watchlist=["NVDA"],
        prev_ark_holdings=ARK_CSV_PREV,
    )
    # NVDA: in watchlist, net +50K → buy in watchlist_matches
    assert "NVDA" in wl_matches
    assert wl_matches["NVDA"][0]["action"] == "buy"
    assert wl_matches["NVDA"][0]["shares"] == 50_000
    # TSLA: net -10K → exactly at threshold, excluded (< 10K strict)
    assert not any(s["sym"] == "TSLA" for s in untracked)
    # PLTR: not in watchlist, net +300K → untracked
    assert any(s["sym"] == "PLTR" for s in untracked)
    # today_holdings should reflect parsed CSV
    assert today_h["ARKK"]["NVDA"] == 200_000


def test_parse_ark_csv_handles_comma_shares():
    result = signals._parse_ark_csv(ARK_CSV_TODAY)
    assert result["NVDA"] == 200_000
    assert result["TSLA"] == 500_000


def test_fetch_ark_trades_skips_unknown_fund(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda x: None)
    wl, untracked, today_h = signals.fetch_ark_trades(
        funds=["FAKEFUND"],
        watchlist=["NVDA"],
        prev_ark_holdings={},
    )
    assert wl == {}
    assert today_h == {}
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/test_signals.py::test_fetch_ark_trades_detects_net_changes tests/test_signals.py::test_parse_ark_csv_handles_comma_shares tests/test_signals.py::test_fetch_ark_trades_skips_unknown_fund -v
```

Expected: 3 PASSED

- [ ] **Step 4: Commit**

```bash
git add signals.py tests/test_signals.py
git commit -m "feat: add fetch_ark_trades with daily CSV diff"
```

---

## Task 4: `signals.py` — refresh_signals() orchestrator

**Files:**
- Modify: `signals.py`
- Modify: `tests/test_signals.py`

- [ ] **Step 1: Add `refresh_signals` to `signals.py`**

Add at the end of `signals.py`:

```python
def refresh_signals(
    watchlist: list[str],
    config: dict,
    prev_cache: dict,
    quiver_key: str = "",
) -> dict:
    """
    Fetch all signal types and return a fresh signal_cache dict.

    Args:
        watchlist:   list of stock symbols from load_watchlist()
        config:      signal_config dict {"politicians": [...], "ark_funds": [...]}
        prev_cache:  previous signal_cache (used for ARK yesterday-holdings diff)
                     Pass {} on first run.
        quiver_key:  Quiver Quantitative API key

    Returns signal_cache dict:
        {
          "fetched_at":       ISO timestamp str,
          "partial":          bool (True if any source failed),
          "watchlist_signals": {sym: [signal, ...]},
          "untracked_signals": [signal, ...],
          "ark_holdings":      {fund: {ticker: shares}},  # saved for next diff
        }
    """
    stock_symbols = [s for s in watchlist if isinstance(s, str)]
    politicians   = config.get("politicians", [])
    ark_funds     = config.get("ark_funds", [])
    prev_ark      = prev_cache.get("ark_holdings", {})

    watchlist_signals: dict[str, list[dict]] = {}
    untracked_signals: list[dict]            = []
    partial = False

    # ── Insider trades ────────────────────────────────────────────
    try:
        insider_map = fetch_insider_trades(stock_symbols)
        for sym, sigs in insider_map.items():
            watchlist_signals.setdefault(sym, []).extend(sigs)
    except Exception as e:
        logger.error("refresh_signals: insider fetch failed: %s", e)
        partial = True

    # ── Politician trades ─────────────────────────────────────────
    try:
        pol_wl, pol_untracked = fetch_politician_trades(
            politicians=politicians,
            watchlist=stock_symbols,
            quiver_key=quiver_key,
        )
        for sym, sigs in pol_wl.items():
            watchlist_signals.setdefault(sym, []).extend(sigs)
        untracked_signals.extend(pol_untracked)
    except Exception as e:
        logger.error("refresh_signals: politician fetch failed: %s", e)
        partial = True

    # ── ARK trades ────────────────────────────────────────────────
    today_holdings: dict[str, dict[str, int]] = {}
    try:
        ark_wl, ark_untracked, today_holdings = fetch_ark_trades(
            funds=ark_funds,
            watchlist=stock_symbols,
            prev_ark_holdings=prev_ark,
        )
        for sym, sigs in ark_wl.items():
            watchlist_signals.setdefault(sym, []).extend(sigs)
        untracked_signals.extend(ark_untracked)
    except Exception as e:
        logger.error("refresh_signals: ARK fetch failed: %s", e)
        partial = True

    # ── Drop signals older than 30 days ───────────────────────────
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for sym in list(watchlist_signals):
        watchlist_signals[sym] = [
            s for s in watchlist_signals[sym]
            if (s.get("date") or "9999") >= cutoff
        ]
        if not watchlist_signals[sym]:
            del watchlist_signals[sym]
    untracked_signals = [
        s for s in untracked_signals
        if (s.get("date") or "9999") >= cutoff
    ]

    return {
        "fetched_at":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "partial":           partial,
        "watchlist_signals": watchlist_signals,
        "untracked_signals": untracked_signals,
        "ark_holdings":      today_holdings,
    }
```

- [ ] **Step 2: Add `refresh_signals` test to `tests/test_signals.py`**

```python
def test_refresh_signals_merges_all_sources(monkeypatch):
    monkeypatch.setattr(signals, "fetch_insider_trades",
                        lambda syms: {"NVDA": [{"type": "insider", "date": "2026-05-01",
                                                 "action": "buy", "amount": 2_000_000,
                                                 "shares": 10_000, "who": "CEO", "role": "CEO",
                                                 "filing_date": "2026-05-03", "is_plan": False}]})
    monkeypatch.setattr(signals, "fetch_politician_trades",
                        lambda politicians, watchlist, quiver_key:
                            ({"NVDA": [{"type": "politician", "date": "2026-05-02",
                                         "action": "buy", "amount_range": "$500K-$1M",
                                         "who": "Pelosi", "role": "D-CA", "sym": "NVDA"}]},
                             [{"type": "politician", "sym": "TSM", "date": "2026-05-03",
                               "action": "buy", "amount_range": "$1M-$5M",
                               "who": "Pelosi", "role": "D-CA"}]))
    monkeypatch.setattr(signals, "fetch_ark_trades",
                        lambda funds, watchlist, prev_ark_holdings:
                            ({}, [], {}))

    result = signals.refresh_signals(
        watchlist=["NVDA", "AAPL"],
        config={"politicians": ["Nancy Pelosi"], "ark_funds": ["ARKK"]},
        prev_cache={},
        quiver_key="testkey",
    )

    assert "fetched_at" in result
    assert result["partial"] is False
    # NVDA should have both insider + politician signals merged
    assert len(result["watchlist_signals"]["NVDA"]) == 2
    # TSM is untracked
    assert any(s["sym"] == "TSM" for s in result["untracked_signals"])


def test_refresh_signals_partial_on_failure(monkeypatch):
    def bad_fetch(syms):
        raise RuntimeError("network error")
    monkeypatch.setattr(signals, "fetch_insider_trades", bad_fetch)
    monkeypatch.setattr(signals, "fetch_politician_trades",
                        lambda **kw: ({}, []))
    monkeypatch.setattr(signals, "fetch_ark_trades",
                        lambda **kw: ({}, [], {}))

    result = signals.refresh_signals(
        watchlist=["NVDA"],
        config={},
        prev_cache={},
        quiver_key="",
    )
    assert result["partial"] is True
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/test_signals.py::test_refresh_signals_merges_all_sources tests/test_signals.py::test_refresh_signals_partial_on_failure -v
```

Expected: 2 PASSED

- [ ] **Step 4: Run full test suite to check no regressions**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASSED

- [ ] **Step 5: Commit**

```bash
git add signals.py tests/test_signals.py
git commit -m "feat: add refresh_signals orchestrator with 30-day pruning"
```

---

## Task 5: `app.py` — storage + dispatch + cron route; `vercel.json` — cron entry

**Files:**
- Modify: `app.py`
- Modify: `vercel.json`

- [ ] **Step 1: Add storage constants and import in `app.py`**

After `WATCHLIST_FILE = DATA_DIR / "watchlist.json"` (around line 105), add:

```python
SIGNAL_CONFIG_FILE = DATA_DIR / "signal_config.json"
SIGNAL_CACHE_FILE  = DATA_DIR / "signal_cache.json"
```

Add `from signals import refresh_signals as _refresh_signals` after the existing `from strategy_v6 import ...` block.

Find the existing `from config import (...)` block (around line 24) and add `QUIVER_KEY` to it:

```python
from config import (
    FINNHUB_KEY, NEWSAPI_KEY, CLAUDE_KEY, GROK_KEY, DEEPSEEK_KEY,
    SERPAPI_KEY, QUIVER_KEY, PORT, MODELS, MAX_TOKENS, SESSION_MAX_TOKENS,
    FINNHUB_QUOTE_URL, FINNHUB_NEWS_URL, COINGECKO_COIN_URL, NEWSAPI_URL,
    PRICE_CACHE_TTL, CRYPTO_CACHE_TTL, NEWS_CACHE_TTL,
    check_config,
)
```

- [ ] **Step 2: Add storage functions to `app.py`**

After `save_watchlist()` (around line 220), add:

```python
def load_signal_config() -> dict:
    if _USE_KV:
        data = _kv(["GET", "signal_config"])
        if data:
            try:
                return json.loads(data)
            except Exception:
                pass
    if SIGNAL_CONFIG_FILE.exists():
        try:
            return json.loads(SIGNAL_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {"politicians": [], "ark_funds": [], "updated_at": ""}

def save_signal_config(config: dict) -> None:
    if _USE_KV:
        _kv(["SET", "signal_config", json.dumps(config)])
    try:
        SIGNAL_CONFIG_FILE.write_text(json.dumps(config))
    except OSError:
        pass

def load_signal_cache() -> dict:
    if _USE_KV:
        data = _kv(["GET", "signal_cache"])
        if data:
            try:
                return json.loads(data)
            except Exception:
                pass
    if SIGNAL_CACHE_FILE.exists():
        try:
            return json.loads(SIGNAL_CACHE_FILE.read_text())
        except Exception:
            pass
    return {"fetched_at": "", "partial": False,
            "watchlist_signals": {}, "untracked_signals": [], "ark_holdings": {}}

def save_signal_cache(cache: dict) -> None:
    if _USE_KV:
        _kv(["SET", "signal_cache", json.dumps(cache)])
    try:
        SIGNAL_CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass
```

- [ ] **Step 3: Add 4 dispatch cases to `dispatch()` in `app.py`**

Inside `dispatch()`, after the last existing `if action ==` block (before the `return None` or raise at the end), add:

```python
    # Signal tracking
    if action == "getSignalConfig":
        return load_signal_config()
    if action == "saveSignalConfig":
        save_signal_config(data["config"])
        return {"saved": True}
    if action == "getSignalCache":
        return load_signal_cache()
    if action == "refreshSignals":
        cfg   = load_signal_config()
        prev  = load_signal_cache()
        cache = _refresh_signals(load_watchlist(), cfg, prev, QUIVER_KEY)
        save_signal_cache(cache)
        return cache
```

- [ ] **Step 4: Add cron route to `app.py`**

After the last existing `@app.route("/api/cron/...")` route, add:

```python
@app.route("/api/cron/signals", methods=["GET"])
def cron_signals():
    """Daily 09:00 ET — refresh all signal sources."""
    if not _verify_cron(request):
        return jsonify({"error": "unauthorized"}), 401
    try:
        cfg   = load_signal_config()
        prev  = load_signal_cache()
        cache = _refresh_signals(load_watchlist(), cfg, prev, QUIVER_KEY)
        save_signal_cache(cache)
        counts = {
            "watchlist_with_signals": len(cache.get("watchlist_signals", {})),
            "untracked":              len(cache.get("untracked_signals", [])),
            "partial":                cache.get("partial", False),
        }
        return jsonify({"ok": True, "data": counts})
    except Exception as e:
        _logging.getLogger(__name__).error("cron_signals failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 200  # 200 to suppress Vercel retry
```

- [ ] **Step 5: Add cron entry to `vercel.json`**

Replace the entire contents of `vercel.json` with:

```json
{
  "version": 2,
  "builds": [
    {
      "src": "app.py",
      "use": "@vercel/python",
      "config": { "maxDuration": 120 }
    }
  ],
  "routes": [
    {
      "src": "/(.*)",
      "dest": "app.py"
    }
  ],
  "crons": [
    {"path": "/api/cron/signals", "schedule": "0 13 * * 1-5"}
  ]
}
```

- [ ] **Step 6: Verify app starts without errors**

```bash
cd "/Users/shuning.wang/Library/CloudStorage/GoogleDrive-shuning2016@gmail.com/My Drive/My Projects/Python projects/StockTraderPy"
python -c "import app; print('app imports OK')"
```

Expected: `app imports OK`

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASSED

- [ ] **Step 8: Commit**

```bash
git add app.py config.py vercel.json
git commit -m "feat: add signal storage, dispatch cases, and daily cron to app.py"
```

---

## Task 6: `index.html` — load signal data on startup + sidebar dots

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add `signalCache` / `signalConfig` globals and loader**

In `index.html`, find the watchlist globals line (line 537):
```javascript
let stocks=[], activeKey=null, quoteCache={}, newsCache={}, aiCache={}, earningsCache={};
```

Add after it:
```javascript
let signalCache={watchlist_signals:{},untracked_signals:[],fetched_at:'',partial:false};
let signalConfig={politicians:[],ark_funds:[]};
```

Find `loadStocks()` function (line 539). After the closing `}` of `loadStocks`, add:

```javascript
async function loadSignalData(){
  try{
    const[cache,cfg]=await Promise.all([
      api('getSignalCache'),
      api('getSignalConfig'),
    ]);
    if(cache&&typeof cache==='object'){signalCache=cache;}
    if(cfg&&typeof cfg==='object'){signalConfig=cfg;}
    renderLists();
    renderSignalBadgeCount();
  }catch(e){}
}
```

- [ ] **Step 2: Add CSS for signal dots**

Find the `.q-tag` CSS line (around line 103):
```css
.q-tag{font-size:11px;color:var(--t3);background:var(--bg);padding:2px 7px;border-radius:4px}
```

Add after it:
```css
.sig-dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0}
.sig-dot.insider{background:#f59e0b}
.sig-dot.politician{background:#a855f7}
.sig-dot.ark{background:#22c55e}
.sig-dots{display:flex;gap:2px;align-items:center}
```

- [ ] **Step 3: Add signal dots to `renderOneList` (line 597)**

Replace the current `renderOneList` function:

```javascript
function renderOneList(type){
  const el=document.getElementById(type+'-list');
  const sub=stocks.filter(s=>s.type===type);
  if(!sub.length){el.innerHTML=`<div class="empty-sm">暂无${type==='stock'?'股票':'Coin'}</div>`;return;}
  el.innerHTML=sub.map(s=>{
    const key=itemKey(s.symbol,s.type),act=activeKey===key?' active':'';
    const dots=buildSigDots(s.symbol,s.type);
    return`<div class="item-row${type==='crypto'?' crypto':''}${act}" onclick="setActive('${key}')">
      <span class="isym${type==='crypto'?' crypto':''}">${s.symbol}</span>
      ${dots?`<div class="sig-dots">${dots}</div>`:''}
      <button class="del-btn" onclick="event.stopPropagation();deleteItem('${s.symbol}','${type}')">×</button>
    </div>`;
  }).join('');
}

function buildSigDots(sym,type){
  if(type!=='stock')return'';
  const sigs=signalCache.watchlist_signals[sym.toUpperCase()]||[];
  if(!sigs.length)return'';
  const types=new Set(sigs.map(s=>s.type));
  let dots='';
  if(types.has('insider'))   dots+=`<span class="sig-dot insider"  title="内部人士信号"></span>`;
  if(types.has('politician'))dots+=`<span class="sig-dot politician" title="政客信号"></span>`;
  if(types.has('ark'))       dots+=`<span class="sig-dot ark"       title="ARK信号"></span>`;
  return dots;
}
```

- [ ] **Step 4: Call `loadSignalData()` on page load**

Find the `window.addEventListener('load', ...)` or the equivalent init call at the bottom of the script section of `index.html`. Add `loadSignalData()` after `loadStocks()` in the initialization:

Search for `loadStocks()` in the init block. It will look something like:
```javascript
loadStocks();
```

Change it to:
```javascript
loadStocks();
loadSignalData();
```

- [ ] **Step 5: Verify sidebar dots render**

Start the Flask dev server, open the app, add a stock — the sidebar item should appear with no dots (signal cache is empty until first refresh). No JS errors in console.

```bash
cd "/Users/shuning.wang/Library/CloudStorage/GoogleDrive-shuning2016@gmail.com/My Drive/My Projects/Python projects/StockTraderPy"
python app.py
```

Open http://localhost:5000 in browser. Check browser console for errors.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat: add signal dot indicators to watchlist sidebar"
```

---

## Task 7: `index.html` — stock card signal panel

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add CSS for signal panel**

After the `.sig-dots` CSS added in Task 6, add:

```css
.sig-panel{border-top:1px solid var(--bdr);padding-top:10px;margin-top:10px}
.sig-panel-title{font-size:11px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.sig-row{padding:7px 10px;border-radius:0 6px 6px 0;margin-bottom:5px;font-size:12px;line-height:1.5}
.sig-row.insider{background:var(--amber-l,#fffbeb);border-left:3px solid #f59e0b}
.sig-row.politician{background:var(--purple-l,#fdf4ff);border-left:3px solid #a855f7}
.sig-row.ark{background:var(--green-l,#f0fdf4);border-left:3px solid #22c55e}
.sig-row-who{font-weight:600;font-size:11px}
.sig-row-who.insider{color:#b45309}
.sig-row-who.politician{color:#7e22ce}
.sig-row-who.ark{color:#15803d}
.sig-action-badge{font-size:10px;font-weight:700;padding:0 5px;border-radius:3px;margin-left:5px}
.sig-action-badge.buy{background:#dcfce7;color:#15803d}
.sig-action-badge.sell{background:#fee2e2;color:#991b1b}
```

- [ ] **Step 2: Add `buildSignalPanel` helper function**

Add after `buildSigDots()` from Task 6:

```javascript
function buildSignalPanel(sym){
  const sigs=(signalCache.watchlist_signals||{})[sym.toUpperCase()];
  if(!sigs||!sigs.length)return'';
  const rows=sigs.map(s=>{
    const badge=`<span class="sig-action-badge ${s.action}">${s.action==='buy'?'BUY':'SELL'}</span>`;
    let detail='';
    if(s.type==='insider'){
      const amt=s.amount?`$${(s.amount/1e6).toFixed(1)}M`:'';
      detail=`${s.shares?s.shares.toLocaleString()+'股':''} ${amt}`.trim();
    }else if(s.type==='politician'){
      detail=s.amount_range||'';
    }else if(s.type==='ark'){
      detail=s.shares?s.shares.toLocaleString()+'股 净变动':'';
    }
    return`<div class="sig-row ${s.type}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span class="sig-row-who ${s.type}">${s.type==='insider'?'🏦 ':s.type==='politician'?'🏛️ ':'🦆 '}${esc(s.who||s.fund||'')}${s.role?` · ${esc(s.role)}`:''}${badge}</span>
        <span style="font-size:10px;color:var(--t3)">${s.date||''}</span>
      </div>
      ${detail?`<div style="margin-top:2px;color:var(--t2)">${esc(detail)}</div>`:''}
    </div>`;
  }).join('');
  return`<div class="sig-panel"><div class="sig-panel-title">📡 信号追踪</div>${rows}</div>`;
}
```

- [ ] **Step 3: Inject signal panel into `buildCard()`**

Find the last line of `buildCard()` (line 673):
```javascript
  return`<div class="qcard${ic?' crypto':''}" id="card-${key}">${qHead}<div class="qcard-actions">...${aiHtml}${newsHtml}</div>`;
```

Replace it with:
```javascript
  const sigHtml=!ic?buildSignalPanel(item.symbol):'';
  return`<div class="qcard${ic?' crypto':''}" id="card-${key}">${qHead}<div class="qcard-actions"><button class="btn btn-p btn-sm" onclick="queryItem('${item.symbol}','${item.type}')" id="qbtn-${key}">🔍 查询行情 &amp; 新闻</button><button class="ai-btn" onclick="analyzeItem('${item.symbol}','${item.type}')" id="ai-btn-${key}">🤖 AI 分析</button></div>${aiHtml}${newsHtml}${sigHtml}</div>`;
```

- [ ] **Step 4: Verify signal panel renders**

Run app, click a stock in the sidebar. The card should show (if no signals in cache) no signal panel. Manually test by inserting a fake entry in the browser console:

```javascript
signalCache.watchlist_signals['NVDA'] = [{
  type:'insider', who:'Jensen Huang', role:'CEO', action:'buy',
  amount:2500000, shares:20000, date:'2026-05-01', filing_date:'2026-05-03', is_plan:false
}];
setActive('NVDA:stock');
```

Expected: signal panel appears in the NVDA card with amber left border showing Jensen Huang's buy.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: add signal panel to stock card (buildCard)"
```

---

## Task 8: `index.html` — 信号追踪 tab (full tab + JS)

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add tab button to the tabs bar**

Find the tabs bar (line 353–360):
```html
    <div class="tabs">
      <div class="tab active" id="tab-news"     onclick="switchTab('news')">行情 &amp; 新闻</div>
      <div class="tab"        id="tab-trade"    onclick="switchTab('trade')">模拟交易</div>
      <div class="tab"        id="tab-messages" onclick="switchTab('messages')">交易记录</div>
      <div class="tab"        id="tab-daily"    onclick="switchTab('daily')">📋 日检</div>
      <div class="tab"        id="tab-weekly"   onclick="switchTab('weekly')">周报 &amp; 建议</div>
      <div class="tab"        id="tab-guardian" onclick="switchTab('guardian')">🛡️ 心跳</div>
    </div>
```

Replace with:
```html
    <div class="tabs">
      <div class="tab active" id="tab-news"     onclick="switchTab('news')">行情 &amp; 新闻</div>
      <div class="tab"        id="tab-trade"    onclick="switchTab('trade')">模拟交易</div>
      <div class="tab"        id="tab-messages" onclick="switchTab('messages')">交易记录</div>
      <div class="tab"        id="tab-signals"  onclick="switchTab('signals')">📡 信号追踪</div>
      <div class="tab"        id="tab-daily"    onclick="switchTab('daily')">📋 日检</div>
      <div class="tab"        id="tab-weekly"   onclick="switchTab('weekly')">周报 &amp; 建议</div>
      <div class="tab"        id="tab-guardian" onclick="switchTab('guardian')">🛡️ 心跳</div>
    </div>
```

- [ ] **Step 2: Update `switchTab()` to include 'signals'**

Find the `switchTab` function (line 717):
```javascript
function switchTab(n){
  ['news','trade','messages','daily','weekly','guardian'].forEach(t=>{
```

Replace the array with:
```javascript
function switchTab(n){
  ['news','trade','messages','signals','daily','weekly','guardian'].forEach(t=>{
```

- [ ] **Step 3: Add the 信号追踪 panel HTML**

Find the `<!-- Daily Review panel -->` comment (around line 402). Insert the following **before** it:

```html
    <!-- Signals panel -->
    <div id="panel-signals" style="display:none">
      <div class="wr-sub-tabs">
        <button class="wr-sub-btn active" id="sigtab-settings"   onclick="switchSigTab('settings')">⚙️ 设置</button>
        <button class="wr-sub-btn"        id="sigtab-untracked"  onclick="switchSigTab('untracked')">🔔 未追踪信号 <span id="sig-untracked-count" style="display:none;background:#ef4444;color:#fff;border-radius:8px;padding:0 5px;font-size:10px;margin-left:3px"></span></button>
        <button class="wr-sub-btn"        id="sigtab-all"        onclick="switchSigTab('all')">📋 全部信号</button>
      </div>

      <!-- Settings sub-panel -->
      <div id="sigpanel-settings">
        <div style="background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:10px">
          <div class="ts-label" style="margin-bottom:8px">🏛️ 关注的政客</div>
          <div id="pol-tags" style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px"></div>
          <div style="display:flex;gap:6px">
            <input id="pol-inp" type="text" placeholder="输入政客名字，回车添加…"
                   style="border:1px solid var(--bdr);border-radius:6px;padding:5px 10px;font-size:12px;background:var(--card);color:var(--t);flex:1"
                   onkeydown="if(event.key==='Enter')addPolitician()">
            <button class="btn btn-sm btn-o" onclick="addPolitician()">添加</button>
          </div>
        </div>
        <div style="background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:10px">
          <div class="ts-label" style="margin-bottom:8px">🦆 关注的 ARK 基金</div>
          <div id="ark-chips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
        </div>
        <div class="wr-run-bar">
          <button class="btn btn-sm btn-p" onclick="triggerRefreshSignals()" id="btn-refresh-signals">🔄 立即刷新信号</button>
          <span class="wr-meta-txt" id="sig-meta">每周一至五 09:00 ET 自动运行</span>
        </div>
      </div>

      <!-- Untracked sub-panel -->
      <div id="sigpanel-untracked" style="display:none">
        <div id="sig-untracked-list"><div class="empty-panel">暂无未追踪信号 — 运行刷新后显示</div></div>
      </div>

      <!-- All signals sub-panel -->
      <div id="sigpanel-all" style="display:none">
        <div id="sig-all-list"><div class="empty-panel">暂无信号数据 — 运行刷新后显示</div></div>
      </div>
    </div>
```

- [ ] **Step 4: Add signal tab JS functions**

Find the `// ═══ TAB SWITCHING ═══` comment (line 713) and add the following **after** the `switchTab` function's closing `}`:

```javascript
// ═══════════════════════════════════════════════════════════════
//  SIGNAL TRACKING TAB
// ═══════════════════════════════════════════════════════════════
const ARK_ALL_FUNDS=['ARKK','ARKW','ARKQ','ARKG','ARKF','ARKX'];

function switchSigTab(n){
  ['settings','untracked','all'].forEach(t=>{
    document.getElementById('sigpanel-'+t).style.display=t===n?'':'none';
    document.getElementById('sigtab-'+t).className='wr-sub-btn'+(t===n?' active':'');
  });
}

function renderSignalBadgeCount(){
  const n=(signalCache.untracked_signals||[]).length;
  const el=document.getElementById('sig-untracked-count');
  if(!el)return;
  if(n>0){el.textContent=n;el.style.display='';}else{el.style.display='none';}
}

function renderSigSettings(){
  // Politician tags
  const polEl=document.getElementById('pol-tags');
  if(polEl){
    const pols=signalConfig.politicians||[];
    polEl.innerHTML=pols.map(p=>
      `<span style="background:var(--blue-l);color:var(--blue);padding:3px 10px;border-radius:12px;font-size:12px;cursor:pointer"
             onclick="removePolitician('${esc(p)}')">${esc(p)} <span style="opacity:.6">×</span></span>`
    ).join('');
  }
  // ARK chips
  const arkEl=document.getElementById('ark-chips');
  if(arkEl){
    const sel=new Set(signalConfig.ark_funds||[]);
    arkEl.innerHTML=ARK_ALL_FUNDS.map(f=>{
      const on=sel.has(f);
      return`<span style="padding:3px 12px;border-radius:12px;font-size:12px;cursor:pointer;
                         background:${on?'var(--green-l,#dcfce7)':'var(--bg)'};
                         color:${on?'#15803d':'var(--t3)'};
                         border:1px solid ${on?'#22c55e':'var(--bdr)'}"
                   onclick="toggleArk('${f}')">${on?'✓ ':''}${f}</span>`;
    }).join('');
  }
  // Meta timestamp
  const metaEl=document.getElementById('sig-meta');
  if(metaEl&&signalCache.fetched_at){
    metaEl.textContent=`上次更新: ${signalCache.fetched_at.replace('T',' ')} UTC · 每周一至五 09:00 ET 自动运行`+
      (signalCache.partial?' ⚠️ 部分数据':'');
  }
}

function renderSigUntracked(){
  const el=document.getElementById('sig-untracked-list');
  if(!el)return;
  const list=signalCache.untracked_signals||[];
  if(!list.length){el.innerHTML='<div class="empty-panel">暂无未追踪信号</div>';return;}
  el.innerHTML=list.map(s=>{
    const typeBg=s.type==='insider'?'#fffbeb':s.type==='politician'?'#fdf4ff':'#f0fdf4';
    const typeColor=s.type==='insider'?'#b45309':s.type==='politician'?'#7e22ce':'#15803d';
    const typeLabel=s.type==='insider'?'🏦 Insider':s.type==='politician'?'🏛️ '+esc(s.who||''):'🦆 '+esc(s.fund||'');
    const badge=`<span class="sig-action-badge ${s.action}">${s.action==='buy'?'BUY':'SELL'}</span>`;
    const detail=s.amount?`$${(s.amount/1e6).toFixed(1)}M`:s.amount_range||'';
    return`<div style="display:flex;align-items:center;justify-content:space-between;
                       padding:9px 10px;background:var(--card);border:1px solid var(--bdr);
                       border-radius:6px;margin-bottom:5px;font-size:12px">
      <div>
        <span style="font-weight:700;color:var(--blue)">${esc(s.sym)}</span>
        <span style="background:${typeBg};color:${typeColor};font-size:10px;padding:1px 6px;border-radius:3px;margin-left:6px">${typeLabel}</span>
        ${badge}
        <span style="margin-left:8px;color:var(--t2)">${esc(detail)} · ${s.date||''}</span>
      </div>
      <button class="btn btn-sm btn-o" onclick="addFromSignal('${esc(s.sym)}')">+ Watchlist</button>
    </div>`;
  }).join('');
}

function renderSigAll(){
  const el=document.getElementById('sig-all-list');
  if(!el)return;
  const wl=signalCache.watchlist_signals||{};
  const all=Object.entries(wl).flatMap(([sym,sigs])=>sigs.map(s=>({...s,sym})));
  all.sort((a,b)=>(b.date||'').localeCompare(a.date||''));
  if(!all.length){el.innerHTML='<div class="empty-panel">暂无信号数据</div>';return;}
  el.innerHTML=all.map(s=>{
    const typeLabel=s.type==='insider'?'🏦 '+esc(s.who||''):s.type==='politician'?'🏛️ '+esc(s.who||''):'🦆 '+esc(s.fund||'');
    const badge=`<span class="sig-action-badge ${s.action}">${s.action==='buy'?'BUY':'SELL'}</span>`;
    const detail=s.amount?`$${(s.amount/1e6).toFixed(1)}M`:s.amount_range||(s.shares?s.shares.toLocaleString()+'股':'');
    return`<div style="padding:8px 10px;background:var(--card);border:1px solid var(--bdr);
                       border-radius:6px;margin-bottom:5px;font-size:12px;display:flex;
                       justify-content:space-between;align-items:center">
      <div>
        <span style="font-weight:700;color:var(--blue)">${esc(s.sym)}</span>
        <span style="margin-left:8px;color:var(--t2)">${typeLabel}</span>
        ${badge}
        <span style="margin-left:6px;color:var(--t3)">${esc(detail)} · ${s.date||''}</span>
      </div>
    </div>`;
  }).join('');
}

function addPolitician(){
  const inp=document.getElementById('pol-inp');
  const name=inp.value.trim();
  if(!name)return;
  const pols=signalConfig.politicians||[];
  if(pols.includes(name)){toast('已在列表中','err');return;}
  signalConfig.politicians=[...pols,name];
  inp.value='';
  saveSigConfig();
  renderSigSettings();
}
function removePolitician(name){
  signalConfig.politicians=(signalConfig.politicians||[]).filter(p=>p!==name);
  saveSigConfig();
  renderSigSettings();
}
function toggleArk(fund){
  const sel=new Set(signalConfig.ark_funds||[]);
  if(sel.has(fund))sel.delete(fund);else sel.add(fund);
  signalConfig.ark_funds=[...sel];
  saveSigConfig();
  renderSigSettings();
}
async function saveSigConfig(){
  try{await api('saveSignalConfig',{config:signalConfig});}catch(e){}
}
async function triggerRefreshSignals(){
  const btn=document.getElementById('btn-refresh-signals');
  if(btn){btn.disabled=true;btn.innerHTML='<span class="sp"></span> 刷新中…';}
  try{
    const cache=await api('refreshSignals');
    if(cache&&typeof cache==='object'){
      signalCache=cache;
      renderLists();
      renderSignalBadgeCount();
      renderSigSettings();
      renderSigUntracked();
      renderSigAll();
      toast('信号已刷新');
    }
  }catch(e){toast('刷新失败: '+e.message,'err');}
  if(btn){btn.disabled=false;btn.innerHTML='🔄 立即刷新信号';}
}
async function addFromSignal(sym){
  if(stocks.find(s=>s.symbol===sym&&s.type==='stock')){toast('已在列表中','err');return;}
  stocks.push({symbol:sym,type:'stock'});
  await persistStocks();
  renderLists();renderChips();
  toast(sym+' 已加入 Watchlist');
  renderSigUntracked();  // re-render to reflect the stock is now tracked
}

// Re-render signal panels when switching to signals tab
const _origSwitchTab=switchTab;
function switchTab(n){
  _origSwitchTab(n);
  if(n==='signals'){
    renderSigSettings();
    renderSigUntracked();
    renderSigAll();
  }
}
```

- [ ] **Step 5: Verify the tab renders correctly**

Run `python app.py`, open http://localhost:5000, click "📡 信号追踪" tab.
Expected:
- ⚙️ 设置 sub-panel visible with empty politician list and ARK chips (all unselected)
- No JS errors in browser console
- Click 🔔 未追踪信号 → shows empty panel message
- Add a politician name → tag appears, click × removes it
- Toggle ARKK chip → turns green

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat: add 信号追踪 tab with settings, untracked signals, and all-signals panels"
```

---

## Final Verification

- [ ] **Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASSED

- [ ] **End-to-end smoke test**

1. Start app: `python app.py`
2. Add NVDA to watchlist
3. Open 信号追踪 tab → add "Nancy Pelosi" as politician, enable ARKK
4. Click 🔄 立即刷新信号 (will hit real APIs — needs QUIVER_KEY set in .env)
5. If QUIVER_KEY not set: verify graceful empty result, no crash
6. Check sidebar — if any signals returned, colored dots appear next to NVDA
7. Click NVDA → signal panel shows at bottom of card

- [ ] **Verify strategy code untouched**

```bash
git diff main..HEAD -- strategy_v6.py
```

Expected: empty output (no changes to strategy_v6.py)
