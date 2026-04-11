"""
Quant AI Stock Trader — Python Flask Backend
Replaces Google Apps Script backend.
Maintains identical API surface consumed by the frontend.
"""

import os, json, time, re
from datetime import datetime, timezone
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    from datetime import timezone as _tz, timedelta
    _ET = _tz(timedelta(hours=-5))
import requests
from flask import Flask, request, jsonify, render_template, send_from_directory, Response

# ─── Config (API keys, model names, cache TTLs) ─────────────────
from config import (
    FINNHUB_KEY, NEWSAPI_KEY, CLAUDE_KEY, GROK_KEY, DEEPSEEK_KEY,
    SERPAPI_KEY, PORT, MODELS, MAX_TOKENS,
    FINNHUB_QUOTE_URL, FINNHUB_NEWS_URL, COINGECKO_COIN_URL, NEWSAPI_URL,
    PRICE_CACHE_TTL, CRYPTO_CACHE_TTL, NEWS_CACHE_TTL,
    check_config,
)
from strategy_v6 import (
    CFG, new_trade_state, calc_nav, get_quant_metrics,
    build_prompt_v6, parse_ai_decisions, parse_confidence_score,
    parse_regime_from_text, parse_atr_from_text,
    execute_decisions, check_operating_rules, get_market_regime,
)

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
import logging as _logging
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
STATE_DIR      = DATA_DIR / "trade_states"
STATE_DIR.mkdir(parents=True, exist_ok=True)

def load_watchlist():
    # Priority 1: local file (works locally and persists across restarts)
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except Exception:
            pass
    # Priority 2: WATCHLIST env var (set this in Vercel dashboard to persist
    # across cold starts — copy the JSON array value from a working session)
    env_wl = os.environ.get("WATCHLIST_JSON", "")
    if env_wl:
        try:
            return json.loads(env_wl)
        except Exception:
            pass
    return []

def save_watchlist(stocks):
    # Always try to write the file (works locally; /tmp on Vercel — temporary but
    # better than nothing within a session)
    try:
        WATCHLIST_FILE.write_text(json.dumps(stocks))
    except OSError:
        pass
    # Hint in logs so user knows to set env var for persistence on Vercel
    if os.environ.get("VERCEL"):
        import logging as _l
        _l.getLogger(__name__).warning(
            "VERCEL: watchlist saved to /tmp only (ephemeral). "
            "To persist across deployments, set WATCHLIST_JSON env var in "
            "Vercel dashboard to: %s", json.dumps(stocks)
        )

def _state_file(provider: str) -> Path:
    return STATE_DIR / f"state_{provider}.json"

def load_trade_state(provider: str) -> dict:
    f = _state_file(provider)
    if f.exists():
        try:
            state = json.loads(f.read_text())
            # If state.log is empty but JSONL trade log has records,
            # rebuild state.log from the persistent JSONL file so the
            # trade panel shows history even after a Vercel cold start.
            if not state.get("log"):
                today = today_et() if callable(today_et) else ""
                month = today[:7] if today else ""
                trade_log = read_log_range("trades",
                                           month + "-01" if month else "2020-01-01",
                                           today or "2099-12-31",
                                           provider)
                if trade_log:
                    state["log"] = list(reversed(trade_log))  # oldest first
            return state
        except Exception:
            pass
    return new_trade_state()

def save_trade_state(provider: str, state: dict):
    _state_file(provider).write_text(json.dumps(state, default=str))

def reset_trade_state(provider: str) -> dict:
    s = new_trade_state()
    save_trade_state(provider, s)
    return s

# ─── Drive-equivalent: JSONL log files ───────────────────────────
def _month_log_path(prefix: str, date_str: str = None) -> Path:
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m")
    else:
        date_str = date_str[:7]
    return LOGS_DIR / f"{prefix}_{date_str}.jsonl"

