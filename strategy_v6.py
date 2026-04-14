"""
Strategy v6.0 — 量化可验证系统
A01_philosophy → A10_system_control

核心原则(A01):
1. 不预测,只验证 — 所有规则可量化/可回测/可复现
2. AI 是信号增强,不是决策来源 — P(up)>0.6 作为过滤器
3. 风险优先于收益 — E = P(win)×Avg(win) - P(loss)×Avg(loss)
"""

import math, re, time, logging as _logging
from datetime import datetime, timezone
from typing import Optional


# ─── Constants ────────────────────────────────────────────────────
class CFG:
    version              = "v6.0"
    INITIAL_CASH         = 10_000.0
    MIN_CASH_RATIO       = 0.20
    MAX_SINGLE_RATIO     = 0.04     # max 4% NAV per trade
    MAX_HOLDINGS         = 5
    SINGLE_TRADE_RISK    = 0.01     # risk 1% NAV per trade
    ATR_PERIOD           = 14
    STOP_ATR_MULT        = 1.5
    TRAIL_1R_MULT        = 1.5
    TRAIL_2R_MULT        = 1.0
    HARD_STOP_PCT        = 2.0
    HARD_PROFIT_PCT      = 5.0
    BREAKOUT_LOOKBACK    = 20
    VOLUME_MULT          = 1.5
    MA_TREND_SHORT       = 50
    MA_TREND_LONG        = 200
    AI_PUP_MIN           = 0.6
    AI_CONF_MIN          = 0.5
    SCORE_MIN_NORMAL     = 6
    SCORE_MIN_TRANSITION = 7
    REGIME_ADX_TREND     = 25
    REGIME_ADX_CHOP      = 20
    REGIME_CONFIRM_DAYS  = 3
    NO_TRADE_GAP_PCT     = 3.0   # reserved: skip buys on >3% gap-up (not yet enforced)
    EXEC_SLIPPAGE        = 0.002
    EXEC_COMMISSION      = 1.0
    FEEDBACK_MIN_TRADES  = 20
    FEEDBACK_EV_DROP     = 0.20
    FEEDBACK_WR_DROP     = 0.15
    TARGET_MAX_DD        = 0.20
    TARGET_SHARPE        = 1.0
    TARGET_PROFIT_FACTOR = 1.5
    TARGET_EV_MIN        = 0.0


def new_trade_state() -> dict:
    return {
        "cash": CFG.INITIAL_CASH,
        "holdings": {},
        "log": [],
        "dailyPnL": {},
        "todayTrades": {},
        "lastPrices": {},
        "currentRegime": "Trend",
        "regimeCandidate": "Trend",
        "regimeCandidateDays": 0,
        "feedbackBaseline": None,
    }


def calc_nav(state: dict) -> float:
    nav = state.get("cash", CFG.INITIAL_CASH)
    for sym, h in state.get("holdings", {}).items():
        price = state.get("lastPrices", {}).get(sym, h["avgCost"])
        nav += price * h["shares"]
    return nav


# ─── Section 1: Market Regime (A10) ──────────────────────────────
def get_market_regime(state: dict, spy_adx: float, spy_above_200ma: bool) -> str:
    if spy_adx >= CFG.REGIME_ADX_TREND and spy_above_200ma:
        candidate = "Trend"
    elif spy_adx < CFG.REGIME_ADX_CHOP:
        candidate = "Chop"
    else:
        candidate = "Transition"

    if "regimeCandidate" not in state:
        state["regimeCandidate"] = candidate
        state["regimeCandidateDays"] = 0
    if "currentRegime" not in state:
        state["currentRegime"] = "Trend"

    if candidate == state["regimeCandidate"]:
        state["regimeCandidateDays"] = state.get("regimeCandidateDays", 0) + 1
        if (state["regimeCandidateDays"] >= CFG.REGIME_CONFIRM_DAYS
                and state["currentRegime"] != candidate):
            state["currentRegime"] = candidate
    else:
        state["regimeCandidate"] = candidate
        state["regimeCandidateDays"] = 1

    return state["currentRegime"]


def check_regime_allow_trade(state: dict) -> dict:
    regime = state.get("currentRegime", "Trend")
    if regime == "Chop":
        return {"allowed": False, "reason": "[Regime=Chop] 市场震荡，禁止新开仓"}
    return {"allowed": True, "regime": regime}


