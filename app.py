"""
Quant AI Stock Trader — Python Flask Backend
Replaces Google Apps Script backend.
Maintains identical API surface consumed by the frontend.
"""

import os, json, time, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    # Python < 3.9: approximate DST by checking if local clock is in summer.
    import time as _time
    from datetime import timezone as _tz, timedelta
    _is_dst = bool(getattr(_time, "daylight", 0) and _time.localtime().tm_isdst > 0)
    _ET = _tz(timedelta(hours=-4 if _is_dst else -5))
import requests
from flask import Flask, request, jsonify, render_template, send_from_directory, Response

# ─── Config (API keys, model names, cache TTLs) ─────────────────
from config import (
    FINNHUB_KEY, NEWSAPI_KEY, CLAUDE_KEY, GROK_KEY, DEEPSEEK_KEY,
    SERPAPI_KEY, QUIVER_KEY, PORT, MODELS, MAX_TOKENS, SESSION_MAX_TOKENS,
    FINNHUB_QUOTE_URL, FINNHUB_NEWS_URL, COINGECKO_COIN_URL, NEWSAPI_URL,
    PRICE_CACHE_TTL, CRYPTO_CACHE_TTL, NEWS_CACHE_TTL,
    check_config,
)
from strategy_v6 import (
    CFG, new_trade_state, calc_nav, get_quant_metrics,
    build_prompt_v6, parse_ai_decisions, parse_confidence_score,
    parse_regime_from_text, parse_atr_from_text,
    execute_decisions, check_operating_rules, get_market_regime,
    check_auto_stop_rules, build_trade_log_entry,
)
from signals import refresh_signals as _refresh_signals
from weekly_review import (
    most_recent_week,
    run_weekend_feedback,
    run_watchlist_suggestions,
)
from daily_review import run_daily_review

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.urandom(24)

