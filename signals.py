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