# ─── Section 2: ATR Position Sizing (A04) ────────────────────────
def calc_position_size(total_assets: float, price: float, atr: float, regime: str) -> dict:
    risk_amount    = total_assets * CFG.SINGLE_TRADE_RISK
    risk_per_share = atr * CFG.STOP_ATR_MULT
    if risk_per_share <= 0:
        risk_per_share = price * 0.02
    shares = math.floor(risk_amount / risk_per_share)
    max_by_capital = math.floor((total_assets * CFG.MAX_SINGLE_RATIO) / price)
    shares = min(shares, max_by_capital)
    if regime == "Transition":
        shares = math.floor(shares * 0.5)
    shares = max(1, shares)
    return {
        "shares": shares,
        "stop_price": round(price - risk_per_share, 4),
        "risk_per_share": round(risk_per_share, 4),
        "position_value": round(shares * price, 2),
    }


# ─── Section 3: Hard Position Rules (A04) ────────────────────────
def check_position_rules(state: dict, sym: str, shares: int, price: float) -> dict:
    total_assets = calc_nav(state)
    holdings = state.get("holdings", {})

    if sym not in holdings and len(holdings) >= CFG.MAX_HOLDINGS:
        return {"shares": 0, "skip": True,
                "reason": f"持仓已达上限 {CFG.MAX_HOLDINGS} 只，跳过 {sym}"}

    rc = check_regime_allow_trade(state)
    if not rc["allowed"]:
        return {"shares": 0, "skip": True, "reason": rc["reason"]}

    min_cash = total_assets * CFG.MIN_CASH_RATIO
    usable = state["cash"] - min_cash
    if usable <= 0:
        return {"shares": 0, "skip": True, "reason": f"现金低于20%底线，跳过{sym}"}
    shares = min(shares, math.floor(usable / price))
    if shares <= 0:
        return {"shares": 0, "skip": True, "reason": f"买入后现金低于20%底线，跳过{sym}"}

    if sym in holdings and price < holdings[sym]["avgCost"]:
        return {"shares": 0, "skip": True,
                "reason": f"{sym}禁止加仓摊平（A04）"}

    return {"shares": shares, "skip": False, "reason": ""}


# ─── Section 4: Trailing Stop / Auto Exits (A04) ─────────────────
def check_auto_stop_rules(state: dict, session: str) -> list:
    sells = []
    _logger = None  # lazy-init only if needed to avoid import overhead
    for sym, h in state.get("holdings", {}).items():
        last_prices = state.get("lastPrices", {})
        if sym not in last_prices:
            # R4: price unavailable — skip stop check rather than using avgCost
            # (avgCost always gives pnl_pct=0 and silently disables stop logic)
            _logging.getLogger("quant.stops").warning(
                "No price for %s in lastPrices — stop check skipped this cycle. "
                "Ensure price feed is healthy.", sym)
            continue
        price = last_prices[sym]
        atr   = h.get("entryAtr", h["avgCost"] * 0.02)
        h["highPrice"] = max(h.get("highPrice", price), price)

        pnl_pct = (price - h["avgCost"]) / h["avgCost"] * 100
        risk_per = h.get("riskPerShare", atr * CFG.STOP_ATR_MULT)
        unr = (price - h["avgCost"]) / risk_per if risk_per else 0

        current_stop = h.get("stopPrice", h["avgCost"] - risk_per)
        new_stop = current_stop
        if unr >= 2:
            new_stop = max(new_stop, h["highPrice"] - atr * CFG.TRAIL_2R_MULT)
        elif unr >= 1:
            new_stop = max(new_stop, h["highPrice"] - atr * CFG.TRAIL_1R_MULT)
        if new_stop > h.get("stopPrice", float("-inf")):
            h["stopPrice"] = new_stop

        if price <= h.get("stopPrice", float("-inf")):
            tag = "TRAIL_STOP_PROFIT" if price >= h["avgCost"] else "STOP_LOSS"
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"追踪止损${h['stopPrice']:.2f}（{unr:+.2f}R）", "tag": tag})
            continue

        # Layer 3B: skip hard stop during the first 30 min after entry to avoid
        # being stopped out by normal open-session volatility on a fresh position.
        now_et    = state.get("_nowET", "")
        entry_et  = h.get("entryTime", "")
        in_open_guard = False
        if now_et and entry_et:
            try:
                nh, nm = int(now_et[:2]),   int(now_et[3:5])
                eh, em = int(entry_et[:2]), int(entry_et[3:5])
                mins_since = (nh * 60 + nm) - (eh * 60 + em)
                in_open_guard = 0 <= mins_since <= 30
            except Exception:
                pass

        # Layer 3C: ATR-relative hard stop floor — prevents the fixed 2% floor from
        # firing before the intended ATR-based stop (e.g. ATR stop at 3.5% > 2%).
        atr_stop_pct    = (risk_per / h["avgCost"]) * 100 if h["avgCost"] > 0 else CFG.HARD_STOP_PCT
        dynamic_hard_stop = max(CFG.HARD_STOP_PCT, atr_stop_pct)

        if not in_open_guard and pnl_pct <= -dynamic_hard_stop:
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"硬止损-{dynamic_hard_stop:.1f}%", "tag": "HARD_STOP"})
            continue
        if pnl_pct >= CFG.HARD_PROFIT_PCT:
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": f"硬止盈+{CFG.HARD_PROFIT_PCT}%", "tag": "HARD_PROFIT"})
            continue
        if session == "closing" and state.get("currentRegime") == "Chop":
            sells.append({"sym": sym, "shares": h["shares"],
                          "reason": "Regime=Chop强制退出（A10）", "tag": "REGIME_EXIT"})
    return sells


