"""
weekly_review.py — Weekend Feedback Loop & Watchlist Suggestions

Feature 1 — Trade decision quality analysis (runs every Saturday):
  • Reads the past Mon-Fri trade logs for all three AI providers
  • Fetches end-of-week prices so each closed/open trade has an outcome
  • Classifies decisions: good / bad / neutral based on realized or unrealized PnL
  • Sends results to Claude for a narrative report + per-model improvement suggestions
  • Persists report to logs as prefix="feedback"

Feature 2 — Watchlist suggestions (runs every Saturday, after Feature 1):
  • Fetches broad market / sector-ETF performance (XLK, XLF, XLE …)
  • Pulls general market news from Finnhub
  • AI (Claude) analyses trends and recommends sectors + specific stocks to add/remove
  • Parses structured ADD|SYMBOL|… / REMOVE|SYMBOL|… lines for easy UI rendering
  • Persists report to logs as prefix="suggestions"
"""

import json
import time
import requests
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger("quant.weekly")

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def most_recent_week() -> tuple:
    """
    Return (monday_str, friday_str) for the most recently completed Mon-Fri week.
    Correct whether called on Saturday or Sunday.
    """
    now = datetime.now(timezone.utc)
    # weekday(): Mon=0 … Fri=4, Sat=5, Sun=6
    dow = now.weekday()
    if dow == 5:   # Saturday → last Mon = 5 days ago
        days_to_monday = 5
    elif dow == 6: # Sunday → last Mon = 6 days ago
        days_to_monday = 6
    else:          # weekday → go back to previous Mon
        days_to_monday = dow + 7
    last_monday = now - timedelta(days=days_to_monday)
    last_friday = last_monday + timedelta(days=4)
    return last_monday.strftime("%Y-%m-%d"), last_friday.strftime("%Y-%m-%d")


def _pct(a: float, b: float) -> float:
    """Safe percentage change (b - a) / a × 100."""
    return (b - a) / a * 100.0 if a else 0.0


# ─────────────────────────────────────────────────────────────────────
# Feature 1 — Trade Decision Analysis
# ─────────────────────────────────────────────────────────────────────

def analyze_trade_decisions(trades: list, price_fn: Callable) -> dict:
    """
    Match BUY records with their SELL/STOP counterparts and evaluate quality.

    For closed trades (BUY → SELL pair): uses realized PnL / return %.
    For stop-loss exits: additionally fetches current price to judge timeliness.
    For still-open positions: fetches current price for unrealized PnL.

    Returns:
        {
          "grok":     [decision_dict, …],
          "claude":   […],
          "deepseek": […],
        }
    """
    # Group open buys by "provider:symbol" key (FIFO queue per key)
    open_buys: dict = {}
    decisions: dict = {}

    for trade in sorted(trades, key=lambda x: x.get("timestamp", "")):
        prov = trade.get("ai_provider", "unknown")
        sym  = trade.get("symbol", "")
        act  = trade.get("action", "")
        key  = f"{prov}:{sym}"

        decisions.setdefault(prov, [])

        if act == "BUY":
            open_buys.setdefault(key, []).append(trade)

        elif act in ("SELL", "STOP"):
            buy_t = (open_buys.get(key) or [None]).pop(0) if open_buys.get(key) else None
            buy_price  = (buy_t or {}).get("price", trade.get("price", 0))
            sell_price = trade.get("price", 0)
            realized   = trade.get("realized_pnl") or 0
            ret_pct    = _pct(buy_price, sell_price)
            exit_tag   = trade.get("exit_tag", "")

            entry = {
                "symbol":       sym,
                "action_type":  "closed_trade",
                "buy_date":     (buy_t or {}).get("date", "?"),
                "sell_date":    trade.get("date"),
                "buy_price":    buy_price,
                "sell_price":   sell_price,
                "shares":       (buy_t or {}).get("shares", trade.get("shares", 0)),
                "return_pct":   round(ret_pct, 2),
                "realized_pnl": round(realized, 2),
                "exit_tag":     exit_tag,
                "buy_reason":   (buy_t or {}).get("reason", ""),
                "sell_reason":  trade.get("reason", ""),
                "confidence":   (buy_t or {}).get("confidence", 0),
                "regime":       (buy_t or {}).get("regime", trade.get("regime", "")),
                "quality": (
                    "good"    if ret_pct > 0.5
                    else "bad" if ret_pct < -0.5
                    else "neutral"
                ),
            }

            # Extra verdict for stop-loss exits: is the stock still falling?
            if act == "STOP" or exit_tag in ("stop_loss", "hard_stop", "auto_stop", "atr_stop"):
                try:
                    q = price_fn(sym)
                    if q:
                        current = q.get("c", sell_price)
                        entry["current_price_eow"] = round(current, 2)
                        entry["stop_verdict"] = (
                            "good_stop"      if current < sell_price
                            else "premature_stop"
                        )
                except Exception:
                    pass

            decisions[prov].append(entry)

    # Handle positions still open at end of week
    for key, buy_list in open_buys.items():
        prov, sym = key.split(":", 1)
        q = None
        try:
            q = price_fn(sym)
        except Exception:
            pass

        for buy_t in buy_list:
            buy_price   = buy_t.get("price", 0)
            current     = (q or {}).get("c", buy_price) if q else buy_price
            unrealized  = _pct(buy_price, current)

            entry = {
                "symbol":        sym,
                "action_type":   "open_position",
                "buy_date":      buy_t.get("date", "?"),
                "sell_date":     None,
                "buy_price":     buy_price,
                "current_price": round(current, 2),
                "shares":        buy_t.get("shares", 0),
                "return_pct":    round(unrealized, 2),
                "realized_pnl":  None,
                "buy_reason":    buy_t.get("reason", ""),
                "confidence":    buy_t.get("confidence", 0),
                "regime":        buy_t.get("regime", ""),
                "quality": (
                    "good"    if unrealized > 0.5
                    else "bad" if unrealized < -1.0
                    else "neutral"
                ),
            }
            decisions.setdefault(prov, []).append(entry)

    return decisions


