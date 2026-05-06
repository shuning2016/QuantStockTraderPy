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