# ─── Section 5: Expectancy & Metrics (A06/A08) ───────────────────
def calc_expectancy(trade_log: list) -> dict:
    closed = [e for e in trade_log
              if e.get("action", "").upper() == "SELL"
              and e.get("realized_pnl") is not None]
    if not closed:
        return {"expectancy": 0, "winRate": 0, "totalTrades": 0,
                "avgWin": 0, "avgLoss": 0, "profitFactor": 0,
                "grossWin": 0, "grossLoss": 0}
    wins   = [e for e in closed if e["realized_pnl"] > 0]
    losses = [e for e in closed if e["realized_pnl"] <= 0]
    avg_win  = sum(e["realized_pnl"] for e in wins)   / len(wins)   if wins   else 0
    avg_loss = abs(sum(e["realized_pnl"] for e in losses) / len(losses)) if losses else 0
    p_win = len(wins) / len(closed)
    E     = p_win * avg_win - (1 - p_win) * avg_loss
    gw    = sum(e["realized_pnl"] for e in wins)
    gl    = abs(sum(e["realized_pnl"] for e in losses))
    pf    = gw / gl if gl > 0 else (99 if gw > 0 else 0)
    return {"expectancy": round(E, 2), "winRate": round(p_win * 100, 1),
            "avgWin": round(avg_win, 2), "avgLoss": round(avg_loss, 2),
            "profitFactor": round(pf, 2), "totalTrades": len(closed),
            "grossWin": round(gw, 2), "grossLoss": round(gl, 2)}


def calc_max_drawdown(equity_curve: list) -> float:
    if len(equity_curve) < 2:
        return 0.0
    peak, max_dd = equity_curve[0]["totalValue"], 0.0
    for pt in equity_curve:
        if pt["totalValue"] > peak:
            peak = pt["totalValue"]
        dd = (peak - pt["totalValue"]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 2)


def get_quant_metrics(state: dict) -> dict:
    metrics = calc_expectancy(state.get("log", []))
    curve, cum = [], CFG.INITIAL_CASH
    for d in sorted(state.get("dailyPnL", {}).keys()):
        cum += state["dailyPnL"][d]
        curve.append({"date": d, "totalValue": cum})
    # Replace (or add) today's data point with the real mark-to-market NAV so
    # the equity curve reflects unrealized gains, not just closed daily PnL.
    today = datetime.now().strftime("%Y-%m-%d")
    current_nav = round(calc_nav(state), 2)
    if curve and curve[-1]["date"] == today:
        curve[-1]["totalValue"] = current_nav
    else:
        curve.append({"date": today, "totalValue": current_nav})
    max_dd = calc_max_drawdown(curve)
    fb = check_feedback_trigger(state)
    return {
        "metrics": metrics, "maxDrawdown": max_dd,
        "currentRegime": state.get("currentRegime", "Unknown"),
        "feedbackAlert": fb, "equityCurve": curve,
        "targets": {
            "ev_ok": metrics["expectancy"] > CFG.TARGET_EV_MIN,
            "dd_ok": max_dd < (CFG.TARGET_MAX_DD * 100),  # max_dd is %, TARGET_MAX_DD is ratio → *100
            "pf_ok": metrics["profitFactor"] >= CFG.TARGET_PROFIT_FACTOR,
        },
    }