def _fmt_decision(d: dict) -> str:
    """One-line summary of a single trade decision for the AI prompt."""
    sym   = d.get("symbol", "?")
    typ   = d.get("action_type", "?")
    ret   = d.get("return_pct", 0)
    bp    = d.get("buy_price", 0)
    sp    = d.get("sell_price") or d.get("current_price", 0)
    conf  = d.get("confidence", "?")
    reg   = d.get("regime", "?")
    buy_r = (d.get("buy_reason") or "")[:100]
    sell_r = (d.get("sell_reason") or "")[:80]
    tag   = d.get("stop_verdict", "")

    line = f"  • {sym} [{typ}] 买入${bp:.2f}"
    if sp:
        label = "→卖出" if typ == "closed_trade" else "→当前"
        line += f" {label}${sp:.2f} ({ret:+.1f}%)"
    line += f" | 信心:{conf}/10 | Regime:{reg}"
    if buy_r:
        line += f"\n    买理由: {buy_r}"
    if sell_r:
        line += f"\n    卖理由: {sell_r}"
    if tag:
        verdict = "✓正确止损(股价继续下行)" if tag == "good_stop" else "✗过早止损(股价随后反弹)"
        line += f"\n    止损评价: {verdict}"
    return line


def build_feedback_prompt(provider_name: str,
                           decisions: list,
                           from_date: str,
                           to_date: str) -> str:
    """
    Build the analyst prompt that asks Claude to narrate one provider's
    weekly decisions and give improvement recommendations.
    """
    good    = [d for d in decisions if d.get("quality") == "good"]
    bad     = [d for d in decisions if d.get("quality") == "bad"]
    neutral = [d for d in decisions if d.get("quality") == "neutral"]
    total   = len(decisions)
    win_rt  = len(good) / total * 100 if total else 0

    good_txt    = "\n".join(_fmt_decision(d) for d in good)    or "  （本周无盈利交易）"
    bad_txt     = "\n".join(_fmt_decision(d) for d in bad)     or "  （本周无亏损交易）"
    neutral_txt = "\n".join(_fmt_decision(d) for d in neutral) or "  （无）"

    return f"""你是一位量化交易专家兼AI模型评估师。
请对 {provider_name} AI模型在 {from_date} 至 {to_date} 一周内的股票交易决策进行深入复盘。

═══════════════════════════════════
{provider_name} 本周决策汇总
═══════════════════════════════════
总决策数: {total} | 胜率: {win_rt:.0f}% ({len(good)}胜 / {len(bad)}败 / {len(neutral)}平)

✅ 好的决策 ({len(good)}个):
{good_txt}

❌ 错误决策 ({len(bad)}个):
{bad_txt}

⚖️ 中性结果 ({len(neutral)}个):
{neutral_txt}

═══════════════════════════════════
请按以下结构输出复盘报告：

## 本周决策质量总结
[1-2句总评，点出最突出的优点和问题]

## 好的决策分析
[逐条说明为何正确：信号可靠性、入场时机、Regime判断、止损设置是否合理]

## 错误决策分析
[逐条说明根本原因：信号误判？Regime错判？仓位过重？情绪性交易？]

## 止损执行质量
[专项评价：止损位合理性、是否有过早/过晚止损、止损后股价走势]

## 针对 {provider_name} 的改进建议
[3-5条具体可操作建议，例如：调整Confidence阈值、某类信号的处理方式、特定Regime下的策略变化、提示词优化方向]

## 下周重点关注
[基于本周表现，下周需要特别留意的行为模式或风险]
"""


