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

        prompt = build_feedback_prompt(provider, decisions, from_date, to_date)
        try:
            ai_report = call_ai_fn(prompt, "claude", 2000)
        except Exception as e:
            logger.error("AI feedback call failed for %s: %s", provider, e)
            ai_report = f"[ERROR] 分析生成失败: {e}"

        good      = [d for d in decisions if d.get("quality") == "good"]
        bad       = [d for d in decisions if d.get("quality") == "bad"]
        total_pnl = sum((d.get("realized_pnl") or 0) for d in decisions)

        entry = {
            "id":           f"feedback_{provider}_{from_date}",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "date":         to_date,
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
# Feature 2 — Watchlist Suggestions (Two-Layer Selection Strategy)
#
# Strategy: us_stock_selection_strategy.md
#   Layer 1 — Sector ETF screening (weighted scoring: relative strength
#             35%, capital inflow 30%, catalysts 25%, trend confirm 10%)
#   Layer 2 — Individual stock screening (fundamental gates + technical
#             breakout signals, output 3-5 candidates)
# ─────────────────────────────────────────────────────────────────────

# Sector ETFs from the strategy document (used for Layer 1 scoring)
_SECTOR_ETFS = [
    ("XLK",  "科技"),
    ("SOXX", "半导体/AI"),
    ("CLOU", "AI/云计算"),
    ("XBI",  "生物医药"),
    ("IHI",  "医疗器械"),
    ("XLE",  "能源"),
    ("XOP",  "油气"),
    ("XLF",  "金融"),
    ("IAI",  "券商"),
    ("XLY",  "消费"),
    ("XLI",  "工业"),
    ("XLU",  "防御/公用事业"),
]

# SPY is the benchmark — fetched separately for relative-strength comparison
_BENCHMARK_ETF = "SPY"


def fetch_sector_performance(get_quote_fn: Callable) -> list:
    """
    Fetch price data for each strategy-defined sector ETF + SPY benchmark.
    Returns list sorted by daily % change descending.
    Each entry includes the SPY daily change so the AI can compute
    relative strength (sector vs benchmark) directly.
    """
    spy_q = None
    try:
        spy_q = get_quote_fn(_BENCHMARK_ETF)
    except Exception as e:
        logger.warning("SPY quote failed: %s", e)
    spy_dp = round((spy_q or {}).get("dp", 0) or 0, 2)

    results = []
    for etf, name in _SECTOR_ETFS:
        try:
            q = get_quote_fn(etf)
            if q:
                dp = round(q.get("dp", 0) or 0, 2)
                results.append({
                    "etf":           etf,
                    "name":          name,
                    "price":         round(q.get("c", 0), 2),
                    "change_pct":    dp,
                    "day_change":    round(q.get("d", 0) or 0, 2),
                    "vs_spy":        round(dp - spy_dp, 2),
                    "open":          round(q.get("o", 0) or 0, 2),
                    "high":          round(q.get("h", 0) or 0, 2),
                    "low":           round(q.get("l", 0) or 0, 2),
                    "prev_close":    round(q.get("pc", 0) or 0, 2),
                })
        except Exception as e:
            logger.warning("Sector ETF quote failed for %s: %s", etf, e)

    results = sorted(results, key=lambda x: x.get("change_pct", 0), reverse=True)
    # Inject SPY benchmark data at position 0 for reference
    if spy_q:
        results.insert(0, {
            "etf": "SPY", "name": "基准(标普500)",
            "price": round(spy_q.get("c", 0), 2),
            "change_pct": spy_dp, "day_change": round(spy_q.get("d", 0) or 0, 2),
            "vs_spy": 0.0,
            "open": round(spy_q.get("o", 0) or 0, 2),
            "high": round(spy_q.get("h", 0) or 0, 2),
            "low": round(spy_q.get("l", 0) or 0, 2),
            "prev_close": round(spy_q.get("pc", 0) or 0, 2),
        })
    return results


def fetch_general_market_news(finnhub_key: str, limit: int = 25) -> list:
    """Pull general market news from Finnhub for catalyst identification."""
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
    Build the two-layer stock selection prompt following
    us_stock_selection_strategy.md methodology.
    """
    # Sector table with relative-strength data
    sector_lines = []
    for r in sector_perf:
        vs = r.get("vs_spy", 0)
        vs_tag = f"跑赢SPY {vs:+.2f}%" if vs > 0 else f"跑输SPY {vs:+.2f}%"
        sector_lines.append(
            f"  {r['etf']:5s} ({r['name']:8s}): "
            f"{r['change_pct']:+.2f}%  ${r['price']:.2f}  "
            f"H/L ${r.get('high',0):.2f}/${r.get('low',0):.2f}  "
            f"[{vs_tag}]"
        )
    sector_txt = "\n".join(sector_lines) or "  暂无数据"

    # News headlines for catalyst identification
    news_txt = "\n".join(
        f"  [{i+1:2d}] {n['headline'][:130]}"
        for i, n in enumerate(market_news[:18])
    ) or "  暂无新闻"

    # Current watchlist
    wl_symbols = [s.get("symbol", "") for s in current_watchlist if s.get("symbol")]
    wl_txt = ", ".join(wl_symbols) or "（空）"

    return f"""你是一位资深美股超短线量化分析师（持仓2-5天），严格执行以下两层选股策略。

今天是 {to_date}（周末），请根据本周 ({from_date} → {to_date}) 数据执行选股。

═══════════════════════════════════════════════════
数据输入：板块ETF实时行情（含相对SPY强度）
═══════════════════════════════════════════════════
{sector_txt}

═══════════════════════════════════════════════════
数据输入：本周重要市场新闻（用于催化剂判断）
═══════════════════════════════════════════════════
{news_txt}

═══════════════════════════════════════════════════
当前观察名单
═══════════════════════════════════════════════════
{wl_txt}

═══════════════════════════════════════════════════
第一层：板块筛选（输出2-3个强势板块）
═══════════════════════════════════════════════════

请对上方12个板块ETF逐一评分（0-100分），使用以下加权标准：

| 指标                | 权重 | 评分标准                                                    |
|---------------------|------|-------------------------------------------------------------|
| 板块ETF相对强度     | 35%  | 过去5日涨幅高于SPY（跑赢）→ 高分；跑输 → 低分              |
| 资金净流入          | 30%  | 近3日量价齐升（成交量放大+价格上涨）→ 高分                  |
| 近期催化剂          | 25%  | 本周/下周有行业事件（财报潮、政策、产品发布）→ 高分         |
| 趋势方向确认        | 10%  | 日线收盘在20日均线上方 → 得分；下方 → 0分                   |

**淘汰规则：** ETF收盘价在20日均线下方 → 直接淘汰，不参与评分

对每个板块，严格输出一行（程序解析用）：
SECTOR|ETF代码|板块名|综合得分|入选原因（1句话）

示例：
SECTOR|XLK|科技|82|AI需求推动科技股持续跑赢大盘，量价齐升趋势明确
SECTOR|SOXX|半导体/AI|78|芯片出口政策利好催化，板块突破前高

请列出所有12个板块的评分（包括被淘汰的，被淘汰的得分标0）：

═══════════════════════════════════════════════════
第二层：个股筛选（从入选板块中选3-5只候选股）
═══════════════════════════════════════════════════

从第一层得分最高的2-3个板块中，每个板块选1-2只个股。

### Step A · 基本面门槛（一票否决，不达标直接排除）
| 条件           | 标准                                           |
|----------------|------------------------------------------------|
| 市值           | > $5B                                          |
| 日均成交量     | > 500万股                                      |
| 近期营收增速   | 最新季度 YoY > 10%                             |
| 机构持仓趋势   | 近一季度机构持仓增加或持平                     |

### Step B · 技术面突破信号（满足以下4项中至少3项）
| 信号           | 判断标准                                       |
|----------------|------------------------------------------------|
| 均线位置       | 股价站上20日均线，且20日线斜率向上              |
| 放量突破       | 突破近期平台，当日量 ≥ 20日均量 × 1.5          |
| 相对强度       | 近5日个股涨幅 > 板块ETF涨幅（板块内领涨）      |
| MACD形态       | 日线MACD金叉，或DIF在零轴上方且柱状图扩张      |

**加分项（不计入3项达标，但同等分数优先选择）：**
旗形整理后突破 / 杯柄形态 / 缩量回调至均线后放量启动

每只候选股输出一行（程序解析用）：
ADD|股票代码|所属板块|通过技术信号数(N/4)|理由（限80字，含关键支撑位和压力位）|交易类型

交易类型选项：短线动量 / 趋势追踪 / 事件驱动 / 突破买入

示例：
ADD|NVDA|科技/AI|4/4|AI算力龙头突破前高$950放量，20日线斜率陡峭，支撑$920压力$980|趋势追踪

请列出3-5只候选股：

═══════════════════════════════════════════════════
建议从观察名单移除的股票
═══════════════════════════════════════════════════
检查当前观察名单（{wl_txt}），如有股票满足以下任一条件则建议移除：
- 跌破20日均线且趋势转弱
- 所属板块在第一层被淘汰（20MA下方）
- 基本面恶化（营收不及预期、机构大幅减持）

每只一行：
REMOVE|股票代码|移除理由（限60字）

若无建议移除，写：REMOVE|无|本周暂无需移除的标的

═══════════════════════════════════════════════════
下周交易策略重点
═══════════════════════════════════════════════════
请输出3-5条具体操作建议，包括：
- 优先操作哪些板块及原因
- 入场信号关注什么（突破、回调、量能）
- 本周需规避的风险（宏观事件、财报雷区）
- 仓位策略建议（趋势市满仓 vs 震荡市半仓）

注意事项：
- 只推荐NYSE/NASDAQ上市美股，不推荐OTC/粉单
- 日均成交量须 > 500万股
- 市值须 > $5B
- 优先有催化剂的标的"""


def parse_watchlist_suggestions(ai_text: str) -> dict:
    """
    Extract SECTOR, ADD, and REMOVE lines from the two-layer AI output.

    Formats:
      SECTOR|ETF|Name|Score|Reason
      ADD|SYMBOL|Sector|TechSignals|Reason|TradeType
      REMOVE|SYMBOL|Reason
    """
    sectors = []
    adds    = []
    removes = []

    for line in ai_text.splitlines():
        line = line.strip()

        if line.upper().startswith("SECTOR|"):
            parts = [p.strip() for p in line.split("|")]
            etf = parts[1].upper() if len(parts) > 1 else ""
            if etf and etf != "无":
                score = 0
                try:
                    score = int(parts[3]) if len(parts) > 3 else 0
                except ValueError:
                    pass
                sectors.append({
                    "etf":    etf,
                    "name":   parts[2] if len(parts) > 2 else "",
                    "score":  score,
                    "reason": parts[4] if len(parts) > 4 else "",
                })

        elif line.upper().startswith("ADD|"):
            parts = [p.strip() for p in line.split("|")]
            sym = parts[1].upper() if len(parts) > 1 else ""
            if sym and sym != "无":
                adds.append({
                    "symbol":       sym,
                    "sector":       parts[2] if len(parts) > 2 else "",
                    "tech_signals": parts[3] if len(parts) > 3 else "",
                    "reason":       parts[4] if len(parts) > 4 else "",
                    "trade_type":   parts[5] if len(parts) > 5 else "短线动量",
                })

        elif line.upper().startswith("REMOVE|"):
            parts = [p.strip() for p in line.split("|")]
            sym = parts[1].upper() if len(parts) > 1 else ""
            if sym and sym != "无":
                removes.append({
                    "symbol": sym,
                    "reason": parts[2] if len(parts) > 2 else "",
                })

    return {"sectors": sectors, "add": adds, "remove": removes}


def run_watchlist_suggestions(from_date: str,
                               to_date: str,
                               finnhub_key: str,
                               get_quote_fn: Callable,
                               call_ai_fn: Callable,
                               current_watchlist: list,
                               append_log_fn: Callable) -> dict:
    """
    Execute the two-layer stock selection pipeline.

    Layer 1: Score all 12 sector ETFs via weighted criteria
    Layer 2: From top 2-3 sectors, pick 3-5 individual stocks
    """
    logger.info("Watchlist suggestions: %s → %s", from_date, to_date)

    sector_perf = fetch_sector_performance(get_quote_fn)
    market_news = fetch_general_market_news(finnhub_key)

    logger.info("Sector data: %d ETFs | Market news: %d articles",
                len(sector_perf), len(market_news))

    prompt  = build_watchlist_suggestion_prompt(
        sector_perf, market_news, current_watchlist, from_date, to_date)

    try:
        ai_text = call_ai_fn(prompt, "claude", 2000)
    except Exception as e:
        logger.error("Watchlist suggestion AI call failed: %s", e)
        ai_text = f"[ERROR] AI分析生成失败: {e}"

    parsed  = parse_watchlist_suggestions(ai_text)

    entry = {
        "id":                 f"suggestions_{to_date}",
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "date":               to_date,
        "from_date":          from_date,
        "to_date":            to_date,
        "sector_performance": sector_perf,
        "sector_scores":      parsed["sectors"],
        "market_news_count":  len(market_news),
        "ai_analysis":        ai_text,
        "suggestions_add":    parsed["add"],
        "suggestions_remove": parsed["remove"],
        "current_watchlist":  [s.get("symbol") for s in current_watchlist],
    }
    append_log_fn("suggestions", entry, to_date)

    logger.info("Suggestions: %d sectors scored, %d add, %d remove",
                len(parsed["sectors"]), len(parsed["add"]), len(parsed["remove"]))
    return entry