# ─── Section 6: Feedback Trigger (A07) ───────────────────────────
def check_feedback_trigger(state: dict) -> Optional[dict]:
    metrics = calc_expectancy(state.get("log", []))
    if metrics["totalTrades"] < CFG.FEEDBACK_MIN_TRADES:
        return None
    baseline = state.get("feedbackBaseline")
    if not baseline:
        state["feedbackBaseline"] = metrics
        return None
    ev_drop = ((baseline["expectancy"] - metrics["expectancy"])
               / abs(baseline["expectancy"])) if baseline["expectancy"] > 0 else 0
    wr_drop = (baseline["winRate"] - metrics["winRate"]) / 100
    if ev_drop >= CFG.FEEDBACK_EV_DROP:
        return {"needReview": True,
                "reason": f"期望值下降{ev_drop*100:.0f}%（{baseline['expectancy']}→{metrics['expectancy']}）"}
    if wr_drop >= CFG.FEEDBACK_WR_DROP:
        return {"needReview": True,
                "reason": f"胜率下降{wr_drop*100:.0f}%（{baseline['winRate']}%→{metrics['winRate']}%）"}
    return None


# ─── Section 7: Operating Rules (A09) ────────────────────────────
def check_operating_rules(state: dict) -> dict:
    violations = []
    for sym, h in state.get("holdings", {}).items():
        if h.get("stopPrice") is None:
            violations.append(f"{sym} 持仓无止损价记录")
    fb = check_feedback_trigger(state)
    if fb:
        violations.append(f"⚠️ 反馈系统触发：{fb['reason']}")
    return {"pass": len(violations) == 0, "violations": violations}


