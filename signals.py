"""
signals.py — Signal tracking: insider trades, politician trades, ARK fund trades.

Pure fetch/compute functions only.  No storage, no imports from app.py.
Storage (load_signal_config, save_signal_config, load_signal_cache, save_signal_cache)
lives in app.py to avoid circular imports.
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger("quant.signals")

# ── ARK fund trades API (arkfunds.io) ─────────────────────────────────────────
# Returns recent buy/sell trades per fund; avoids Cloudflare-blocked CSV URLs.
_ARKFUNDS_TRADES_URL = "https://arkfunds.io/api/v2/etf/trades?symbol={fund}&period=5d"

VALID_ARK_FUNDS = {"ARKK", "ARKW", "ARKQ", "ARKG", "ARKF", "ARKX"}

# ── Fund manager 13F registry ─────────────────────────────────────────────────
# Maps display name → {cik (10-digit zero-padded), fund name}
# Verified via data.sec.gov/submissions May 2026
FUND_MANAGER_REGISTRY: dict[str, dict] = {
    "Michael Burry":         {"cik": "0001649339", "fund": "Scion Asset Management"},
    "Carl Icahn":            {"cik": "0000921669", "fund": "Icahn Capital"},
    "Bill Ackman":           {"cik": "0001336528", "fund": "Pershing Square"},
    "Stanley Druckenmiller": {"cik": "0001536411", "fund": "Duquesne Family Office"},
    "Warren Buffett":        {"cik": "0001067983", "fund": "Berkshire Hathaway"},
    "George Soros":          {"cik": "0001029160", "fund": "Soros Fund Management"},
}

# ── EDGAR 13F URLs ────────────────────────────────────────────────────────────
_EDGAR_13F_TABLE_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/infotable.xml"
)

_13F_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _parse_13f_infotable(xml_text: str) -> dict[str, dict]:
    """
    Parse a 13F-HR infotable.xml into {cusip: {issuer, shares, value}}.
    Value is in dollars as reported. Returns {} on any parse error.
    """
    holdings: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("_parse_13f_infotable: XML parse error: %s", e)
        return {}

    ns = {"t": _13F_NS}
    for entry in root.findall("t:infoTable", ns):
        cusip_el  = entry.find("t:cusip", ns)
        issuer_el = entry.find("t:nameOfIssuer", ns)
        shares_el = entry.find(".//t:sshPrnamt", ns)
        value_el  = entry.find("t:value", ns)

        if cusip_el is None or issuer_el is None:
            continue

        cusip = (cusip_el.text or "").strip()
        if not cusip:
            continue

        try:
            shares = int(shares_el.text or "0") if shares_el is not None else 0
            value  = int(value_el.text  or "0") if value_el  is not None else 0
        except ValueError:
            continue

        holdings[cusip] = {
            "issuer": (issuer_el.text or "").strip(),
            "shares": shares,
            "value":  value,
        }

    return holdings


# ── SEC EDGAR Form 4 (insider trades, ~2 business day delay) ─────────────────
_EDGAR_UA          = "StockTrader/1.0 shuning.wang@shopee.com"
_EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_SUBS_URL    = "https://data.sec.gov/submissions/CIK{cik}.json"
_EDGAR_FORM4_URL   = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/form4.xml"

_ticker_cik_cache: dict[str, int] = {}


def _get_ticker_cik_map() -> dict[str, int]:
    """Fetch and in-process-cache the SEC ticker → CIK mapping."""
    global _ticker_cik_cache
    if _ticker_cik_cache:
        return _ticker_cik_cache
    try:
        resp = requests.get(_EDGAR_TICKERS_URL,
                            headers={"User-Agent": _EDGAR_UA}, timeout=15)
        resp.raise_for_status()
        _ticker_cik_cache = {
            v["ticker"].upper(): v["cik_str"]
            for v in resp.json().values()
        }
        logger.info("Loaded %d tickers from SEC EDGAR", len(_ticker_cik_cache))
    except Exception as e:
        logger.warning("_get_ticker_cik_map failed: %s", e)
    return _ticker_cik_cache


def _parse_form4_xml(xml_text: str, filing_date: str) -> list[dict]:
    """Parse a Form 4 XML document into a list of insider trade signals."""
    signals = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("_parse_form4_xml: XML parse error: %s", e)
        return []

    owner_el   = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
    owner_name = (owner_el.text or "").strip() if owner_el is not None else ""

    title_el = root.find(".//reportingOwnerRelationship/officerTitle")
    title    = (title_el.text or "").strip() if title_el is not None else ""
    if not title:
        is_dir = root.find(".//reportingOwnerRelationship/isDirector")
        if is_dir is not None and is_dir.text == "1":
            title = "Director"

    period_el  = root.find("periodOfReport")
    trade_date = (period_el.text or filing_date).strip() if period_el is not None else filing_date

    is_plan = (root.findtext("aff10b5One") or "false").lower() == "true"

    for tx in root.findall(".//nonDerivativeTransaction"):
        code_el = tx.find(".//transactionCode")
        code    = (code_el.text or "").strip() if code_el is not None else ""
        if code == "P":
            action = "buy"
        elif code == "S":
            action = "sell"
        else:
            continue  # skip option exercises, gifts, tax withholding, etc.

        shares_el = tx.find(".//transactionShares/value")
        shares    = int(float(shares_el.text or "0")) if shares_el is not None else 0

        price_el = tx.find(".//transactionPricePerShare/value")
        price    = float(price_el.text or "0") if price_el is not None else 0.0

        amount = int(shares * price)
        if amount < 100_000:  # skip tiny trades (< $100K)
            continue

        signals.append({
            "type":        "insider",
            "who":         owner_name,
            "role":        title,
            "action":      action,
            "amount":      amount,
            "shares":      shares,
            "date":        trade_date,
            "filing_date": filing_date,
            "is_plan":     is_plan,
        })
    return signals


def fetch_insider_trades(symbols: list[str]) -> dict[str, list[dict]]:
    """
    Fetch recent insider trades for the given symbols from SEC EDGAR Form 4.
    ~2 business day delay; free, no API key required.

    Returns {sym: [signal, ...]} where each signal is:
      {"type": "insider", "who": str, "role": str, "action": "buy"|"sell",
       "amount": int, "shares": int, "date": str, "filing_date": str, "is_plan": bool}
    """
    cutoff  = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    cik_map = _get_ticker_cik_map()
    result: dict[str, list[dict]] = {}

    for sym in symbols:
        cik = cik_map.get(sym.upper())
        if not cik:
            logger.warning("fetch_insider_trades: no CIK for %s", sym)
            continue

        cik_padded = str(cik).zfill(10)
        try:
            resp = requests.get(
                _EDGAR_SUBS_URL.format(cik=cik_padded),
                headers={"User-Agent": _EDGAR_UA}, timeout=15,
            )
            resp.raise_for_status()
            recent = resp.json().get("filings", {}).get("recent", {})
        except Exception as e:
            logger.warning("fetch_insider_trades(%s): submissions failed: %s", sym, e)
            time.sleep(0.1)
            continue

        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])

        sym_signals: list[dict] = []
        for i, form in enumerate(forms):
            if form != "4":
                continue
            if dates[i] < cutoff:
                break  # filings are reverse-chronological, safe to stop

            acc_clean = accessions[i].replace("-", "")
            xml_url   = _EDGAR_FORM4_URL.format(cik=cik, acc=acc_clean)
            try:
                time.sleep(0.12)  # EDGAR asks ≤10 req/s
                r4 = requests.get(xml_url, headers={"User-Agent": _EDGAR_UA}, timeout=10)
                r4.raise_for_status()
                sym_signals.extend(_parse_form4_xml(r4.text, dates[i]))
            except Exception as e:
                logger.warning("fetch_insider_trades(%s): form4 fetch failed: %s", sym, e)

        if sym_signals:
            result[sym.upper()] = sym_signals
        time.sleep(0.1)

    return result


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


def fetch_ark_trades(
    funds: list[str],
    watchlist: list[str],
    prev_ark_holdings: Optional[dict] = None,  # kept for API compat, unused
) -> tuple[dict[str, list[dict]], list[dict], dict]:
    """
    Fetch recent ARK fund trades from arkfunds.io (last 5 trading days).

    Returns:
        watchlist_matches: {sym: [signal, ...]} for watchlist symbols
        untracked_list:    [signal] for symbols NOT in watchlist
        today_holdings:    {} (holdings diff no longer needed)

    Each signal:
        {"type": "ark", "fund": str, "action": "buy"|"sell",
         "shares": int, "date": str, "sym": str}
    """
    watchlist_upper = {s.upper() for s in watchlist}
    watchlist_matches: dict[str, list[dict]] = {}
    untracked_list: list[dict] = []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    for fund in funds:
        fund_upper = fund.upper()
        if fund_upper not in VALID_ARK_FUNDS:
            logger.warning("fetch_ark_trades: unknown fund %s", fund)
            continue
        try:
            url = _ARKFUNDS_TRADES_URL.format(fund=fund_upper)
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("fetch_ark_trades(%s) failed: %s", fund, e)
            continue

        for trade in data.get("trades", []):
            ticker = (trade.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            date = (trade.get("date") or "")[:10]
            if date < cutoff:
                continue
            direction = (trade.get("direction") or "").lower()
            if direction == "buy":
                action = "buy"
            elif direction == "sell":
                action = "sell"
            else:
                continue

            signal: dict = {
                "type":   "ark",
                "fund":   fund_upper,
                "action": action,
                "shares": int(trade.get("shares") or 0),
                "date":   date,
                "sym":    ticker,
            }
            if ticker in watchlist_upper:
                watchlist_matches.setdefault(ticker, []).append(signal)
            else:
                untracked_list.append(signal)

        time.sleep(0.3)

    return watchlist_matches, untracked_list, {}


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