def run_weekend_feedback(from_date: str,
                          to_date: str,
                          read_log_fn: Callable,
                          get_quote_fn: Callable,
                          call_ai_fn: Callable,
                          append_log_fn: Callable) -> dict:
    """
    Orchestrate the full weekend feedback loop.

    Args:
        from_date / to_date : "YYYY-MM-DD" boundaries (Mon-Fri of past week)
        read_log_fn   : read_log_range(prefix, from, to, provider=None) → list
        get_quote_fn  : get_stock_quote(symbol) → dict | None
        call_ai_fn    : call_ai(prompt, provider) → str
        append_log_fn : append_log(prefix, entry, date_str)

    Returns:
        {"from_date", "to_date", "reports": {provider: {...}}, "generated_at"}
    """
    logger.info("Weekend feedback loop: %s → %s", from_date, to_date)

    all_trades = read_log_fn("trades", from_date, to_date)
    logger.info("Loaded %d trade records for the week", len(all_trades))

    decisions_by_provider = analyze_trade_decisions(all_trades, get_quote_fn)

    reports = {}
    for provider, decisions in decisions_by_provider.items():
        if not decisions:
            reports[provider] = {
                "provider":     provider,
                "from_date":    from_date,
                "to_date":      to_date,
                "total_trades": 0,
                "win_count":    0,
                "loss_count":   0,
                "total_pnl":    0.0,
                "win_rate":     0.0,
                "ai_report":    "本周该模型无交易记录。",
                "decisions":    [],
            }
            continue

        prompt    = build_feedback_prompt(provider, decisions, from_date, to_date)
        # Use Claude as the meta-analyst for all providers for best narrative quality
        ai_report = call_ai_fn(prompt, "claude")

        good      = [d for d in decisions if d.get("quality") == "good"]
        bad       = [d for d in decisions if d.get("quality") == "bad"]
        total_pnl = sum((d.get("realized_pnl") or 0) for d in decisions)

        entry = {
            "id":           f"feedback_{provider}_{from_date}",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "from_date":    from_date,
            "to_date":      to_date,
            "provider":     provider,
            "total_trades": len(decisions),
            "win_count":    len(good),
            "loss_count":   len(bad),
            "total_pnl":    round(total_pnl, 2),
            "win_rate":     round(len(good) / len(decisions) * 100, 1),
            "ai_report":    ai_report,
            "decisions":    decisions,
        }
        append_log_fn("feedback", entry, to_date)
        reports[provider] = entry
        logger.info("Feedback done for %s: %d trades, win_rate=%.0f%%",
                    provider, len(decisions), entry["win_rate"])

    return {
        "from_date":    from_date,
        "to_date":      to_date,
        "reports":      reports,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────
# Feature 2 — Watchlist Suggestions
# ─────────────────────────────────────────────────────────────────────

# Major sector ETFs used to gauge market breadth and rotation
_SECTOR_ETFS = [
    ("SPY",  "标普500"),
    ("QQQ",  "纳斯达克100"),
    ("XLK",  "科技板块"),
    ("XLF",  "金融板块"),
    ("XLE",  "能源板块"),
    ("XLV",  "医疗健康"),
    ("XLI",  "工业板块"),
    ("XLC",  "通信服务"),
    ("XLY",  "非必需消费"),
    ("XLP",  "必需消费品"),
    ("XLRE", "房地产"),
    ("XLU",  "公用事业"),
    ("XLB",  "材料板块"),
    ("GLD",  "黄金"),
    ("IWM",  "小盘股"),
    ("ARKK", "创新/颠覆性"),
    ("SMH",  "半导体"),
    ("IBB",  "生物科技"),
]


def fetch_sector_performance(get_quote_fn: Callable) -> list:
    """
    Fetch current price / daily % change for each sector ETF.
    Returns list sorted by % change descending (hot sectors first).
    """
    results = []
    for etf, name in _SECTOR_ETFS:
        try:
            q = get_quote_fn(etf)
            if q:
                results.append({
                    "etf":        etf,
                    "name":       name,
                    "price":      q.get("c", 0),
                    "change_pct": round(q.get("dp", 0) or 0, 2),
                    "day_change": round(q.get("d", 0) or 0, 2),
                })
        except Exception as e:
            logger.warning("Sector ETF quote failed for %s: %s", etf, e)

    return sorted(results, key=lambda x: x.get("change_pct", 0), reverse=True)


def fetch_general_market_news(finnhub_key: str, limit: int = 25) -> list:
    """
    Pull general market news from Finnhub's /news endpoint.
    Category "general" covers broad US market / macro stories.
    """
    if not finnhub_key:
        logger.warning("FINNHUB_KEY not set — skipping market news fetch")
        return []
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={finnhub_key}"
        r   = requests.get(url, timeout=12)
        if r.status_code != 200:
            logger.warning("Finnhub general news HTTP %d", r.status_code)
            return []
        items = r.json()[:limit]
        return [
            {
                "headline": a.get("headline", ""),
                "summary":  (a.get("summary") or "")[:200],
                "source":   a.get("source", ""),
                "dt":       a.get("datetime", 0),
            }
            for a in items
            if a.get("headline")
        ]
    except Exception as e:
        logger.error("General news fetch error: %s", e)
        return []


def build_watchlist_suggestion_prompt(sector_perf: list,
                                       market_news: list,
                                       current_watchlist: list,
                                       from_date: str,
                                       to_date: str) -> str:
    """
    Build the AI prompt that generates structured stock recommendations
    for the coming week based on sector rotation and news catalysts.
    """
    # Sector table
    sector_txt = "\n".join(
        f"  {r['etf']:5s} ({r['name']:10s}):  {r['change_pct']:+.2f}%  @ ${r['price']:.2f}"
        for r in sector_perf
    ) or "  暂无数据"

    # Top news headlines
    news_txt = "\n".join(
        f"  [{i+1:2d}] {n['headline'][:130]}"
        for i, n in enumerate(market_news[:18])
    ) or "  暂无新闻"

    # Current watchlist symbols
    wl_symbols = [s.get("symbol", "") for s in current_watchlist if s.get("symbol")]
    wl_txt = ", ".join(wl_symbols) or "（空）"

    return f"""你是一位资深美股短线量化分析师，专注于动量交易（持仓1-5天）。

今天是 {to_date}（周末），请根据本周 ({from_date} - {to_date}) 市场表现，
为下周交易制定股票观察名单策略。

═══════════════════════════════════
本周板块ETF表现（按涨跌幅排名）
═══════════════════════════════════
{sector_txt}

═══════════════════════════════════
本周重要市场新闻
═══════════════════════════════════
{news_txt}

═══════════════════════════════════
当前观察名单
═══════════════════════════════════
{wl_txt}

═══════════════════════════════════
任务：输出下周观察名单调整建议
═══════════════════════════════════

请严格按以下格式输出（程序将自动解析）：

## 市场整体判断
[2-3句：市场情绪、趋势强度（Trend/Chop/Transition）、主要风险]

## 下周重点关注板块（前3名）
1. **[板块名/ETF代码]** — [理由，结合新闻和涨跌幅，50字以内]
2. **[板块名/ETF代码]** — [理由]
3. **[板块名/ETF代码]** — [理由]

## 建议加入观察名单的股票
每只股票一行，严格遵守以下管道符格式（程序解析用）：
ADD|股票代码|板块|理由（限80字）|交易类型

交易类型选项：短线动量 / 趋势追踪 / 事件驱动 / 突破买入

示例（请模仿此格式）：
ADD|NVDA|科技/AI|AI算力需求强劲，突破前高，成交量放大|趋势追踪

请列出5-8只股票：

## 建议从观察名单移除的股票
每只一行，格式：
REMOVE|股票代码|移除理由（限60字）

若无建议移除，写：REMOVE|无|本周暂无需移除的标的

## 下周交易策略重点
[3-5条具体操作建议：优先板块、入场信号、规避风险、仓位策略]

注意事项：
- 只推荐NYSE/NASDAQ上市的美股，不推荐OTC/粉单市场股票
- 日均成交量须 > 50万股（确保流动性）
- 优先有催化剂（财报预期、产品发布、政策利好）的标的
- 结合板块Regime给出相应仓位建议（趋势市满仓，震荡市半仓）"""


def parse_watchlist_suggestions(ai_text: str) -> dict:
    """
    Extract ADD and REMOVE lines from AI output.

    Expected pipe format:
      ADD|SYMBOL|Sector|Reason|TradeType
      REMOVE|SYMBOL|Reason
    """
    adds    = []
    removes = []

    for line in ai_text.splitlines():
        line = line.strip()

        if line.upper().startswith("ADD|"):
            parts = [p.strip() for p in line.split("|")]
            sym = parts[1].upper() if len(parts) > 1 else ""
            if sym and sym != "无":
                adds.append({
                    "symbol":     sym,
                    "sector":     parts[2] if len(parts) > 2 else "",
                    "reason":     parts[3] if len(parts) > 3 else "",
                    "trade_type": parts[4] if len(parts) > 4 else "短线动量",
                })

        elif line.upper().startswith("REMOVE|"):
            parts = [p.strip() for p in line.split("|")]
            sym = parts[1].upper() if len(parts) > 1 else ""
            if sym and sym != "无":
                removes.append({
                    "symbol": sym,
                    "reason": parts[2] if len(parts) > 2 else "",
                })

    return {"add": adds, "remove": removes}


def run_watchlist_suggestions(from_date: str,
                               to_date: str,
                               finnhub_key: str,
                               get_quote_fn: Callable,
                               call_ai_fn: Callable,
                               current_watchlist: list,
                               append_log_fn: Callable) -> dict:
    """
    Orchestrate the full watchlist suggestion pipeline.

    Args:
        from_date / to_date : past week boundaries
        finnhub_key         : API key for general news fetch
        get_quote_fn        : get_stock_quote(symbol) → dict | None
        call_ai_fn          : call_ai(prompt, provider) → str
        current_watchlist   : list of {"symbol":…, "type":…}
        append_log_fn       : append_log(prefix, entry, date_str)

    Returns:
        Full suggestion entry dict (also persisted to logs).
    """
    logger.info("Watchlist suggestions: %s → %s", from_date, to_date)

    sector_perf = fetch_sector_performance(get_quote_fn)
    market_news = fetch_general_market_news(finnhub_key)

    logger.info("Sector data: %d ETFs | Market news: %d articles",
                len(sector_perf), len(market_news))

    prompt  = build_watchlist_suggestion_prompt(
        sector_perf, market_news, current_watchlist, from_date, to_date)
    ai_text = call_ai_fn(prompt, "claude")   # Claude for best analysis

    parsed  = parse_watchlist_suggestions(ai_text)

    entry = {
        "id":                 f"suggestions_{to_date}",
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "from_date":          from_date,
        "to_date":            to_date,
        "sector_performance": sector_perf,
        "market_news_count":  len(market_news),
        "ai_analysis":        ai_text,
        "suggestions_add":    parsed["add"],
        "suggestions_remove": parsed["remove"],
        "current_watchlist":  [s.get("symbol") for s in current_watchlist],
    }
    append_log_fn("suggestions", entry, to_date)

    logger.info("Suggestions: %d add, %d remove",
                len(parsed["add"]), len(parsed["remove"]))
    return entry