# ─── Section 8: AI Response Parser ───────────────────────────────
def parse_ai_decisions(ai_text):
    """
    Parse DECISION block from AI text. Handles:
      1. Standard pipe format:   BUY|SYM|N|reason
      2. Spaces around pipes:    BUY | SYM | N | reason
      3. Markdown bold action:   **BUY**|SYM|N|reason
      4. Bold DECISION header:   **DECISION:** BUY|SYM|N|reason
      5. Chinese header:         决策: BUY|SYM|N|reason
      6. Markdown code fence:    ```\\nDECISION:\\n...\\n```
      7. Prose fallback:         AI writes "决定入场AAPL" / "buy AAPL" in natural language

    Returns list of {action, symbol, shares, reason, parse_mode}
    parse_mode: 'structured' | 'prose_fallback'
    """
    import re as _re

    # Pre-process: strip markdown code fences so they don't pollute block lines
    ai_text = _re.sub(r"```[a-zA-Z]*\n?", "", ai_text)

    # ── Pass 1: structured pipe format (preferred) ────────────────
    decisions = []
    block_lines = []
    in_block = False
    blank_count = 0  # allow up to 1 blank line inside block before stopping

    # Match both English "DECISION:" and Chinese "决策:"
    _BLOCK_START = _re.compile(r"(?:DECISION|决策)\s*[:：]", _re.IGNORECASE)

    for line in ai_text.split("\n"):
        if _BLOCK_START.search(line):
            in_block = True
            blank_count = 0
            rest = _BLOCK_START.sub("", line)
            # Strip surrounding markdown bold markers
            rest = _re.sub(r"\*{1,2}", "", rest).strip()
            if rest:
                block_lines.append(rest)
        elif in_block:
            stripped = line.strip()
            # Strip leading list/bullet markers: *, -, –, •, ▸, ▹, →, ①②③, "1. ", "2) "
            stripped = _re.sub(r"^[\*\-\u2013\u2022\u25b8\u25b9\u2192\u2460-\u2473]+\s*", "", stripped)
            stripped = _re.sub(r"^\d+[\.\)]\s*", "", stripped)  # "1. " or "1) "
            if stripped == "":
                blank_count += 1
                # Allow one blank line (e.g. between header and first decision row),
                # but stop if we already have decisions and hit a second blank line.
                if block_lines and blank_count > 1:
                    break
            else:
                blank_count = 0
                block_lines.append(stripped)

    for raw in block_lines:
        # Normalise: strip bold markers (**...**) and spaces around pipes
        normalised = _re.sub(r"\*{1,2}", "", raw)
        normalised = _re.sub(r"\s*\|\s*", "|", normalised).strip()
        m = _re.match(
            r"(BUY|SELL|HOLD)\|([A-Z0-9.]{0,12})\|(\d*)\|?(.*)",
            normalised, _re.IGNORECASE
        )
        if m:
            shares_str = m.group(3)
            decisions.append({
                "action":     m.group(1).upper(),
                "symbol":     m.group(2).upper(),
                "shares":     int(shares_str) if shares_str.isdigit() else 0,
                "reason":     m.group(4).strip(),
                "parse_mode": "structured",
            })
            continue
        # Handle bare "HOLD" line (AI omits pipes when no positions to manage)
        if _re.match(r"^HOLD\s*$", normalised, _re.IGNORECASE):
            decisions.append({
                "action":     "HOLD",
                "symbol":     "",
                "shares":     0,
                "reason":     "no_action",
                "parse_mode": "structured",
            })

    if decisions:
        return decisions

    # ── Pass 2: prose fallback ────────────────────────────────────
    # AI wrote natural language instead of pipe format.
    # We extract the intent conservatively (only clear BUY/SELL signals).
    prose = []

    # Find all stock ticker mentions near buy/sell intent keywords
    # Patterns: "入场AAPL" / "买入AAPL" / "buy AAPL" / "决定买AAPL"
    #           "卖出AAPL" / "sell AAPL" / "平仓AAPL"
    buy_cn  = r"(?:入场|买入|建仓|做多|开仓|购买|决定买)"
    sell_cn = r"(?:卖出|平仓|止盈|止损|减仓|清仓|做空)"
    buy_en  = r"(?:buy|long|enter|purchase)"
    sell_en = r"(?:sell|close|exit|short)"
    ticker  = r"([A-Z]{1,5}(?:\.[A-Z]{0,2})?)"

    buy_pattern  = _re.compile(
        rf"(?:{buy_cn}|{buy_en})\s*{ticker}", _re.IGNORECASE)
    sell_pattern = _re.compile(
        rf"(?:{sell_cn}|{sell_en})\s*{ticker}", _re.IGNORECASE)
    # Also catch "ticker + buy intent" order: "AAPL 入场" / "AAPL buy"
    rev_buy  = _re.compile(
        rf"{ticker}\s*(?:{buy_cn}|{buy_en})", _re.IGNORECASE)
    rev_sell = _re.compile(
        rf"{ticker}\s*(?:{sell_cn}|{sell_en})", _re.IGNORECASE)

    # Extract shares if mentioned: "33股" / "33 shares" / "33 lots"
    shares_pat = _re.compile(r"(\d+)\s*(?:股|shares?|lots?)", _re.IGNORECASE)
    # Extract price if mentioned: "$260.48" / "260.48"
    seen = set()
    full_text = ai_text

    for pat, action in [(buy_pattern, "BUY"), (rev_buy, "BUY"),
                        (sell_pattern, "SELL"), (rev_sell, "SELL")]:
        for m in pat.finditer(full_text):
            sym = m.group(1).upper()
            # Filter out common false positives (Chinese words, single chars)
            if len(sym) < 2 or sym in {"SE", "A", "I", "AI", "IT"}:
                # SE is valid (Sea Ltd), include it if it appears near intent
                if sym != "SE":
                    continue
            key = (action, sym)
            if key in seen:
                continue
            seen.add(key)

            # Try to extract shares from surrounding context (±200 chars)
            ctx_start = max(0, m.start() - 200)
            ctx_end   = min(len(full_text), m.end() + 200)
            ctx       = full_text[ctx_start:ctx_end]

            ctx_clean = ctx.replace(",", "")  # strip comma-separators ($2,500 → $2500)
            shares_m  = shares_pat.search(ctx_clean)
            shares    = int(shares_m.group(1)) if shares_m else 0

            prose.append({
                "action":     action,
                "symbol":     sym,
                "shares":     shares,
                "reason":     f"[prose fallback] {ctx.strip()[:120]}",
                "parse_mode": "prose_fallback",
            })

    return prose


def parse_confidence_score(ai_text: str, symbol: str) -> int:
    """
    Extract confidence score for a symbol.
    Handles spaces/newlines between symbol and C: marker (H1 fix).
    Search order:
      1. Symbol … C:N/10 (same line or nearby, DOTALL)
      2. Any line containing the symbol → first N/10 on that line
      3. Global N/10 fallback (last resort)
    """
    # Pass 1: flexible DOTALL match within 300 chars after symbol
    esc_sym = re.escape(symbol)
    m = re.search(
        rf'{esc_sym}[\s\S]{{0,300}}?(?:C:|置信度[：:])\s*(\d+)/10',
        ai_text, re.IGNORECASE
    )
    if m:
        return int(m.group(1))
    # Pass 2: any line containing the symbol
    for line in ai_text.split('\n'):
        if symbol.upper() in line.upper():
            m2 = re.search(r'(\d+)/10', line)
            if m2:
                return int(m2.group(1))
    # Pass 3: global scan (e.g. only one symbol in response)
    m3 = re.search(r'(?:C:|置信度[：:])\s*(\d+)/10', ai_text, re.IGNORECASE)
    if m3:
        return int(m3.group(1))
    return 0