# ─── Logging setup ────────────────────────────────────────────────
# Vercel captures stdout/stderr — use INFO level so debug lines appear
# in the Vercel Functions log viewer (dashboard → Deployments → Functions)
import logging as _logging
_logging.basicConfig(
    level=_logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_logging.getLogger("quant.session").setLevel(_logging.DEBUG)

# ─── Startup config check ────────────────────────────────────────
for _w in check_config():
    _logging.getLogger(__name__).warning("CONFIG: %s", _w)

# ─── Storage root ─────────────────────────────────────────────────
# Vercel (and most serverless platforms) have a read-only filesystem
# except for /tmp.  We use /tmp when the normal "data/" directory
# cannot be created, so the same code works locally AND on Vercel.
def _make_storage_root() -> Path:
    """
    Return the writable storage root directory.

    Strategy (in order):
    1. If VERCEL env var is set → always use /tmp  (Vercel sets this automatically)
    2. If any other read-only platform signal is present → use /tmp
    3. Otherwise try to write to a local ./data folder (normal local dev)
    4. If that write fails (read-only OS mount) → fall back to /tmp
    """
    tmp = Path("/tmp/quant_trader_data")

    # Vercel sets VERCEL=1 in all serverless functions
    # AWS Lambda sets AWS_LAMBDA_FUNCTION_NAME
    # Railway / Render also have a read-only project root
    serverless_signals = ["VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "LAMBDA_TASK_ROOT"]
    if any(os.environ.get(sig) for sig in serverless_signals):
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

    # Local dev: try ./data first
    local = Path("data")
    try:
        local.mkdir(exist_ok=True)
        test = local / ".write_test"
        test.write_text("ok")
        test.unlink()
        return local
    except OSError:
        # OS-level read-only mount (some CI / container environments)
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

DATA_DIR  = _make_storage_root()
LOGS_DIR  = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Data persistence (JSON files, equivalent to GAS PropertiesService) ──
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
SIGNAL_CONFIG_FILE = DATA_DIR / "signal_config.json"
SIGNAL_CACHE_FILE  = DATA_DIR / "signal_cache.json"
STATE_DIR      = DATA_DIR / "trade_states"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Vercel KV / Upstash Redis — persistent storage ──────────────
# Vercel's /tmp is ephemeral: each serverless invocation may get a
# fresh container, so cron-written logs are invisible to UI requests.
# Solution: use Upstash Redis (via Vercel Marketplace) for persistent log + state storage.
#
# Setup (one-time):
#   1. Vercel Dashboard → Storage → Upstash → Create Redis database (free tier)
#   2. Connect it to this project — env vars are injected automatically:
#      UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
#   Also supports legacy Vercel KV env vars: KV_REST_API_URL / KV_REST_API_TOKEN
#
# Without Redis, the app falls back to /tmp (fine for local dev, broken on Vercel cron).

# Accept all known Upstash / Vercel KV env var naming variants.
# Upstash via Vercel Marketplace injects: UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN
# Upstash direct SDK may use:             UPSTASH_REDIS_URL     + UPSTASH_REDIS_TOKEN
# Legacy Vercel KV (deprecated) used:     KV_REST_API_URL       + KV_REST_API_TOKEN
_KV_URL   = (os.environ.get("UPSTASH_REDIS_REST_URL")
             or os.environ.get("UPSTASH_REDIS_URL")
             or os.environ.get("KV_REST_API_URL", "")).strip()
_KV_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN")
             or os.environ.get("UPSTASH_REDIS_TOKEN")
             or os.environ.get("KV_REST_API_TOKEN", "")).strip()
_USE_KV   = bool(_KV_URL and _KV_TOKEN)
_log_kv   = _logging.getLogger("quant.kv")

# Log which env vars were detected to help diagnose misconfiguration
_KV_CANDIDATES = {
    "UPSTASH_REDIS_REST_URL":   bool(os.environ.get("UPSTASH_REDIS_REST_URL")),
    "UPSTASH_REDIS_REST_TOKEN": bool(os.environ.get("UPSTASH_REDIS_REST_TOKEN")),
    "UPSTASH_REDIS_URL":        bool(os.environ.get("UPSTASH_REDIS_URL")),
    "UPSTASH_REDIS_TOKEN":      bool(os.environ.get("UPSTASH_REDIS_TOKEN")),
    "KV_REST_API_URL":          bool(os.environ.get("KV_REST_API_URL")),
    "KV_REST_API_TOKEN":        bool(os.environ.get("KV_REST_API_TOKEN")),
}

if _USE_KV:
    _log_kv.info("Redis/KV storage enabled (%s)", _KV_URL[:60])
else:
    _log_kv.warning(
        "No Redis env vars found — using /tmp (ephemeral on Vercel). "
        "Env var scan: %s. "
        "Fix: Vercel Dashboard → Storage → Upstash → Create Redis → Connect project.",
        _KV_CANDIDATES
    )

def _kv(cmd: list):
    """Execute one Redis command via Upstash REST API. Returns result or None on error."""
    try:
        resp = requests.post(
            _KV_URL,
            headers={"Authorization": f"Bearer {_KV_TOKEN}"},
            json=cmd,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception as e:
        _log_kv.error("KV command %s failed: %s", cmd[0] if cmd else "?", e)
        return None

def _next_ym(ym: str) -> str:
    """Increment a YYYY-MM string by one month."""
    y, m = int(ym[:4]), int(ym[5:7])
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return f"{y:04d}-{m:02d}"

def load_watchlist():
    # Priority 1: Redis/KV (persistent across Vercel cold starts — same store
    # used for trade state and logs).
    if _USE_KV:
        data = _kv(["GET", "watchlist"])
        if data:
            try:
                wl = json.loads(data)
                if isinstance(wl, list) and wl:
                    return wl
            except Exception as e:
                _log_kv.error("KV watchlist parse error: %s", e)

    # Priority 2: local file (works locally and persists across restarts)
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except json.JSONDecodeError as e:
            _logging.getLogger(__name__).error(
                "Watchlist file corrupted — resetting to empty. Error: %s", e)
        except Exception as e:
            _logging.getLogger(__name__).error("Could not read watchlist: %s", e)

    # Priority 3: WATCHLIST_JSON env var (manual fallback)
    env_wl = os.environ.get("WATCHLIST_JSON", "")
    if env_wl:
        try:
            return json.loads(env_wl)
        except Exception:
            pass
    return []

def save_watchlist(stocks):
    # Always persist to Redis/KV first so cron jobs see the latest list even
    # after a Vercel cold start wipes /tmp.
    if _USE_KV:
        _kv(["SET", "watchlist", json.dumps(stocks)])

    # Also write to local file (works for local dev; /tmp on Vercel as backup)
    try:
        WATCHLIST_FILE.write_text(json.dumps(stocks))
    except OSError:
        pass

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

def _state_file(provider: str) -> Path:
    return STATE_DIR / f"state_{provider}.json"

def _json_safe(obj):
    """Serialize non-JSON types cleanly (no silent str() coercion)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

# ─── Trade state ──────────────────────────────────────────────────
def load_trade_state(provider: str) -> dict:
    if _USE_KV:
        data = _kv(["GET", f"tradestate:{provider}"])
        if data:
            try:
                raw = json.loads(data)
                if isinstance(raw, dict):
                    state = raw
                    # Mirror filesystem mode: rebuild state.log from persistent
                    # trade records if the in-memory log is empty (e.g. after a
                    # Vercel cold-start that reset the state but left Redis intact).
                    if not state.get("log"):
                        today = today_et()
                        month = today[:7] if today else ""
                        trade_log = read_log_range(
                            "trades",
                            month + "-01" if month else "2020-01-01",
                            today or "2099-12-31",
                            provider,
                        )
                        if trade_log:
                            for e in trade_log:
                                e["_logged"] = True
                            state["log"] = list(reversed(trade_log))
                            _log_kv.info(
                                "Rebuilt state.log for %s from %d KV trade records",
                                provider, len(trade_log),
                            )
                    return state
            except Exception as e:
                _log_kv.error("KV state parse error for %s: %s", provider, e)
        # State key missing — still try to rebuild log from persistent trades
        state = new_trade_state()
        today = today_et()
        month = today[:7] if today else ""
        trade_log = read_log_range(
            "trades",
            month + "-01" if month else "2020-01-01",
            today or "2099-12-31",
            provider,
        )
        if trade_log:
            for e in trade_log:
                e["_logged"] = True
            state["log"] = list(reversed(trade_log))
            _log_kv.info(
                "Cold-start: rebuilt state.log for %s from %d KV trade records",
                provider, len(trade_log),
            )
        return state

    # ── Filesystem fallback (local dev) ──
    f = _state_file(provider)
    if f.exists():
        try:
            raw = json.loads(f.read_text())
            if not isinstance(raw, dict):
                raise ValueError(f"State file for {provider} is not a dict")
            state = raw
            # Rebuild state.log from JSONL on cold start if log is empty
            if not state.get("log"):
                today = today_et()
                month = today[:7] if today else ""
                trade_log = read_log_range("trades",
                                           month + "-01" if month else "2020-01-01",
                                           today or "2099-12-31",
                                           provider)
                if trade_log:
                    # BUG-2 fix: mark every rebuilt entry as already logged so the
                    # double-write guard in run_trade_session skips them.  Without
                    # this flag the JSONL anti-duplicate check never fires on cold-
                    # start-rebuilt entries, causing every past trade to be appended
                    # again on the next session → inflated P&L and win-rate metrics.
                    for e in trade_log:
                        e["_logged"] = True
                    state["log"] = list(reversed(trade_log))  # oldest first
            return state
        except (json.JSONDecodeError, ValueError) as e:
            _logging.getLogger(__name__).error(
                "State file for %s corrupted — starting fresh. Error: %s", provider, e)
        except Exception as e:
            _logging.getLogger(__name__).error("Could not load state for %s: %s", provider, e)
    return new_trade_state()

def save_trade_state(provider: str, state: dict):
    state_json = json.dumps(state, default=_json_safe)
    if _USE_KV:
        _kv(["SET", f"tradestate:{provider}", state_json])
        return
    _state_file(provider).write_text(state_json)

def reset_trade_state(provider: str) -> dict:
    s = new_trade_state()
    save_trade_state(provider, s)
    return s

# ─── Log files (JSONL) ────────────────────────────────────────────
def _month_log_path(prefix: str, date_str: str = None) -> Path:
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m")
    else:
        date_str = date_str[:7]
    return LOGS_DIR / f"{prefix}_{date_str}.jsonl"

def append_log(prefix: str, entry: dict, date_str: str = None):
    entry_str = json.dumps(entry, default=_json_safe)
    if _USE_KV:
        ym = (date_str[:7] if date_str else datetime.now().strftime("%Y-%m"))
        _kv(["RPUSH", f"log:{prefix}:{ym}", entry_str])
        return
    path = _month_log_path(prefix, date_str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry_str + "\n")

def read_log_range(prefix: str, from_date: str, to_date: str,
                   ai_provider: str = None) -> list:
    if _USE_KV:
        from_ym, to_ym = from_date[:7], to_date[:7]
        results = []
        ym = from_ym
        while ym <= to_ym:
            items = _kv(["LRANGE", f"log:{prefix}:{ym}", "0", "-1"]) or []
            for item in items:
                try:
                    r = json.loads(item)
                    if r.get("date", "") < from_date or r.get("date", "") > to_date:
                        continue
                    if ai_provider and r.get("ai_provider") != ai_provider:
                        continue
                    results.append(r)
                except Exception:
                    pass
            ym = _next_ym(ym)
        return sorted(results, key=lambda x: x.get("timestamp", ""), reverse=True)

    # ── Filesystem fallback (local dev) ──
    from_ym = from_date[:7]
    to_ym   = to_date[:7]
    results = []
    for f in sorted(LOGS_DIR.glob(f"{prefix}_*.jsonl")):
        ym = f.stem.replace(f"{prefix}_", "")
        if ym < from_ym or ym > to_ym:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("date", "") < from_date or r.get("date", "") > to_date:
                    continue
                if ai_provider and r.get("ai_provider") != ai_provider:
                    continue
                results.append(r)
            except Exception:
                pass
    return sorted(results, key=lambda x: x.get("timestamp", ""), reverse=True)

def list_log_files() -> list:
    if _USE_KV:
        return []  # File download not supported via KV; Sessions/Trades tabs work fine
    files = []
    for f in sorted(LOGS_DIR.glob("*.jsonl")):
        stat = f.stat()
        files.append({
            "name": f.name, "id": f.name,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return files

# ─── Time helpers ─────────────────────────────────────────────────


def now_et():
    return datetime.now(_ET)

def today_et():
    return now_et().strftime("%Y-%m-%d")

def session_for_now() -> str:
    """Determine which trading session we are in based on ET time."""
    t = now_et()
    h = t.hour + t.minute / 60
    if h < 9.5:   return "premarket"
    if h < 12.0:  return "opening"
    if h < 15.0:  return "mid"
    return "closing"

# ─── Price feeds ──────────────────────────────────────────────────
_price_cache = {}

def get_stock_quote(sym: str) -> dict:
    cache = _price_cache.get(sym)
    if cache and (time.time() - cache["_ts"]) < PRICE_CACHE_TTL:
        return cache
    _dlog = _logging.getLogger("quant.data")
    try:
        url = f"{FINNHUB_QUOTE_URL}?symbol={sym}&token={FINNHUB_KEY}"
        for _attempt in range(3):
            r = requests.get(url, timeout=8)
            if r.status_code == 429:
                _wait = min(int(r.headers.get("Retry-After", 2 ** _attempt)), 10)
                _dlog.warning("Finnhub quote 429 for %s — retrying in %ds", sym, _wait)
                time.sleep(_wait)
                continue
            break
        if r.status_code != 200:
            _dlog.warning("Finnhub quote %s: HTTP %s", sym, r.status_code)
            return None
        q        = r.json()
        c_price  = q.get("c", 0) or 0
        pc_price = q.get("pc", 0) or 0
        dp       = c_price if c_price else pc_price
        # BUG-1 fix: log a warning when using prev-close as fallback so that
        # stale-price stop evaluations are visible in Vercel function logs.
        if not c_price and pc_price:
            _logging.getLogger("quant.data").warning(
                "%s: Finnhub c=0 (market closed or API glitch) — "
                "using prev_close=%.2f; stop checks may be stale", sym, pc_price)
        res = {"c": dp, "d": q.get("d") or 0.0, "dp": q.get("dp") or 0.0,
               "h": q.get("h") or 0.0, "l": q.get("l") or 0.0,
               "o": q.get("o") or 0.0, "pc": q.get("pc") or 0.0,
               # STRATEGY-1: include current-day volume so watchlist text can show
               # Vol for the AI's ② volume condition check (was missing before)
               "v": q.get("v", 0),
               "isRealtime": bool(c_price), "type": "stock", "_ts": time.time()}
        _price_cache[sym] = res
        return res
    except Exception:
        return None

_atr_cache: dict = {}   # sym → {"atr": float, "_ts": float}
_ATR_CACHE_TTL = 3600  # 1 hour — ATR changes slowly intraday

# ─── Earnings calendar (Finnhub) ─────────────────────────────────
_earnings_cal_cache: dict = {}   # "from:to" → {"ts": float, "data": list}
_EARNINGS_CAL_TTL   = 21600      # 6 hours — earnings dates don't shift intraday

def _fetch_earnings_calendar(from_date: str, to_date: str) -> list:
    """Return raw earningsCalendar list from Finnhub for [from_date, to_date].
    Each entry: {"symbol": str, "date": "YYYY-MM-DD", "hour": "bmo"|"amc"|"dmh"}.
    Results cached for 6 hours. Returns [] on any error.
    """
    key = f"{from_date}:{to_date}"
    cached = _earnings_cal_cache.get(key)
    if cached and (time.time() - cached["ts"]) < _EARNINGS_CAL_TTL:
        return cached["data"]
    try:
        url = (f"https://finnhub.io/api/v1/calendar/earnings"
               f"?from={from_date}&to={to_date}&token={FINNHUB_KEY}")
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json().get("earningsCalendar") or []
        _earnings_cal_cache[key] = {"ts": time.time(), "data": data}
        return data
    except Exception as _e:
        log.warning("Earnings calendar fetch failed (%s→%s): %s", from_date, to_date, _e)
        return []

def get_earnings_today(today: str) -> set:
    """Symbols that would cause an earnings-driven gap at today's open.

    Includes:
      - BMO / DMH reports dated today (announced before or during market)
      - AMC reports dated yesterday (announced after close → gap shows today)
    Returns empty set on any error so callers fail open (don't block trades).
    """
    try:
        dt_today = datetime.strptime(today, "%Y-%m-%d").date()
        dt_yest  = (dt_today - timedelta(days=1)).strftime("%Y-%m-%d")
        entries  = _fetch_earnings_calendar(dt_yest, today)
        result   = set()
        for e in entries:
            sym  = (e.get("symbol") or "").upper()
            date = e.get("date", "")
            hour = (e.get("hour") or "").lower()
            if not sym:
                continue
            if date == today and hour in ("bmo", "dmh", ""):
                result.add(sym)
            elif date == dt_yest and hour == "amc":
                result.add(sym)
        return result
    except Exception:
        return set()

def get_earnings_upcoming(today: str, symbols: list, days: int = 5) -> dict:
    """Return {sym: "YYYY-MM-DD"} for watchlist symbols with earnings in
    the next `days` calendar days (inclusive of today).
    """
    try:
        dt_today = datetime.strptime(today, "%Y-%m-%d").date()
        to_date  = (dt_today + timedelta(days=days)).strftime("%Y-%m-%d")
        entries  = _fetch_earnings_calendar(today, to_date)
        sym_set  = {s.upper() for s in symbols}
        result   = {}
        for e in entries:
            sym  = (e.get("symbol") or "").upper()
            date = e.get("date", "")
            if sym in sym_set and date and sym not in result:
                result[sym] = date   # keep earliest date per symbol
        return result
    except Exception:
        return {}

def get_stock_atr(sym: str, price: float) -> float:
    """
    STRATEGY-5: Compute a server-side 14-period ATR from Finnhub daily candles.

    Uses a simplified Wilder ATR: average of 14 true-ranges (high-low) from
    the most recent 20 trading days.  Falls back gracefully:
      1. Finnhub candle data (preferred — actual OHLCV)
      2. Today's intraday high-low range × 1.3 (rough multiplier to
         approximate a full-day ATR from a partial session range)
      3. 1.5 % of price (original stub — last resort)
    Result is cached for 1 hour to avoid repeated API calls per session.
    """
    cached = _atr_cache.get(sym)
    if cached and (time.time() - cached["_ts"]) < _ATR_CACHE_TTL:
        return cached["atr"]

    atr: float = price * 0.015   # fallback: 1.5% of price

    try:
        end_ts   = int(time.time())
        start_ts = end_ts - 30 * 86400  # 30 calendar days → ≈20 trading days
        url = (f"https://finnhub.io/api/v1/stock/candle"
               f"?symbol={sym}&resolution=D&from={start_ts}&to={end_ts}"
               f"&token={FINNHUB_KEY}")
        for _attempt in range(3):
            r = requests.get(url, timeout=10)
            if r.status_code == 429:
                _wait = min(int(r.headers.get("Retry-After", 2 ** _attempt)), 10)
                _logging.getLogger("quant.atr").warning(
                    "Finnhub candle 429 for %s — retrying in %ds", sym, _wait)
                time.sleep(_wait)
                continue
            break
        if r.status_code == 200:
            data = r.json()
            highs  = data.get("h", [])
            lows   = data.get("l", [])
            closes = data.get("c", [])
            if len(highs) >= 2:
                true_ranges = []
                for j in range(1, min(15, len(highs))):
                    tr = max(
                        highs[j] - lows[j],                    # high - low
                        abs(highs[j] - closes[j - 1]),         # high - prev close
                        abs(lows[j]  - closes[j - 1]),         # low  - prev close
                    )
                    true_ranges.append(tr)
                if true_ranges:
                    atr = sum(true_ranges) / len(true_ranges)
    except Exception as e:
        _logging.getLogger("quant.atr").warning(
            "get_stock_atr: candle fetch failed for %s (%s), using fallback", sym, e)

    # Sanity clamp: ATR must be 0.3%–8% of price
    atr = max(price * 0.003, min(price * 0.08, atr))
    _atr_cache[sym] = {"atr": round(atr, 4), "_ts": time.time()}
    return atr


COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana",  "XRP": "ripple",   "DOGE": "dogecoin",
    "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap",
    "ATOM": "cosmos", "LTC": "litecoin", "BCH": "bitcoin-cash",
    "NEAR": "near", "APT": "aptos", "ARB": "arbitrum",
    "OP": "optimism", "SUI": "sui",
}

def get_crypto_quote(sym: str) -> dict:
    cache = _price_cache.get("crypto_" + sym)
    if cache and (time.time() - cache["_ts"]) < CRYPTO_CACHE_TTL:
        return cache
    coin_id = COIN_IDS.get(sym.upper(), sym.lower())
    try:
        url = (COINGECKO_COIN_URL.format(coin_id=coin_id)
               + "?localization=false&tickers=false&community_data=false&developer_data=false")
        r   = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        d   = r.json()
        md  = d.get("market_data", {})
        price = md.get("current_price", {}).get("usd", 0)
        pc    = md.get("price_change_percentage_24h", 0)
        res   = {
            "c": price, "d": price * pc / 100 if pc else 0, "dp": pc,
            "h": md.get("high_24h", {}).get("usd"), "l": md.get("low_24h", {}).get("usd"),
            "pc": price - (price * pc / 100 if pc else 0),
            "mcap": md.get("market_cap", {}).get("usd"),
            "vol":  md.get("total_volume", {}).get("usd"),
            "rank": d.get("market_cap_rank"),
            "isRealtime": True, "type": "crypto", "_ts": time.time(),
        }
        _price_cache["crypto_" + sym] = res
        return res
    except Exception as e:
        _logging.getLogger("quant.data").warning("Crypto quote error for %s: %s", sym, e)
        return None

def get_single_quote(sym: str, asset_type: str) -> dict:
    try:
        return get_crypto_quote(sym) if asset_type == "crypto" else get_stock_quote(sym)
    except Exception:
        return None


def _batch_fetch_prices(symbols: list, batch_size: int = 20) -> tuple:
    """Fetch current prices for all symbols in batches.

    Sends at most `batch_size` requests per second to stay under Finnhub's
    30 calls/sec free-tier burst limit.  Returns (prices, day_changes) where:
      prices      — {sym: current_price} for symbols with a valid (>0) price
      day_changes — {sym: dp_pct} intraday % change vs prev close (B1 gate)
    Symbols with no price data are silently omitted from both dicts.
    """
    prices = {}
    day_changes = {}
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        for sym in batch:
            q = get_stock_quote(sym)
            if q and q.get("c", 0) > 0:
                prices[sym] = q["c"]
                dp = q.get("dp")
                if dp is not None:
                    day_changes[sym] = float(dp)
        if i + batch_size < len(symbols):
            time.sleep(1.0)
    return prices, day_changes


def _check_guardian_exits(state: dict, prices: dict, provider: str) -> dict:
    """Detect stop-loss and take-profit breaches for one provider's holdings.

    Calls check_auto_stop_rules with the freshly fetched prices and splits
    results by urgency:
      stop_losses  — need 30-second confirmation before executing
      take_profits — execute immediately (5% gain is not a wick)

    Side-effects: updates highPrice and stopPrice on holdings in state
    (legitimate tracking mutations, same as a normal session would do).
    Also sets partial_taken on a holding when SCALE_OUT_1R fires.
    """
    state["lastPrices"] = prices
    state["_nowET"] = datetime.now(_ET).strftime("%H:%M")

    sells = check_auto_stop_rules(state, "guardian", provider=provider)

    STOP_TAGS   = {"STOP_LOSS", "TRAIL_STOP_PROFIT", "HARD_STOP"}
    PROFIT_TAGS = {"HARD_PROFIT", "SCALE_OUT_1R"}
    KNOWN_TAGS  = STOP_TAGS | PROFIT_TAGS | {"REGIME_EXIT"}

    for s in sells:
        if s.get("tag") not in KNOWN_TAGS:
            log.warning("Guardian: unrecognised sell tag %r for %s — skipping",
                        s.get("tag"), s.get("sym"))

    return {
        "stop_losses":  [s for s in sells if s.get("tag") in STOP_TAGS],
        "take_profits": [s for s in sells if s.get("tag") in PROFIT_TAGS],
    }


def _execute_guardian_sell(state: dict, sell: dict, price: float,
                           today: str, now_et: str) -> None:
    """Execute one guardian-triggered sell. Mutates state in place.

    Reuses the same state-mutation pattern as execute_decisions so guardian
    sells appear in trade logs, dailyPnL, cooldowns, and post_exit_watch
    identically to session-triggered sells.  Tag is prefixed with GUARDIAN_
    so guardian exits are distinguishable in the UI and A07 feedback loop.
    """
    sym      = sell["sym"]
    holdings = state.setdefault("holdings", {})
    if sym not in holdings:
        return

    h        = holdings[sym]
    avg_cost = h["avgCost"]
    shares   = sell["shares"]
    real     = (price - avg_cost) * shares
    tag      = "GUARDIAN_" + sell.get("tag", "STOP")

    state["_today"] = today
    state["_nowET"] = now_et

    entry = build_trade_log_entry("sell", {
        "sym":         sym,
        "shares":      shares,
        "price":       price,
        "realizedPnl": real,
        "reason":      sell["reason"],
        "session":     "guardian",
        "confidence":  h.get("confidence", 0),
    }, state, tag)

    state.setdefault("log", []).append(entry)
    state["cash"] = state.get("cash", 0.0) + price * shares * (1 - CFG.EXEC_SLIPPAGE)
    # Intentionally not incrementing todayTrades — guardian exits are system-triggered,
    # not discretionary. Cooldown already prevents same-day re-entry for the symbol.
    state.setdefault("dailyPnL", {})[today] = (
        state["dailyPnL"].get(today, 0) + real
    )

    if shares >= h["shares"]:
        del holdings[sym]
    else:
        h["shares"] -= shares

    state.setdefault("cooldowns", {})[sym]       = today
    state.setdefault("post_exit_watch", {})[sym] = {
        "exit_price": price,
        "exit_date":  today,
        "avg_cost":   avg_cost,
        "pnl_pct":    round((price - avg_cost) / avg_cost * 100, 2) if avg_cost else 0,
        "log_id":     entry["id"],
    }

# ─── News feed ────────────────────────────────────────────────────
_news_cache = {}

def get_news_for_items(items: list, limit: int = 5) -> dict:
    result = {}
    uncached = []
    for item in items:
        sym  = item["symbol"]
        kind = item.get("type", "stock")
        key  = f"{sym}:{kind}"
        nc   = _news_cache.get(key)
        if nc and (time.time() - nc["_ts"]) < NEWS_CACHE_TTL:
            result[key] = nc["items"]
        else:
            uncached.append((key, sym, kind))

    def _fetch_one_news(args):
        key, sym, kind = args
        articles = _fetch_crypto_news(sym, limit) if kind == "crypto" else _fetch_stock_news(sym, limit)
        return key, articles

    if uncached:
        with ThreadPoolExecutor(max_workers=min(len(uncached), 3)) as _ex:
            for key, articles in _ex.map(_fetch_one_news, uncached):
                _news_cache[key] = {"items": articles, "_ts": time.time()}
                result[key] = articles

    return result

def _fetch_stock_news(sym: str, limit: int) -> list:
    try:
        today = today_et()
        from_dt = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        url   = (FINNHUB_NEWS_URL
                 + f"?symbol={sym}&from={from_dt}&to={today}&token={FINNHUB_KEY}")
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return []
        raw = r.json()[:limit]
        return [{"headline": a.get("headline", ""), "summary": a.get("summary", ""),
                 "url": a.get("url", ""), "source": a.get("source", ""),
                 "datetime": a.get("datetime", 0) * 1000,
                 "sentiment": _sentiment(a.get("headline", ""))} for a in raw]
    except Exception as e:
        _logging.getLogger("quant.data").warning("Stock news error for %s: %s", sym, e)
        return []

def _fetch_crypto_news(sym: str, limit: int) -> list:
    if not NEWSAPI_KEY:
        return []
    try:
        url = (f"{NEWSAPI_URL}?q={sym}+cryptocurrency"
               f"&sortBy=publishedAt&pageSize={limit}&apiKey={NEWSAPI_KEY}")
        r   = requests.get(url, timeout=8)
        if r.status_code != 200:
            return []
        arts = r.json().get("articles", [])[:limit]
        return [{"headline": a.get("title", ""), "summary": a.get("description", ""),
                 "url": a.get("url", ""), "source": a.get("source", {}).get("name", ""),
                 "datetime": _parse_ts(a.get("publishedAt")),
                 "sentiment": _sentiment(a.get("title", ""))} for a in arts]
    except Exception:
        return []

def _sentiment(text: str) -> str:
    pos = ["beat", "surge", "rise", "strong", "record", "growth", "上涨", "突破"]
    neg = ["fall", "drop", "decline", "weak", "concern", "risk", "下跌", "亏损"]
    t   = text.lower()
    if any(w in t for w in pos): return "positive"
    if any(w in t for w in neg): return "negative"
    return "neutral"

def _parse_ts(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)

# ─── AI providers ─────────────────────────────────────────────────

def _api_post_with_retry(
    url: str,
    headers: dict,
    payload: dict,
    timeout: int,
    provider_name: str,
    max_retries: int = 2,
) -> requests.Response:
    """POST with exponential backoff on transient errors (5xx, 429, connection issues).
    TOKEN-4: prevents a single network hiccup from silently killing a session.
    """
    _ai_log = _logging.getLogger("quant.ai")
    delay = 2
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                if r.status_code == 429:
                    # Respect provider's Retry-After header; cap at 10s so the
                    # total worst-case (sleep + retried POST) stays well inside
                    # Vercel's 120s function limit. Larger waits trip the wall
                    # and force a 5xx, which (pre-fix) caused Vercel cron to
                    # auto-retry the entire session.
                    retry_after = int(r.headers.get("Retry-After", delay))
                    sleep_secs = min(retry_after, 10)
                else:
                    sleep_secs = delay
                _ai_log.warning(
                    "%s HTTP %s on attempt %d/%d — retrying in %ds",
                    provider_name, r.status_code, attempt, max_retries, sleep_secs,
                )
                time.sleep(sleep_secs)
                delay *= 2
                continue
            return r
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < max_retries:
                _ai_log.warning(
                    "%s connection error attempt %d/%d: %s — retrying in %ds",
                    provider_name, attempt, max_retries, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
    if last_exc:
        raise last_exc
    return r  # unreachable but satisfies type checkers


def call_claude(prompt: str, max_tokens: int = MAX_TOKENS,
                system_text: str = None) -> str:
    """Call Claude API.

    TOKEN-2 (revised): When `system_text` is provided it is sent as a cacheable
    system message (static rules that are identical across retries within 5 min).
    The user message carries only the dynamic market data — no cache_control,
    so we don't pay the 1.25× cache-creation surcharge on every changing token.

    When `system_text` is None (legacy callers, weekly feedback) the full
    `prompt` is sent as a plain user message with no caching overhead.
    """
    if not CLAUDE_KEY:
        return "[ERROR] CLAUDE_KEY not set"
    _ai_log = _logging.getLogger("quant.ai")
    try:
        if system_text:
            # Split mode: static rules → cacheable system block; dynamic data → user.
            payload = {
                "model":      MODELS["claude"]["model"],
                "max_tokens": max_tokens,
                "system": [{"type": "text", "text": system_text,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": prompt}],
            }
        else:
            # Legacy / weekly-feedback mode: single user message, no caching.
            payload = {
                "model":      MODELS["claude"]["model"],
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            }
        r = _api_post_with_retry(
            MODELS["claude"]["api_url"],
            headers={
                "x-api-key":        CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta":   "prompt-caching-2024-07-31",
                "content-type":     "application/json",
            },
            payload=payload,
            timeout=75,
            provider_name="Claude",
            max_retries=2,
        )
        data = r.json()
        if not r.ok or not data.get("content"):
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
            _ai_log.error("Claude API error: %s", data)
            return f"[ERROR] Claude API: {msg}"
        # TOKEN-3: log token usage for cost visibility and cache hit tracking
        usage = data.get("usage", {})
        if usage:
            _ai_log.info(
                "Claude tokens: in=%d out=%d cache_created=%d cache_read=%d",
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
            )
        content = data.get("content", [])
        return content[0].get("text", "") if content and isinstance(content[0], dict) else ""
    except Exception as e:
        return f"[ERROR] Claude: {e}"

def call_grok(prompt: str, max_tokens: int = MAX_TOKENS,
              system_text: str = None) -> str:
    if not GROK_KEY:
        return "[ERROR] GROK_KEY not set"
    _ai_log = _logging.getLogger("quant.ai")
    try:
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": prompt})
        r = _api_post_with_retry(
            MODELS["grok"]["api_url"],
            headers={"Authorization": f"Bearer {GROK_KEY}",
                     "Content-Type": "application/json"},
            payload={"model": MODELS["grok"]["model"], "max_tokens": max_tokens,
                     "messages": messages},
            timeout=60,
            provider_name="Grok",
            max_retries=2,
        )
        data = r.json()
        if not r.ok or not data.get("choices"):
            # TOKEN-6: safe error extraction — error may be a string or a dict
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
            _ai_log.error("Grok API error: %s", data)
            return f"[ERROR] Grok API: {msg}"
        # TOKEN-3: log token usage
        usage = data.get("usage", {})
        if usage:
            _ai_log.info(
                "Grok tokens: prompt=%d completion=%d total=%d",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] Grok: {e}"

def call_deepseek(prompt: str, max_tokens: int = MAX_TOKENS,
                  system_text: str = None) -> str:
    if not DEEPSEEK_KEY:
        return "[ERROR] DEEPSEEK_KEY not set"
    _ai_log = _logging.getLogger("quant.ai")
    try:
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": prompt})
        r = _api_post_with_retry(
            MODELS["deepseek"]["api_url"],
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "Content-Type": "application/json"},
            payload={"model": MODELS["deepseek"]["model"], "max_tokens": max_tokens,
                     "messages": messages},
            timeout=60,
            provider_name="DeepSeek",
            max_retries=2,
        )
        data = r.json()
        if not r.ok or not data.get("choices"):
            # TOKEN-6: safe error extraction — error may be a string or a dict
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
            _ai_log.error("DeepSeek API error: %s", data)
            return f"[ERROR] DeepSeek API: {msg}"
        # TOKEN-3: log token usage
        usage = data.get("usage", {})
        if usage:
            _ai_log.info(
                "DeepSeek tokens: prompt=%d completion=%d total=%d",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] DeepSeek: {e}"

def call_ai(prompt: str, provider: str = "grok", max_tokens: int = MAX_TOKENS,
            system_text: str = None) -> str:
    if provider == "claude":   return call_claude(prompt, max_tokens, system_text=system_text)
    if provider == "deepseek": return call_deepseek(prompt, max_tokens, system_text=system_text)
    return call_grok(prompt, max_tokens, system_text=system_text)

# ─── Trade session runner ─────────────────────────────────────────

# PERF-3: KV-based per-(provider, session) lock. Prevents two concurrent
# runs of the same session — e.g. the Vercel cron firing while the user
# also clicks "trigger now" in the dashboard, or two cron invocations
# overlapping for any reason. A double-run would double API spend, risk
# duplicate trades, and stretch wall-clock time. SET NX EX is atomic on
# Upstash; if KV is unavailable we fail-open (return a sentinel "no-op"
# release) so local dev / KV outages don't block trading entirely.
_SESSION_LOCK_TTL = 180  # seconds — > Vercel maxDuration so a crashed run
                         #  cannot leave the lock held forever in practice.

def _acquire_session_lock(provider: str, session: str) -> bool:
    """Try to claim the lock. Returns True on success, False if already held."""
    if not (_KV_URL and _KV_TOKEN):
        return True  # no KV configured → no locking possible, allow.
    key = f"session_lock:{provider}:{session}"
    token = f"{int(time.time() * 1000)}-{os.getpid()}"
    # SET key value NX EX <ttl> — atomic create-if-not-exists with TTL.
    # Returns "OK" on success, None when the key already exists.
    res = _kv(["SET", key, token, "NX", "EX", str(_SESSION_LOCK_TTL)])
    return res == "OK"

def _release_session_lock(provider: str, session: str) -> None:
    if not (_KV_URL and _KV_TOKEN):
        return
    _kv(["DEL", f"session_lock:{provider}:{session}"])


def run_trade_session(session: str, provider: str) -> dict:
    if not _acquire_session_lock(provider, session):
        _logging.getLogger("quant.session").warning(
            "[%s/%s] Skipping — another run is already in progress "
            "(session lock held). This is normal if cron and a manual "
            "trigger raced; the in-flight run will complete on its own.",
            provider, session)
        return {
            "state": load_trade_state(provider), "aiText": "", "executed": [],
            "exec_log": [{"status": "skipped",
                          "detail": "session lock held — another run in progress"}],
            "decisions_parsed": 0, "decisions_executed": 0,
            "session": session, "provider": provider,
        }
    try:
        return _run_trade_session_locked(session, provider)
    finally:
        _release_session_lock(provider, session)


def _run_trade_session_locked(session: str, provider: str) -> dict:
    state = load_trade_state(provider)
    stocks = load_watchlist()
    all_stock_items = [s for s in stocks if s.get("type") == "stock"]

    stock_items = all_stock_items

    # Guard: abort early if watchlist is empty so we never send a blank prompt.
    # Root cause is usually a Vercel cold start wiping /tmp before KV was set up —
    # now fixed by persisting watchlist to KV in save_watchlist().
    if not stock_items:
        _logging.getLogger("quant.session").error(
            "[%s/%s] Watchlist is empty — skipping session. "
            "Add stocks via the UI; they are now persisted to Redis.", provider, session)
        return {
            "state": state, "aiText": "", "executed": [],
            "exec_log": [{"status": "error",
                          "detail": "观察列表为空，请在 UI 中添加股票后重试。"}],
            "decisions_parsed": 0, "decisions_executed": 0,
            "session": session, "provider": provider,
        }

    # Update time context
    now    = now_et()
    state["_today"]  = now.strftime("%Y-%m-%d")
    state["_nowET"]  = now.strftime("%H:%M")
    state["provider"] = provider

    # Fetch prices + server-side ATR estimates in parallel (one thread per stock).
    # Previously sequential (~18s per stock × N stocks); now collapses to the
    # slowest single fetch.
    prices = {}
    atr_est = {}
    _quotes: dict = {}

    def _fetch_one(s):
        sym = s["symbol"]
        q = get_stock_quote(sym)
        price = q.get("c", q.get("pc", 0)) if q else 0
        atr = get_stock_atr(sym, price) if q else 0
        return sym, q, price, atr

    # Cap at 3 workers: avoids bursting Finnhub's 60 req/min free-tier limit when
    # all 3 provider sessions start simultaneously and each fetches N stocks in parallel.
    with ThreadPoolExecutor(max_workers=min(len(stock_items), 3)) as _ex:
        for sym, q, price, atr in _ex.map(_fetch_one, stock_items):
            if q:
                prices[sym] = price
                state.setdefault("lastPrices", {})[sym] = price
                atr_est[sym] = atr
                _quotes[sym] = q  # cache for watchlist text below — avoids second fetch

    # B1: build intraday % change dict from already-fetched quotes (no extra API calls).
    day_changes = {sym: float(q["dp"]) for sym, q in _quotes.items()
                   if q.get("dp") is not None}

    # Earnings calendar: symbols reporting today (BMO/DMH) or yesterday AMC.
    # Used by execute_decisions to bypass the B1 gap-up gate for earnings moves.
    earnings_today = get_earnings_today(state.get("_today", ""))

    # Fetch news (also parallelised inside get_news_for_items via cache; runs
    # after price fetch so the cache is warm for any overlapping calls).
    news_data = get_news_for_items(stock_items, 3)

    # Build context
    nav = calc_nav(state)
    holdings = state.get("holdings", {})
    portfolio_txt = (
        f"现金:${state['cash']:.2f} | 净值:${nav:.2f} | "
        f"持仓:{len(holdings)}只 | Regime:{state.get('currentRegime','Unknown')}"
    )
    if holdings:
        h_parts = []
        for sym, h in holdings.items():
            price = prices.get(sym, h["avgCost"])
            pnl   = (price - h["avgCost"]) / h["avgCost"] * 100
            h_parts.append(f"{sym} {h['shares']}股 均价${h['avgCost']:.2f} "
                           f"现价${price:.2f} {pnl:+.1f}% 止损${h.get('stopPrice',0):.2f}")
        portfolio_txt += "\n持仓: " + " | ".join(h_parts)

    # STRATEGY-1: include current-day volume (millions) and today's ATR estimate
    # so the AI can verify entry condition ② 量价 without guessing.
    # Format: AAPL $269.59 (+0.15%) Vol:32.1M ATR≈$4.23
    # Reuse quotes already fetched above — no second round-trip to Finnhub.
    wl_parts = []
    for s in stock_items:
        sym = s["symbol"]
        q   = _quotes.get(sym)
        if q:
            vol_m  = (q.get("v") or 0) / 1_000_000          # shares → millions
            atr_d  = atr_est.get(sym, 0)
            vol_str = f" Vol:{vol_m:.1f}M" if vol_m > 0 else ""
            atr_str = f" ATR≈${atr_d:.2f}" if atr_d > 0 else ""
            wl_parts.append(
                f"{sym} ${q.get('c', 0):.2f} ({q.get('dp', 0):+.2f}%){vol_str}{atr_str}"
            )
        else:
            wl_parts.append(sym)
    watchlist_txt = "\n".join(wl_parts)

    news_parts = []
    for s in stock_items:
        key  = f"{s['symbol']}:stock"
        arts = news_data.get(key, [])[:2]
        for a in arts:
            news_parts.append(f"[{s['symbol']}] {a.get('headline','')}")
    news_txt = "\n".join(news_parts) or "无新闻"

    log_summary = ""
    if session in ("mid", "closing"):
        today_log = [e for e in state.get("log", [])
                     if e.get("date") == state["_today"]]
        log_parts = [f"{e['action']} {e['symbol']} {e['shares']}股 @${e['price']:.2f}"
                     for e in today_log[-5:]]
        log_summary = "\n".join(log_parts) or "今日无交易"

    # S2_handoff: inject premarket NEXT_ACTION as focus_note for opening session.
    # Each session is a fresh AI call with no memory, so we persist the
    # premarket analysis note in state and forward it to the opening prompt.
    focus_note = ""
    if session == "opening":
        saved = state.get("premarket_focus", "")
        if saved:
            focus_note = f"\n\n📌 盘前分析重点: {saved}"

    system_txt, user_txt = build_prompt_v6(session, portfolio_txt, watchlist_txt,
                                            news_txt, log_summary, focus_note,
                                            provider=provider)

    # TOKEN-1: use session-specific output token cap to avoid over-spending on
    # sessions that need fewer tokens (premarket = analysis only, closing = SELLs).
    max_tokens = SESSION_MAX_TOKENS.get(session, MAX_TOKENS)
    ai_text = call_ai(user_txt, provider, max_tokens, system_text=system_txt)

    # Parse regime from AI response and update state
    regime_str, spy_adx, spy_above = parse_regime_from_text(ai_text)
    get_market_regime(state, spy_adx, spy_above)

    # S2_handoff: persist the premarket NEXT_ACTION for the opening session.
    # We extract the first non-empty line after "NEXT_ACTION:" and store it
    # in state so run_trade_session for "opening" can inject it as focus_note.
    if session == "premarket" and not ai_text.startswith("[ERROR]"):
        na_m = re.search(r'NEXT_ACTION\s*[：:]\s*(.+)', ai_text)
        if na_m:
            state["premarket_focus"] = na_m.group(1).strip()[:200]
            _logging.getLogger("quant.session").info(
                "[%s/premarket] Saved focus note: %s",
                provider, state["premarket_focus"],
            )
        else:
            state.pop("premarket_focus", None)   # clear stale note from prior day

    # Parse ATR estimates from AI text
    for s in stock_items:
        atr = parse_atr_from_text(ai_text, s["symbol"])
        if atr:
            atr_est[s["symbol"]] = atr

    # Parse and execute decisions (skip for premarket)
    _logger = _logging.getLogger("quant.session")
    decisions = []
    executed  = []

    if session == "premarket":
        _logger.info("[%s/%s] premarket — analysis only, no execution", provider, session)
    else:
        # Guard: if the AI call itself failed, skip parsing entirely.
        # The real error is already logged inside call_ai; avoid a misleading
        # "no DECISION block found" warning that buries the actual root cause.
        if ai_text.startswith("[ERROR]"):
            _logger.error("[%s/%s] AI call failed — skipping decision parse: %s",
                          provider, session, ai_text)
            decisions = []
        else:
            decisions = parse_ai_decisions(ai_text)
            _logger.info("[%s/%s] parsed %d decision(s): %s",
                         provider, session, len(decisions),
                         [(d["action"], d["symbol"], d["shares"], d.get("parse_mode","?"))
                          for d in decisions])

            # Synthetic HOLD fallback: AI gave a real response but no parseable
            # DECISION block — this is normal "hold all" behaviour (AI decided no
            # trades). Record it as HOLD so the session log is clean instead of
            # showing the scary "no DECISION found" warning.
            if not decisions:
                _logger.info(
                    "[%s/%s] No DECISION block found — inserting synthetic HOLD. "
                    "AI text preview: %s", provider, session, ai_text[:300]
                )
                decisions = [{
                    "action": "HOLD", "symbol": "", "shares": 0,
                    "reason": "AI未输出结构化DECISION，默认持仓不变",
                    "parse_mode": "synthetic_hold", "confidence": 0,
                }]

        # Inject confidence scores
        for d in decisions:
            if d["symbol"]:
                d["confidence"] = parse_confidence_score(ai_text, d["symbol"])
                if d["confidence"] == 0 and d.get("parse_mode") == "structured":
                    if d["action"] == "BUY":
                        # BUG-2 (BUY side): AI gave a structured BUY but omitted
                        # "C:X/10".  parse_confidence_score returns 0, which would
                        # always fail the gate (0 < 6).  Default to threshold so the
                        # trade is evaluated by all other rules rather than silently
                        # blocked by a missing format field.
                        d["confidence"] = CFG.SCORE_MIN_NORMAL
                        _logger.info(
                            "[%s/%s] %s: BUY — no C:X/10 found, defaulting to C:%d",
                            provider, session, d["symbol"], CFG.SCORE_MIN_NORMAL,
                        )
                    elif d["action"] == "SELL":
                        # BUG-2 (SELL side): SELL decisions never had a confidence
                        # fallback, so the trade log always recorded confidence=0 for
                        # every sell, corrupting A07 calibration data.
                        # Fix: carry forward the entry confidence from holdings so the
                        # sell log reflects the actual conviction level of the position.
                        held_conf = (state.get("holdings", {})
                                     .get(d["symbol"], {})
                                     .get("confidence", CFG.SCORE_MIN_NORMAL))
                        d["confidence"] = held_conf
                        _logger.info(
                            "[%s/%s] %s: SELL — no C:X/10 found, "
                            "inherited entry confidence C:%d from holdings",
                            provider, session, d["symbol"], held_conf,
                        )

        # Log pre-execution state
        _logger.info("[%s/%s] pre-exec state: cash=$%.2f holdings=%s regime=%s",
                     provider, session, state["cash"],
                     list(state["holdings"].keys()),
                     state.get("currentRegime", "?"))

        # B7: account-wide open-position count across all three providers
        try:
            total_open = 0
            for _p in _VALID_PROVIDERS:
                if _p == provider:
                    total_open += len(state.get("holdings", {}))
                else:
                    _other = load_trade_state(_p)
                    total_open += len(_other.get("holdings", {}))
            account_ctx = {"total_open_positions": total_open}
        except Exception as _e:
            _logger.warning("[%s/%s] account_ctx build failed: %s", provider, session, _e)
            account_ctx = {}

        executed = execute_decisions(decisions, state, session, prices, atr_est,
                                     provider=provider, account_ctx=account_ctx,
                                     day_changes=day_changes,
                                     earnings_today=earnings_today)

        _logger.info("[%s/%s] ── EXECUTION RESULTS (%d) ──────────────────",
                     provider, session, len(executed))
        for _line in executed:
            _logger.info("[%s/%s]   %s", provider, session, _line)
        if not executed:
            _logger.info("[%s/%s]   (no decisions executed)", provider, session)

    # ── Execution diagnostic log ──────────────────────────────────
    # Records every decision attempt with outcome + reason, so you can
    # always see in the log why a trade did or did not execute.
    exec_log = []
    for line in executed:
        if line.startswith("✅"):
            exec_log.append({"status": "executed",  "detail": line})
        elif line.startswith("⚠️"):
            exec_log.append({"status": "skipped",   "detail": line})
        else:
            exec_log.append({"status": "system",    "detail": line})

    # If AI produced decisions but NONE executed, log the raw decisions too
    if decisions and not any(e["status"] == "executed" for e in exec_log):
        for d in decisions:
            exec_log.append({
                "status":     "parse_ok_but_not_executed",
                "action":     d.get("action"),
                "symbol":     d.get("symbol"),
                "shares":     d.get("shares"),
                "parse_mode": d.get("parse_mode", "structured"),
                "detail":     f"⚠️ {d.get('action','?')}|{d.get('symbol','?')} — 决策已解析但被规则拦截（仓位/session限制）",
            })

    # If an AI error prevented parsing, record that clearly (separate from "hold" case)
    if not decisions and session != "premarket" and ai_text.startswith("[ERROR]"):
        exec_log.append({
            "status": "ai_error",
            "detail": ai_text,
        })

    # Log session to JSONL
    session_entry = {
        "id": f"session_{int(time.time()*1000)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": state["_today"],
        "session": session,
        "ai_provider": provider,
        "portfolio_snapshot": {
            "cash": round(state["cash"], 2),
            "total_value": round(calc_nav(state), 2),
            "holdings": [{"symbol": sym, "shares": h["shares"],
                          "avg_cost": h["avgCost"],
                          "current_price": prices.get(sym, h["avgCost"])}
                         for sym, h in state["holdings"].items()],
        },
        "ai_analysis": ai_text,
        "executed":    executed,
        "exec_log":    exec_log,
        # BUG-5 fix: store raw decision parse modes so CHK-3 can read them for
        # ALL decisions (not just unexecuted ones which are the only ones that
        # had parse_mode in exec_log previously).
        "decisions_raw": [
            {"action": d.get("action"), "symbol": d.get("symbol"),
             "parse_mode": d.get("parse_mode", "structured")}
            for d in decisions
        ],
        "regime":      state.get("currentRegime", "Unknown"),
        "decisions_parsed": len(decisions),
        "decisions_executed": sum(1 for e in exec_log if e["status"] == "executed"),
    }
    append_log("sessions", session_entry, state["_today"])

    # Write each individual trade to trades JSONL (persistent record)
    # This is the source of truth that survives Vercel cold starts.
    for entry in state.get("log", []):
        if entry.get("date") == state["_today"] and not entry.get("_logged"):
            append_log("trades", entry, state["_today"])
            entry["_logged"] = True  # prevent double-write on next session

    # Persist state
    save_trade_state(provider, state)

    return {
        "state":              state,
        "aiText":             ai_text,
        "executed":           executed,
        "exec_log":           exec_log,
        "decisions_parsed":   len(decisions),
        "decisions_executed": sum(1 for e in exec_log if e["status"] == "executed"),
        "session":            session,
        "provider":           provider,
    }

# ─── Routes ───────────────────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    # Return empty 204 No Content — stops the browser 404 log noise
    return "", 204
@app.route("/")
def index():
    # Serve index.html as a plain static file — NOT through Jinja2.
    # The HTML contains JavaScript with {{ }} object literals which
    # Jinja2 would try to interpret as template variables and crash.
    html_path = Path(__file__).parent / "templates" / "index.html"
    return Response(html_path.read_text(encoding="utf-8"),
                    mimetype="text/html")


# ─── Daily Execution Health Check ────────────────────────────────
# Manual trigger only — run after market close each day to detect
# execution bugs before they repeat across multiple sessions.
#
# Usage:
#   GET /api/daily-review              →  today's report
#   GET /api/daily-review/2026-04-26   →  specific date
#
# Returns JSON with all 10 check results + human-readable report_text.
# To view the plain-text report in a terminal:
#   curl -s https://your-app.vercel.app/api/daily-review | python3 -m json.tool | grep -A1 report_text

@app.route("/api/daily-review", methods=["GET"])
@app.route("/api/daily-review/<date>", methods=["GET"])
def api_daily_review(date: str = None):
    """
    Run the 10-check execution health report for a given date (default: today).

    Query params:
      format=text   → returns plain-text report instead of JSON (easier to read)

    The report checks execution health only — no strategy decisions are made.
    """
    if date is None:
        date = today_et()

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": f"Invalid date format: '{date}'. Use YYYY-MM-DD."}), 400

    try:
        result = run_daily_review(
            date                 = date,
            read_log_fn          = read_log_range,
            load_state_fn        = load_trade_state,
            watchlist            = load_watchlist(),
        )

        # Optional plain-text mode for easy terminal reading
        if request.args.get("format") == "text":
            return Response(result["report_text"], mimetype="text/plain; charset=utf-8")

        return jsonify(result)

    except Exception as e:
        _logging.getLogger(__name__).error("Daily review failed for %s: %s", date, e,
                                           exc_info=True)
        return jsonify({"error": str(e), "date": date}), 500


# ─── Cron job routes (Vercel Cron) ───────────────────────────────
# Vercel calls these GET endpoints on the schedule in vercel.json.
# Each session runs for all 3 AI providers (staggered 2 min apart).
#
# Schedule (UTC, EDT = UTC-4):
#   premarket  9:15 ET  = 13:15 UTC  (Mon-Fri)
#   opening   10:00 ET  = 14:00 UTC  (Mon-Fri)
#   mid       12:00 ET  = 16:00 UTC  (Mon-Fri)
#   closing   15:30 ET  = 19:30 UTC  (Mon-Fri)
#
# Note: During EST (winter, UTC-5) sessions run 1 hour late.
# To fix: update schedules in vercel.json to +1 hour in Nov-Mar.

_VALID_SESSIONS  = {"premarket", "opening", "mid", "closing"}
_VALID_PROVIDERS = {"grok", "claude", "deepseek"}
_CRON_SECRET     = os.environ.get("CRON_SECRET", "")  # required on Vercel
# Escape hatch for local development only. Set CRON_ALLOW_UNAUTH=1 in your
# local env to bypass auth; never set this on Vercel.
_CRON_ALLOW_UNAUTH = os.environ.get("CRON_ALLOW_UNAUTH", "").lower() in ("1", "true", "yes")


def _verify_cron(req) -> bool:
    """
    Verify the request is from Vercel's cron runner.

    Vercel automatically injects `Authorization: Bearer <CRON_SECRET>` on
    cron-triggered requests when the CRON_SECRET env var is set. We require
    it — otherwise any external caller (uptime monitor, crawler, stale tab)
    can fire a real trading session, which combined with Vercel's cron
    auto-retry was causing duplicate invocations and 10+ minute wall times.

    Local dev: set CRON_ALLOW_UNAUTH=1 to bypass.
    """
    if _CRON_ALLOW_UNAUTH:
        return True
    if not _CRON_SECRET:
        # Fail closed: missing secret = block, do not silently allow.
        _logging.getLogger("quant.cron").error(
            "CRON_SECRET is not set — rejecting request. "
            "Set CRON_SECRET on Vercel (Project Settings → Environment Variables).")
        return False
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {_CRON_SECRET}"


@app.route("/api/cron/<session>/<provider>", methods=["GET"])
def cron_run(session, provider):
    """Called by Vercel Cron. Runs one trading session for one AI provider."""
    if not _verify_cron(request):
        return jsonify({"error": "Unauthorized"}), 401

    if session not in _VALID_SESSIONS:
        return jsonify({"error": f"Unknown session: {session}"}), 400
    if provider not in _VALID_PROVIDERS:
        return jsonify({"error": f"Unknown provider: {provider}"}), 400

    _logging.getLogger("quant.cron").info(
        "Cron triggered: session=%s provider=%s", session, provider)

    try:
        result = run_trade_session(session, provider)
        executed_count = sum(
            1 for e in (result.get("exec_log") or [])
            if e.get("status") == "executed"
        )
        _logging.getLogger("quant.cron").info(
            "Cron complete: session=%s provider=%s executed=%d",
            session, provider, executed_count)
        return jsonify({
            "ok":       True,
            "session":  session,
            "provider": provider,
            "executed": result.get("executed", []),
            "exec_log": result.get("exec_log", []),
            "decisions_parsed":   result.get("decisions_parsed", 0),
            "decisions_executed": result.get("decisions_executed", 0),
        })
    except Exception as e:
        _logging.getLogger("quant.cron").error(
            "Cron error: session=%s provider=%s error=%s", session, provider, e)
        # Return 200 (not 500) so Vercel Cron does NOT auto-retry. A retry
        # would re-run the AI call, double-bill API spend, risk duplicate
        # trades, and stretch the wall-clock to 6-12 minutes for a single
        # logical session. The error is captured in logs and the JSON body.
        return jsonify({"ok": False, "session": session, "provider": provider,
                        "error": str(e)}), 200


_GUARDIAN_LOCK_KEY = "guardian_lock"
_GUARDIAN_LOCK_TTL = 90  # seconds: covers 30s confirmation + execution headroom
_GUARDIAN_HB_KEY   = "guardian:heartbeats"
_GUARDIAN_HB_TTL   = 86400  # 24 hours in seconds


def _record_guardian_heartbeat(record: dict) -> None:
    """Append one guardian-run record to the Redis sorted set.

    Score = Unix timestamp (float) so ZRANGEBYSCORE naturally gives time ranges.
    Entries older than 24 h are pruned on every write so the set stays bounded.
    """
    if not _USE_KV:
        return
    ts = record.get("ts", time.time())
    cutoff = ts - _GUARDIAN_HB_TTL
    try:
        # Prune entries older than 24 h, then append the new one
        _kv(["ZREMRANGEBYSCORE", _GUARDIAN_HB_KEY, "-inf", str(cutoff)])
        _kv(["ZADD", _GUARDIAN_HB_KEY, str(ts), json.dumps(record, ensure_ascii=False)])
    except Exception as e:
        _logging.getLogger("quant.guardian").warning("Heartbeat write failed: %s", e)


@app.route("/api/cron/guardian", methods=["GET"])
def cron_guardian():
    """Guardian cron: intra-session stop-loss and take-profit enforcement.

    Triggered by cron-job.org (not Vercel cron — every-5-min frequency
    exceeds the Hobby plan's once-per-day limit).

    Schedule on cron-job.org:
      Job 1 — market hours:  */5 13-20 * * 1-5  (UTC)
      Job 2 — off-hours:     0 * * * *
    Both jobs call: GET https://<app>.vercel.app/api/cron/guardian
    with header:    Authorization: Bearer <CRON_SECRET>
    """
    if not _verify_cron(request):
        return jsonify({"error": "Unauthorized"}), 401

    log = _logging.getLogger("quant.guardian")
    _t0 = time.time()

    # Acquire guardian lock (atomic SET NX) — skip if a prior run is mid-flight
    if _USE_KV:
        token  = f"{int(time.time() * 1000)}-{os.getpid()}"
        result = _kv(["SET", _GUARDIAN_LOCK_KEY, token, "NX", "EX",
                       str(_GUARDIAN_LOCK_TTL)])
        if result != "OK":
            log.info("Guardian skipped — lock held (prior run still active)")
            _record_guardian_heartbeat({
                "ts": _t0, "ts_et": datetime.now(_ET).strftime("%H:%M ET"),
                "status": "skipped", "skip_reason": "lock_held",
                "n_checked": 0, "symbols": [],
                "profit_executed": [], "stop_executed": [], "duration_ms": 0,
            })
            return jsonify({"ok": True, "status": "skipped",
                            "reason": "guardian lock held"}), 200

    try:
        # Skip if any trading session is currently running — avoids race where
        # both guardian and a session attempt to sell the same holding.
        for p in _VALID_PROVIDERS:
            for s in _VALID_SESSIONS:
                if _USE_KV and _kv(["GET", f"session_lock:{p}:{s}"]):
                    log.info("Guardian skipped — session lock held: %s/%s", p, s)
                    _record_guardian_heartbeat({
                        "ts": _t0, "ts_et": datetime.now(_ET).strftime("%H:%M ET"),
                        "status": "skipped", "skip_reason": f"session:{p}/{s}",
                        "n_checked": 0, "symbols": [],
                        "profit_executed": [], "stop_executed": [], "duration_ms": 0,
                    })
                    return jsonify({"ok": True, "status": "skipped",
                                    "reason": f"session lock: {p}/{s}"}), 200

        today   = today_et()
        now_et  = datetime.now(_ET).strftime("%H:%M")

        # Load all three providers' states
        states = {p: load_trade_state(p) for p in _VALID_PROVIDERS}

        # Collect unique symbols currently held across all providers
        all_symbols = list({
            sym
            for state in states.values()
            for sym in state.get("holdings", {})
        })

        if not all_symbols:
            log.info("Guardian: no holdings to check")
            _record_guardian_heartbeat({
                "ts": _t0, "ts_et": datetime.now(_ET).strftime("%H:%M ET"),
                "status": "no_holdings", "skip_reason": None,
                "n_checked": 0, "symbols": [],
                "profit_executed": [], "stop_executed": [],
                "duration_ms": int((time.time() - _t0) * 1000),
            })
            return jsonify({"ok": True, "status": "no_holdings", "checked": 0}), 200

        # Batch-fetch prices (20/batch, 1s gap → ≤20 calls/sec, safe under 30/sec)
        prices, _ = _batch_fetch_prices(all_symbols)
        log.info("Guardian: fetched %d/%d prices for %s",
                 len(prices), len(all_symbols), all_symbols)

        # First pass: detect exits per provider
        suspected_stops  = {}   # {provider: [sell_dict, ...]}
        take_profit_hits = {}   # {provider: [sell_dict, ...]}

        for provider, state in states.items():
            if not state.get("holdings"):
                continue
            exits = _check_guardian_exits(state, prices, provider)
            if exits["stop_losses"]:
                suspected_stops[provider]  = exits["stop_losses"]
            if exits["take_profits"]:
                take_profit_hits[provider] = exits["take_profits"]

        # Execute take-profits immediately — 5% gain is not a wick
        profit_executed = []
        for provider, sells in take_profit_hits.items():
            state = states[provider]
            for sell in sells:
                sym   = sell["sym"]
                price = prices.get(sym, 0)
                if price <= 0 or sym not in state.get("holdings", {}):
                    continue
                _execute_guardian_sell(state, sell, price, today, now_et)
                profit_executed.append(f"{provider}/{sym}")
                log.info("Guardian PROFIT: %s/%s @$%.2f tag=%s",
                         provider, sym, price, sell.get("tag"))

        # Confirm stop-losses: sleep 30s, re-fetch, verify breach persists
        stop_executed = []
        if suspected_stops:
            stop_syms = list({
                s["sym"]
                for sells in suspected_stops.values()
                for s in sells
            })
            log.info("Guardian: suspected stop breach on %s — confirming in 30s",
                     stop_syms)
            time.sleep(30)
            confirm_prices, _ = _batch_fetch_prices(stop_syms)

            for provider, sells in suspected_stops.items():
                state = states[provider]
                for sell in sells:
                    sym        = sell["sym"]
                    conf_price = confirm_prices.get(sym, 0)
                    holding    = state.get("holdings", {}).get(sym)
                    if not holding or conf_price <= 0:
                        continue

                    stop_p      = holding.get("stopPrice", float("inf"))
                    avg_cost    = holding["avgCost"]
                    risk_per    = holding.get("riskPerShare", avg_cost * 0.02)
                    pnl_pct     = (conf_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
                    hard_stop   = max(CFG.HARD_STOP_PCT,
                                      risk_per / avg_cost * 100 if avg_cost > 0
                                      else CFG.HARD_STOP_PCT)

                    still_breached = (conf_price <= stop_p or pnl_pct <= -hard_stop)

                    if still_breached:
                        # Clamp shares in case a take-profit partial executed first
                        sell = dict(sell)
                        sell["shares"] = min(sell["shares"], holding["shares"])
                        _execute_guardian_sell(state, sell, conf_price, today, now_et)
                        stop_executed.append(f"{provider}/{sym}")
                        log.info("Guardian STOP confirmed: %s/%s @$%.2f",
                                 provider, sym, conf_price)
                    else:
                        log.info("Guardian STOP wick filtered: %s/%s recovered $%.2f",
                                 provider, sym, conf_price)

        # Persist updated states and write guardian trades to persistent log
        changed_providers = {p.split("/")[0] for p in profit_executed + stop_executed}
        for provider in changed_providers:
            save_trade_state(provider, states[provider])
            for entry in states[provider].get("log", []):
                if (entry.get("session") == "guardian"
                        and entry.get("date") == today
                        and not entry.get("_logged")):
                    append_log("trades", entry, today)
                    entry["_logged"] = True

        log.info("Guardian complete: checked=%d stop=%s profit=%s",
                 len(all_symbols), stop_executed, profit_executed)
        _record_guardian_heartbeat({
            "ts":             _t0,
            "ts_et":          datetime.now(_ET).strftime("%H:%M ET"),
            "status":         "checked",
            "skip_reason":    None,
            "n_checked":      len(all_symbols),
            "symbols":        all_symbols,
            "profit_executed": profit_executed,
            "stop_executed":  stop_executed,
            "duration_ms":    int((time.time() - _t0) * 1000),
        })
        return jsonify({
            "ok":             True,
            "status":         "checked",
            "checked":        len(all_symbols),
            "prices_fetched": len(prices),
            "stop_executed":  stop_executed,
            "profit_executed": profit_executed,
        }), 200

    except Exception as e:
        log.error("Guardian error: %s", e, exc_info=True)
        _record_guardian_heartbeat({
            "ts":          _t0,
            "ts_et":       datetime.now(_ET).strftime("%H:%M ET"),
            "status":      "error",
            "skip_reason": None,
            "error":       str(e),
            "n_checked":   0,
            "symbols":     [],
            "profit_executed": [], "stop_executed": [],
            "duration_ms": int((time.time() - _t0) * 1000),
        })
        return jsonify({"ok": False, "error": str(e)}), 200
    finally:
        if _USE_KV:
            _kv(["DEL", _GUARDIAN_LOCK_KEY])


@app.route("/api/guardian/heartbeat", methods=["GET"])
def guardian_heartbeat():
    """Return guardian heartbeat records from the last 24 hours.

    Records are stored in a Redis sorted set (score = Unix timestamp).
    Returned newest-first so the widget can render top-to-bottom.
    """
    if not _USE_KV:
        return jsonify({"ok": True, "records": [], "note": "KV not configured"}), 200

    cutoff = time.time() - _GUARDIAN_HB_TTL
    try:
        raw = _kv(["ZRANGEBYSCORE", _GUARDIAN_HB_KEY, str(cutoff), "+inf"]) or []
        records = []
        for item in raw:
            try:
                records.append(json.loads(item))
            except Exception:
                pass
        records.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return jsonify({"ok": True, "records": records}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "records": []}), 200


@app.route("/api/cron/status", methods=["GET"])
def cron_status():
    """
    Returns the last run time for each session/provider combination.
    Frontend polls this to show the trigger status panel.
    """
    status = {}
    for provider in _VALID_PROVIDERS:
        try:
            # Read the most recent session log entry for this provider
            today = today_et()
            from_date = (datetime.now(_ET) - timedelta(days=2)).strftime("%Y-%m-%d")
            sessions = read_log_range("sessions", from_date, today, provider)
            if sessions:
                last = sessions[0]  # already sorted newest-first
                status[provider] = {
                    "last_session":   last.get("session"),
                    "last_run":       last.get("timestamp"),
                    "last_date":      last.get("date"),
                    "decisions_exec": last.get("decisions_executed", 0),
                    "regime":         last.get("regime", "Unknown"),
                }
            else:
                status[provider] = {"last_run": None}
        except Exception:
            status[provider] = {"last_run": None}

    return jsonify({"ok": True, "status": status,
                    "schedule_utc": {
                        "premarket": "13:15 UTC (9:15 ET EDT)",
                        "opening":   "14:00 UTC (10:00 ET EDT)",
                        "mid":       "16:00 UTC (12:00 ET EDT)",
                        "closing":   "19:30 UTC (15:30 ET EDT)",
                    }})

@app.route("/api/cron/weekend-feedback", methods=["GET"])
def cron_weekend_feedback():
    """
    Saturday cron — analyze last week's trade decisions vs actual outcomes.
    Runs at 14:00 UTC (10:00 ET Saturday) via vercel.json cron.
    """
    if not _verify_cron(request):
        return jsonify({"error": "Unauthorized"}), 401

    _logging.getLogger("quant.cron").info("Weekend feedback cron triggered")
    try:
        from_dt, to_dt = most_recent_week()
        result = run_weekend_feedback(
            from_date     = from_dt,
            to_date       = to_dt,
            read_log_fn   = read_log_range,
            get_quote_fn  = get_stock_quote,
            call_ai_fn    = call_ai,
            append_log_fn = append_log,
        )
        providers = list(result.get("reports", {}).keys())
        _logging.getLogger("quant.cron").info(
            "Weekend feedback done: %s → %s, providers=%s", from_dt, to_dt, providers)
        return jsonify({"ok": True, "from_date": from_dt, "to_date": to_dt,
                        "providers_analyzed": providers})
    except Exception as e:
        _logging.getLogger("quant.cron").error("Weekend feedback error: %s", e)
        # 200 to disable Vercel cron auto-retry (see cron_run for rationale).
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/cron/watchlist-suggestions", methods=["GET"])
def cron_watchlist_suggestions():
    """
    Saturday cron — scan news and generate watchlist add/remove suggestions.
    Runs at 16:00 UTC (12:00 ET Saturday) via vercel.json cron.
    """
    if not _verify_cron(request):
        return jsonify({"error": "Unauthorized"}), 401

    _logging.getLogger("quant.cron").info("Watchlist suggestions cron triggered")
    try:
        from_dt, to_dt = most_recent_week()
        result = run_watchlist_suggestions(
            from_date         = from_dt,
            to_date           = to_dt,
            finnhub_key       = FINNHUB_KEY,
            get_quote_fn      = get_stock_quote,
            call_ai_fn        = call_ai,
            current_watchlist = load_watchlist(),
            append_log_fn     = append_log,
        )
        return jsonify({
            "ok":         True,
            "from_date":  from_dt,
            "to_date":    to_dt,
            "add_count":  len(result.get("suggestions_add", [])),
            "remove_count": len(result.get("suggestions_remove", [])),
        })
    except Exception as e:
        _logging.getLogger("quant.cron").error("Watchlist suggestions error: %s", e)
        # 200 to disable Vercel cron auto-retry (see cron_run for rationale).
        return jsonify({"ok": False, "error": str(e)}), 200


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


@app.route("/api/storage-diag", methods=["GET"])
def storage_diag():
    """Show exactly what trade/session data exists in Redis so we can tell
    whether historical records are present or were lost before Redis was set up."""
    if not _USE_KV:
        return jsonify({"ok": False, "message": "Redis not configured — data lives in /tmp only"})

    # Scan the last 6 months of log keys for both trades and sessions
    from datetime import date as _date
    today_s = today_et()
    summary = {}
    ym = today_s[:7]
    for _ in range(6):
        for prefix in ("trades", "sessions"):
            key = f"log:{prefix}:{ym}"
            count = _kv(["LLEN", key])
            if count:
                summary[key] = int(count)
        # decrement month
        y, m = int(ym[:4]), int(ym[5:7])
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        ym = f"{y:04d}-{m:02d}"

    # Also check state keys
    states = {}
    for p in _VALID_PROVIDERS:
        raw = _kv(["GET", f"tradestate:{p}"])
        if raw:
            try:
                s = json.loads(raw)
                states[p] = {
                    "cash": s.get("cash"),
                    "holdings": list(s.get("holdings", {}).keys()),
                    "log_entries": len(s.get("log", [])),
                }
            except Exception:
                states[p] = {"error": "parse failed"}
        else:
            states[p] = None

    total_trades = sum(v for k, v in summary.items() if "trades" in k)
    return jsonify({
        "ok": True,
        "total_trade_records_in_redis": total_trades,
        "keys": summary,
        "states": states,
        "verdict": "Data exists in Redis" if total_trades > 0 else "No trade records found in Redis — data was likely stored in /tmp before Redis was configured",
    })

@app.route("/api/kv-status", methods=["GET"])
def kv_status():
    """Check KV/Redis connectivity — used by the UI to warn if logs won't persist."""
    diag = {
        "env_vars_found": _KV_CANDIDATES,
        "url_resolved":   _KV_URL[:60] if _KV_URL else None,
        "token_resolved": bool(_KV_TOKEN),
    }
    if not _USE_KV:
        return jsonify({"ok": False, "kv": False,
                        "message": "No Redis env vars detected.",
                        "diag": diag})
    ping = _kv(["PING"])
    ok = (ping == "PONG")
    return jsonify({"ok": ok, "kv": ok,
                    "message": "Redis connected" if ok else "Redis env vars set but PING failed",
                    "diag": diag})

@app.route("/api", methods=["POST"])
@app.route("/claude-api", methods=["POST"])
def api():
    try:
        data   = request.get_json(force=True) or {}
        action = data.get("action", "")
        result = dispatch(action, data)
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def dispatch(action: str, data: dict):
    # Watchlist
    if action == "getStocks":
        return load_watchlist()
    if action == "saveStocks":
        save_watchlist(data["stocks"])
        return {"saved": len(data["stocks"])}

    # Quotes & News
    if action == "getQuote":
        return get_single_quote(data["symbol"], data.get("type", "stock"))
    if action == "getNews":
        return get_news_for_items(data["items"], data.get("limit", 5))
    if action == "getEarnings":
        today    = datetime.now(_ET).strftime("%Y-%m-%d")
        symbols  = [s["symbol"] for s in (data.get("stocks") or [])
                    if s.get("type") == "stock"]
        return get_earnings_upcoming(today, symbols, days=5)

    # AI analysis (free-form)
    if action == "analyzeStock":
        provider = data.get("provider", "grok")
        return call_ai(data["prompt"], provider, 2000)

    # Trade state
    if action == "getTradeState":
        return load_trade_state(data.get("provider", "grok"))
    if action == "saveStateToBackend":
        # Frontend pushes its localStorage copy back to /tmp after cold start
        provider = data.get("provider", "grok")
        state    = data.get("state")
        if state and isinstance(state, dict):
            save_trade_state(provider, state)
            return {"ok": True}
        return {"ok": False, "error": "no state provided"}
    if action == "resetTradeState":
        return reset_trade_state(data.get("provider", "grok"))
    if action == "getQuantMetrics":
        state = load_trade_state(data.get("provider", "grok"))
        return get_quant_metrics(state)

    # Trade session
    if action == "runTradeSession":
        return run_trade_session(
            data.get("session", session_for_now()),
            data.get("provider", "grok"),
        )

    # Trigger management (stubs — Python runs via scheduler)
    if action == "setupTradeTriggers":
        return {"status": "ok", "note": "Use APScheduler or cron for Python"}
    if action == "removeTradeTriggers":
        return {"status": "stopped"}
    if action == "getTradeTriggerStatus":
        return {"active": [], "running": False}

    # Drive-equivalent logs
    if action == "getDriveSessions":
        today = today_et()
        return read_log_range("sessions",
                              data.get("fromDate", today[:7] + "-01"),
                              data.get("toDate", today),
                              data.get("aiProvider"))
    if action == "getDriveTrades":
        today = today_et()
        return read_log_range("trades",
                              data.get("fromDate", today[:7] + "-01"),
                              data.get("toDate", today),
                              data.get("aiProvider"))
    if action == "listDriveLogFiles":
        return list_log_files()

    # ── Weekly feedback report ────────────────────────────────────
    if action == "getWeeklyFeedback":
        default_from, default_to = most_recent_week()
        from_dt = data.get("fromDate") or default_from
        to_dt   = data.get("toDate")   or default_to
        return read_log_range("feedback", from_dt, to_dt)

    if action == "runWeeklyFeedback":
        from_dt, to_dt = most_recent_week()
        return run_weekend_feedback(
            from_date     = from_dt,
            to_date       = to_dt,
            read_log_fn   = read_log_range,
            get_quote_fn  = get_stock_quote,
            call_ai_fn    = call_ai,
            append_log_fn = append_log,
        )

    # ── Watchlist suggestions ─────────────────────────────────────
    if action == "getWatchlistSuggestions":
        default_from, default_to = most_recent_week()
        from_dt = data.get("fromDate") or default_from
        to_dt   = data.get("toDate")   or default_to
        return read_log_range("suggestions", from_dt, to_dt)

    if action == "runWatchlistSuggestions":
        from_dt, to_dt = most_recent_week()
        return run_watchlist_suggestions(
            from_date        = from_dt,
            to_date          = to_dt,
            finnhub_key      = FINNHUB_KEY,
            get_quote_fn     = get_stock_quote,
            call_ai_fn       = call_ai,
            current_watchlist= load_watchlist(),
            append_log_fn    = append_log,
        )

    # Trigger time (stub)
    if action == "getTriggerTime":
        return {"hour": 20, "min": 0, "active": False}
    if action == "setTriggerTime":
        return {"ok": True}
    if action == "startBackground":
        return {"ok": True}
    if action == "stopBackground":
        return {"ok": True}
    if action == "triggerNow":
        provider = data.get("provider", "grok")
        return run_trade_session(session_for_now(), provider)

    # Session messages (keep compatible)
    if action == "getSessionMessages":
        return []
    if action == "saveSessionMessage":
        return {"id": str(int(time.time()))}
    if action == "deleteSessionMessage":
        return {"ok": True}
    if action == "clearAllSessionMessages":
        return {"ok": True}

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

    raise ValueError(f"Unknown action: {action}")


@app.route("/logs/<path:filename>")
def serve_log(filename):
    return send_from_directory(LOGS_DIR, filename)

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=PORT)