def append_log(prefix: str, entry: dict, date_str: str = None):
    path = _month_log_path(prefix, date_str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")

def read_log_range(prefix: str, from_date: str, to_date: str,
                   ai_provider: str = None) -> list:
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
    if h < 9.25:  return "premarket"
    if h < 12.0:  return "opening"
    if h < 15.0:  return "mid"
    return "closing"

# ─── Price feeds ──────────────────────────────────────────────────
_price_cache = {}

def get_stock_quote(sym: str) -> dict:
    cache = _price_cache.get(sym)
    if cache and (time.time() - cache["_ts"]) < PRICE_CACHE_TTL:
        return cache
    try:
        url = f"{FINNHUB_QUOTE_URL}?symbol={sym}&token={FINNHUB_KEY}"
        r   = requests.get(url, timeout=8)
        if r.status_code != 200:
            return None
        q   = r.json()
        dp  = q.get("c", 0) or q.get("pc", 0)
        res = {"c": dp, "d": q.get("d"), "dp": q.get("dp"),
               "h": q.get("h"), "l": q.get("l"), "o": q.get("o"), "pc": q.get("pc"),
               "isRealtime": bool(q.get("c", 0)), "type": "stock", "_ts": time.time()}
        _price_cache[sym] = res
        return res
    except Exception:
        return None

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
    except Exception:
        return None

def get_single_quote(sym: str, asset_type: str) -> dict:
    try:
        return get_crypto_quote(sym) if asset_type == "crypto" else get_stock_quote(sym)
    except Exception:
        return None

# ─── News feed ────────────────────────────────────────────────────
_news_cache = {}

def get_news_for_items(items: list, limit: int = 5) -> dict:
    result = {}
    for item in items:
        sym  = item["symbol"]
        kind = item.get("type", "stock")
        key  = f"{sym}:{kind}"
        nc   = _news_cache.get(key)
        if nc and (time.time() - nc["_ts"]) < NEWS_CACHE_TTL:
            result[key] = nc["items"]
            continue
        articles = []
        if kind == "crypto":
            articles = _fetch_crypto_news(sym, limit)
        else:
            articles = _fetch_stock_news(sym, limit)
        _news_cache[key] = {"items": articles, "_ts": time.time()}
        result[key] = articles
    return result

def _fetch_stock_news(sym: str, limit: int) -> list:
    try:
        today = today_et()
        url   = (FINNHUB_NEWS_URL
                 + f"?symbol={sym}&from=2020-01-01&to={today}&token={FINNHUB_KEY}")
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return []
        raw = r.json()[:limit]
        return [{"headline": a.get("headline", ""), "summary": a.get("summary", ""),
                 "url": a.get("url", ""), "source": a.get("source", ""),
                 "datetime": a.get("datetime", 0) * 1000,
                 "sentiment": _sentiment(a.get("headline", ""))} for a in raw]
    except Exception:
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
def call_claude(prompt: str) -> str:
    if not CLAUDE_KEY:
        return "[ERROR] CLAUDE_KEY not set"
    try:
        r = requests.post(
            MODELS["claude"]["api_url"],
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODELS["claude"]["model"], "max_tokens": MAX_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        data = r.json()
        return data["content"][0]["text"] if data.get("content") else str(data)
    except Exception as e:
        return f"[ERROR] Claude: {e}"

def call_grok(prompt: str) -> str:
    if not GROK_KEY:
        return "[ERROR] GROK_KEY not set"
    try:
        r = requests.post(
            MODELS["grok"]["api_url"],
            headers={"Authorization": f"Bearer {GROK_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MODELS["grok"]["model"], "max_tokens": MAX_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] Grok: {e}"

def call_deepseek(prompt: str) -> str:
    if not DEEPSEEK_KEY:
        return "[ERROR] DEEPSEEK_KEY not set"
    try:
        r = requests.post(
            MODELS["deepseek"]["api_url"],
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MODELS["deepseek"]["model"], "max_tokens": MAX_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR] DeepSeek: {e}"

def call_ai(prompt: str, provider: str = "grok") -> str:
    if provider == "claude":   return call_claude(prompt)
    if provider == "deepseek": return call_deepseek(prompt)
    return call_grok(prompt)

# ─── Trade session runner ─────────────────────────────────────────
def run_trade_session(session: str, provider: str) -> dict:
    state = load_trade_state(provider)
    stocks = load_watchlist()
    stock_items = [s for s in stocks if s.get("type") == "stock"]

    # Update time context
    now    = now_et()
    state["_today"]  = now.strftime("%Y-%m-%d")
    state["_nowET"]  = now.strftime("%H:%M")
    state["provider"] = provider

    # Fetch prices
    prices = {}
    atr_est = {}
    for s in stock_items:
        q = get_stock_quote(s["symbol"])
        if q:
            prices[s["symbol"]] = q.get("c", q.get("pc", 0))
            state["lastPrices"][s["symbol"]] = prices[s["symbol"]]
            # Rough ATR estimate: 1% of price (will be refined by AI)
            atr_est[s["symbol"]] = prices[s["symbol"]] * 0.01

    # Fetch news
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

    wl_parts = []
    for s in stock_items:
        q = get_single_quote(s["symbol"], "stock")
        if q:
            wl_parts.append(f"{s['symbol']} ${q.get('c',0):.2f} ({q.get('dp',0):+.2f}%)")
        else:
            wl_parts.append(s["symbol"])
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

    prompt = build_prompt_v6(session, portfolio_txt, watchlist_txt,
                              news_txt, log_summary)

    # Call AI
    ai_text = call_ai(prompt, provider)

    # Parse regime from AI response and update state
    regime_str, spy_adx, spy_above = parse_regime_from_text(ai_text)
    get_market_regime(state, spy_adx, spy_above)

    # Parse ATR estimates from AI text
    for s in stock_items:
        atr = parse_atr_from_text(ai_text, s["symbol"])
        if atr:
            atr_est[s["symbol"]] = atr

    # Parse and execute decisions (skip for premarket)
    import logging as _log
    _logger = _log.getLogger("quant.session")
    decisions = []
    executed  = []

    if session == "premarket":
        _logger.info("[%s/%s] premarket — analysis only, no execution", provider, session)
    else:
        decisions = parse_ai_decisions(ai_text)
        _logger.info("[%s/%s] parsed %d decision(s): %s",
                     provider, session, len(decisions),
                     [(d["action"], d["symbol"], d["shares"], d.get("parse_mode","?"))
                      for d in decisions])

        if not decisions:
            _logger.warning("[%s/%s] NO decisions parsed — AI text preview: %s",
                            provider, session, ai_text[:400])

        # Inject confidence scores
        for d in decisions:
            if d["symbol"]:
                d["confidence"] = parse_confidence_score(ai_text, d["symbol"])

        # Log pre-execution state
        _logger.info("[%s/%s] pre-exec state: cash=$%.2f holdings=%s regime=%s",
                     provider, session, state["cash"],
                     list(state["holdings"].keys()),
                     state.get("currentRegime", "?"))

        executed = execute_decisions(decisions, state, session, prices, atr_est)

        _logger.info("[%s/%s] execution results (%d lines): %s",
                     provider, session, len(executed), executed)

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
                "detail":     f"Decision parsed but blocked by position rules or session rules",
            })

    # If NO decisions were parsed at all, record that clearly
    if not decisions and session != "premarket":
        exec_log.append({
            "status": "no_decisions_parsed",
            "detail": ("AI response did not contain a parseable DECISION block. "
                       "Expected format: BUY|SYM|N|reason or prose buy/sell intent."),
            "ai_text_preview": ai_text[:300],
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
        "ai_analysis": ai_text[:2000],
        "executed":    executed,
        "exec_log":    exec_log,
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

@app.route("/api", methods=["POST"])
def api():
    data   = request.get_json(force=True)
    action = data.get("action", "")
    try:
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

    # AI analysis (free-form)
    if action == "analyzeStock":
        provider = data.get("provider", "grok")
        return call_ai(data["prompt"], provider)

    # Trade state
    if action == "getTradeState":
        return load_trade_state(data.get("provider", "grok"))
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
        return run_trade_session(session_for_now(), "grok")

    # Session messages (keep compatible)
    if action == "getSessionMessages":
        return []
    if action == "saveSessionMessage":
        return {"id": str(int(time.time()))}
    if action == "deleteSessionMessage":
        return {"ok": True}
    if action == "clearAllSessionMessages":
        return {"ok": True}

    raise ValueError(f"Unknown action: {action}")


@app.route("/logs/<path:filename>")
def serve_log(filename):
    return send_from_directory(LOGS_DIR, filename)

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=PORT)