def parse_regime_from_text(ai_text: str) -> tuple:
    regime = "Trend"
    spy_adx = 25.0
    spy_above = True
    m = re.search(r'Regime\s*[:：]\s*(Trend|Chop|Transition)', ai_text, re.IGNORECASE)
    if m:
        regime = m.group(1).capitalize()

    # Bug-fix: require a separator (=/:/ /≈) *after* the optional "(14)" period label so
    # "ADX(14)=28.5" captures 28.5 (not 14), and bare "ADX(14)" yields no match.
    m_adx = re.search(
        r'ADX\s*(?:\(\d+\))?\s*(?:[=:：≈]|\s)\s*(\d+\.?\d*)',
        ai_text, re.IGNORECASE
    )
    if m_adx:
        spy_adx = float(m_adx.group(1))

    # Regime-override: Chop/Transition always force their synthetic ADX.
    # For Trend: trust parsed ADX only when >= threshold — prevents a bad parse
    # from silently downgrading a stated "Trend" to Chop inside get_market_regime.
    if regime == "Chop":
        spy_adx = 15.0
        spy_above = False          # Chop: price below 200MA
    elif regime == "Transition":
        spy_adx = 22.0
        spy_above = True           # Transition: price near 200MA, assume above
    elif regime == "Trend" and spy_adx < CFG.REGIME_ADX_TREND:
        spy_adx = CFG.REGIME_ADX_TREND  # clamp: trust "Trend" label over a low parsed ADX
    return regime, spy_adx, spy_above


def parse_atr_from_text(ai_text: str, symbol: str) -> Optional[float]:
    """
    Extract ATR value from AI text.
    H5 fix: use non-greedy match + explicit decimal pattern to avoid
    dropping decimal part (e.g. "$1.50" should not be captured as "1").
    Pattern: ATR ... $? digits.digits (require at least one decimal digit).
    """
    # Precise pattern: ATR followed by optional text, then $N.NN
    _atr_pat = re.compile(
        r'ATR[^\n]{0,60}?\$?\s*(\d+\.\d+|\d+)',
        re.IGNORECASE
    )
    for line in ai_text.split('\n'):
        if symbol.upper() in line.upper():
            m = _atr_pat.search(line)
            if m:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    return val
    return None  # R3: no global fallback — wrong symbol ATR is worse than None


# ─── Section 9: Prompt Builder (A03+A10) ─────────────────────────
_COMMON = ("风控: 仓位=净值×1%÷(1.5×ATR)|止损=Entry-1.5×ATR|硬止损-2%|硬止盈+5%\n"
           "追踪: 盈≥1R→最高价-1.5ATR|盈≥2R→最高价-1.0ATR|禁扩止损/禁摊平\n"
           "置信度: ≥6入场 <6观望|三要素①趋势②Breakout放量③P(up)>0.6\n")
_SCORE  = "▸ SYM|↑↓→|C:X/10|①趋势Y/N ②量价Y/N ③P(up)=0.X\n"
_DEC    = "DECISION:\nBUY|SYM|N|信号类型+C:X+ATR=$X+止损=$X\nSELL|SYM|N|理由\nHOLD||0|原因\n"


def build_prompt_v6(session: str, portfolio: str, watchlist_text: str,
                    news_summary: str, log_summary: str = "", focus_note: str = "") -> str:
    if session == "premarket":
        return (f"量化交易员 9:15ET 盘前｜只分析不交易\n\n账户: {portfolio}\n\n"
                f"股票:\n{watchlist_text}{focus_note}\n\n新闻:\n{news_summary}\n\n"
                + _COMMON
                + "Regime判断(A10): SPY ADX(14)→Trend(>25)/Transition(20-25)/Chop(<20)\n"
                "Chop=禁新仓|Transition=置信度提至C:7+\n\n"
                "输出:\n📊 Regime: [Trend/Transition/Chop] SPY:[简述]\n\n"
                + _SCORE + "  ATR(14)估算:$X|新闻:1句\n\nNEXT_ACTION: 今日策略(30字内)")
    if session == "opening":
        return (f"量化交易员 10:00ET 开盘30min后 黄金入场\n\n账户: {portfolio}\n\n"
                f"股票:\n{watchlist_text}{focus_note}\n\n新闻:\n{news_summary}\n\n"
                + _COMMON + _SCORE + "\n" + _DEC + "\nNEXT_ACTION: 下一步观察重点")
    if session == "mid":
        return (f"量化交易员 12:00ET 中盘复盘\n\n账户: {portfolio}\n\n"
                f"今日交易:\n{log_summary}\n\n报价:\n{watchlist_text}\n\n"
                + _COMMON
                + "持仓评估: ▸ SYM|盈亏%|≈XR|止损=$X|建议\n\n"
                "DECISION:\nSELL|SYM|N|理由\nBUY|SYM|N|信号+C:X\nHOLD||0|说明\n\n"
                "NEXT_ACTION: 收尾策略")
    if session == "closing":
        return (f"量化交易员 15:30ET 收尾｜禁新开仓\n\n账户: {portfolio}\n\n"
                f"今日交易:\n{log_summary}\n\n持仓报价:\n{watchlist_text}\n\n"
                "过夜4条件: ①当日盈利 ②无重大宏观 ③无隔夜财报 ④Regime≠Chop\n\n"
                "每仓: ▸ SYM|盈亏%|XR|决定:过夜/平仓|理由\n\n"
                "DECISION:\nSELL|SYM|N|理由\nHOLD||0|原因\n\n"
                "NEXT_ACTION: 明日盘前重点")
    return ""


# ─── Section 10: Trade Log Entry Builder (A05/A08) ───────────────
def build_trade_log_entry(action: str, trade_info: dict, state: dict, tag: str = "") -> dict:
    sym    = trade_info.get("sym", "")
    h      = state.get("holdings", {}).get(sym, {})
    reason = trade_info.get("reason", "").lower()
    r_val  = None
    if action.lower() == "sell" and trade_info.get("realizedPnl") is not None:
        risk = h.get("riskPerShare", 0) * trade_info.get("shares", 1)
        if risk > 0:
            r_val = round(trade_info["realizedPnl"] / risk, 2)
    sig = "unknown"
    if "breakout" in reason: sig = "breakout"
    elif "trend" in reason:  sig = "trend"
    elif "pullback" in reason: sig = "pullback"
    elif "volume" in reason or "量价" in reason: sig = "volume_breakout"
    plan  = any(k in reason for k in ["plan", "盘前", "计划"])
    fomo  = any(k in reason for k in ["fomo", "追涨"])
    viol  = "fomo" if fomo else ("counter_trend" if "counter" in reason else "none")
    now   = datetime.now(timezone.utc)
    return {
        "id": f"trade_{int(time.time()*1000)}_{sym}",
        "timestamp": now.isoformat(),
        "date": state.get("_today", now.strftime("%Y-%m-%d")),
        "time_et": state.get("_nowET", ""),
        "session": trade_info.get("session", ""),
        "ai_provider": state.get("provider", "grok"),
        "action": action.upper(),
        "symbol": sym,
        "shares": trade_info.get("shares", 0),
        "price": trade_info.get("price", 0),
        "cost": round(trade_info["price"] * trade_info["shares"], 2) if action.lower() == "buy" else None,
        "proceeds": round(trade_info["price"] * trade_info["shares"], 2) if action.lower() == "sell" else None,
        "realized_pnl": round(trade_info["realizedPnl"], 2) if trade_info.get("realizedPnl") is not None else None,
        "signal_type": sig, "regime": state.get("currentRegime", "Unknown"),
        "r_value": r_val, "stop_price": h.get("stopPrice"),
        "entry_atr": h.get("entryAtr"), "confidence": trade_info.get("confidence"),
        "is_plan_trade": plan, "is_fomo": fomo, "violation": viol,
        "exit_tag": tag or None, "reason": trade_info.get("reason", ""),
    }


# ─── Section 11: Execution Engine ────────────────────────────────
def execute_decisions(decisions: list, state: dict, session: str,
                      prices: dict, atr_estimates: dict) -> list:
    executed = []
    holdings = state.setdefault("holdings", {})
    log      = state.setdefault("log", [])
    today    = state.get("_today", "")
    today_trades = state.setdefault("todayTrades", {})

    def tkey(sym):
        return f"{today}:{sym}"

    # Auto stop/profit first
    for s in check_auto_stop_rules(state, session):
        sym = s["sym"]
        if sym not in holdings:
            continue
        h     = holdings[sym]
        price = prices.get(sym, h["avgCost"])
        real  = (price - h["avgCost"]) * s["shares"]
        entry = build_trade_log_entry("sell", {
            "sym": sym, "shares": s["shares"], "price": price,
            "realizedPnl": real, "reason": s["reason"], "session": session,
        }, state, s.get("tag", ""))
        log.append(entry)
        state["cash"] += price * s["shares"]
        state.setdefault("dailyPnL", {})[today] = (
            state["dailyPnL"].get(today, 0) + real)
        if s["shares"] >= h["shares"]:
            del holdings[sym]
        else:
            h["shares"] -= s["shares"]
        sign = "盈" if real >= 0 else "亏"
        executed.append(f"✅ 系统{s.get('tag','')} {sym} {s['shares']}股 @${price:.2f} "
                        f"{sign}${abs(real):.2f}")

    for d in decisions:
        action, sym, shares, reason = d["action"], d["symbol"], d["shares"], d["reason"]
        conf = d.get("confidence", 0)
        if action == "HOLD":
            label = f" {sym}" if sym else ""
            executed.append(f"✅ HOLD{label} — {reason[:80]}")
            continue
        if not sym:
            continue

        tk = tkey(sym)
        if today_trades.get(tk, 0) >= 2:
            executed.append(f"⚠️ {sym} 今日已交易2次，跳过"); continue

        price = prices.get(sym, 0)
        if price <= 0:
            executed.append(f"⚠️ {sym} 无有效价格，跳过"); continue

        if action == "BUY":
            if session == "closing":
                executed.append(f"⚠️ {sym} 收尾禁止新开仓"); continue
            regime = state.get("currentRegime", "Trend")
            min_conf = CFG.SCORE_MIN_TRANSITION if regime == "Transition" else CFG.SCORE_MIN_NORMAL
            if conf < min_conf:
                executed.append(f"⚠️ {sym} 置信度C:{conf}<{min_conf}，跳过"); continue
            atr    = atr_estimates.get(sym, price * 0.02)
            sizing = calc_position_size(calc_nav(state), price, atr, regime)
            shares = min(shares, sizing["shares"]) if shares > 0 else sizing["shares"]
            rule   = check_position_rules(state, sym, shares, price)
            if rule["skip"]:
                executed.append(f"⚠️ {sym}: {rule['reason']}"); continue
            shares = rule["shares"]
            state["cash"] -= price * shares
            today_trades[tk] = today_trades.get(tk, 0) + 1
            if sym in holdings:
                h = holdings[sym]
                total = h["shares"] + shares
                h["avgCost"] = (h["avgCost"] * h["shares"] + price * shares) / total
                h["shares"] = total
            else:
                holdings[sym] = {
                    "shares": shares, "avgCost": price,
                    "stopPrice": sizing["stop_price"],
                    "entryAtr": atr, "riskPerShare": sizing["risk_per_share"],
                    "highPrice": price,
                    "entryTime": state.get("_nowET", ""),  # ET "HH:MM" for 30-min hard-stop guard
                }
            log.append(build_trade_log_entry("buy", {
                "sym": sym, "shares": shares, "price": price,
                "realizedPnl": None, "reason": reason, "confidence": conf, "session": session,
            }, state))
            executed.append(f"✅ 买入 {sym} {shares}股 @${price:.2f} "
                            f"花费${price*shares:.2f} C:{conf}/10 [{regime}]")

        elif action == "SELL":
            if sym not in holdings:
                executed.append(f"⚠️ {sym} 未持仓，跳过"); continue
            h = holdings[sym]
            sell_sh = min(shares, h["shares"]) if shares > 0 else h["shares"]
            real    = (price - h["avgCost"]) * sell_sh
            state["cash"] += price * sell_sh
            today_trades[tk] = today_trades.get(tk, 0) + 1
            state.setdefault("dailyPnL", {})[today] = (
                state["dailyPnL"].get(today, 0) + real)
            log.append(build_trade_log_entry("sell", {
                "sym": sym, "shares": sell_sh, "price": price,
                "realizedPnl": real, "reason": reason, "confidence": conf, "session": session,
            }, state))
            if sell_sh >= h["shares"]:
                del holdings[sym]
            else:
                h["shares"] -= sell_sh
            sign = "盈" if real >= 0 else "亏"
            executed.append(f"✅ 卖出 {sym} {sell_sh}股 @${price:.2f} {sign}${abs(real):.2f}")

    if len(log) > 500:
        state["log"] = log[-500:]
    return executed
