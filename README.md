# Quant AI Stock Trader — Python v6.0

Python rewrite of the Google Apps Script (GAS) trading simulator.  
Identical UX to the original GAS app. Runs locally, Git-friendly, pandas-ready logs.

## Architecture

```
quant_trader/
├── app.py              ← Flask backend (replaces Code.gs)
├── strategy_v6.py      ← Strategy engine (replaces Strategy_v6.gs)
├── templates/
│   └── index.html      ← Frontend SPA (identical UX to original)
├── data/               ← Persistent state (replaces GAS PropertiesService)
│   ├── watchlist.json
│   └── trade_states/
│       ├── state_grok.json
│       ├── state_claude.json
│       └── state_deepseek.json
├── logs/               ← JSONL trade logs (replaces Google Drive)
│   ├── trades_YYYY-MM.jsonl
│   └── sessions_YYYY-MM.jsonl
├── requirements.txt
└── .env.example
```

## Quick Start

### 1. Install dependencies
```bash
pip install flask requests python-dotenv
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env with your real keys
```

Your `.env`:
```
FINNHUB_KEY=your_finnhub_key
NEWSAPI_KEY=your_newsapi_key
CLAUDE_KEY=sk-ant-api03-...
GROK_KEY=xai-...
DEEPSEEK_KEY=sk-...
```

### 3. Run
```bash
python app.py
# Open http://localhost:5000
```

---

## Strategy v6.0 — Rules Summary (A01–A10)

### Position Sizing (A04 — ATR-based)
```
shares = floor( NAV × 1% / (ATR × 1.5) )
cap    = floor( NAV × 4% / price )        # single-trade max
shares = min(shares, cap)
# Transition regime: shares × 0.5
```

### Stop Levels (A04 — Two-phase trailing)
| Phase | Trigger | Stop |
|-------|---------|------|
| Initial | Entry | Entry − 1.5×ATR |
| Phase 1 | Profit ≥ 1R | High − 1.5×ATR |
| Phase 2 | Profit ≥ 2R | High − 1.0×ATR (tighter) |
| Hard stop | Loss ≥ 2% | Full exit |
| Hard profit | Gain ≥ 5% | Full exit |

### Market Regime (A10 — 3-day debounce state machine)
| State | Condition | Action |
|-------|-----------|--------|
| **Trend** | ADX > 25 AND price > 200MA | Normal trading |
| **Transition** | ADX 20–25 | Half position, min confidence ≥ 7 |
| **Chop** | ADX < 20 | No new entries, force-exit existing |

### Hard Position Rules (A04)
- Cash floor: always ≥ 20% NAV
- Max holdings: 5 simultaneous positions
- No averaging down (price < avgCost → BUY blocked)
- Max 2 trades per symbol per day

### Expectancy Formula (A01/A08)
```
E = P(win) × Avg(win) − P(loss) × Avg(loss)
```
Targets: E > 0 · Profit Factor ≥ 1.5 · Max Drawdown < 20%

### Sessions (A05 — ET times)
| Session | ET Time | SGT (EDT) |
|---------|---------|-----------|
| 盘前分析 | 09:15 | 21:15 |
| 黄金入场 | 10:00 | 22:00 |
| 中盘复盘 | 12:00 | 00:00+1 |
| 收尾 | 15:30 | 03:30+1 |

### Feedback Learning (A07)
- Triggers after ≥ 20 closed trades
- Alert when: E drops ≥ 20% OR win-rate drops ≥ 15% from baseline

---

## API Reference

All endpoints: `POST /api` with JSON body `{"action": "...", ...}`

| Action | Params | Description |
|--------|--------|-------------|
| `getStocks` | — | Load watchlist |
| `saveStocks` | `{stocks}` | Save watchlist |
| `getQuote` | `{symbol, type}` | Finnhub / CoinGecko price |
| `getNews` | `{items, limit}` | Finnhub / NewsAPI articles |
| `analyzeStock` | `{prompt, provider}` | Free-form AI analysis |
| `getTradeState` | `{provider}` | Load state for AI provider |
| `resetTradeState` | `{provider}` | Reset to $10,000 |
| `getQuantMetrics` | `{provider}` | E-value, WR, PF, drawdown |
| `runTradeSession` | `{session, provider}` | Execute full AI session |
| `getDriveSessions` | `{fromDate, toDate, aiProvider}` | Session log history |
| `getDriveTrades` | `{fromDate, toDate, aiProvider}` | Trade log history |
| `listDriveLogFiles` | — | List JSONL log files |

---

## Log Format

### trades_YYYY-MM.jsonl
```json
{
  "id": "trade_1744123456789_AAPL",
  "timestamp": "2026-04-11T10:05:00+00:00",
  "date": "2026-04-11",
  "time_et": "10:05",
  "session": "opening",
  "ai_provider": "grok",
  "action": "BUY",
  "symbol": "AAPL",
  "shares": 2,
  "price": 185.0,
  "cost": 370.0,
  "realized_pnl": null,
  "signal_type": "breakout",
  "regime": "Trend",
  "r_value": null,
  "stop_price": 179.5,
  "entry_atr": 3.67,
  "confidence": 7,
  "is_plan_trade": true,
  "is_fomo": false,
  "violation": "none",
  "exit_tag": null,
  "reason": "breakout 20-day high + volume C:7 ATR=$3.67"
}
```

### Analyse with pandas
```python
import pandas as pd
df = pd.read_json('logs/trades_2026-04.jsonl', lines=True)
df[df.action=='SELL'].groupby('signal_type')['realized_pnl'].describe()
```

---

## Differences from GAS Version

| Feature | GAS | Python |
|---------|-----|--------|
| Backend | Google Apps Script | Flask (local / any server) |
| State storage | PropertiesService (opaque) | `data/*.json` (readable) |
| Log storage | Google Drive JSONL | `logs/*.jsonl` (local) |
| Scheduling | GAS time triggers | cron / APScheduler |
| AI providers | Grok · Claude · DeepSeek | Same 3 providers |
| Git workflow | Requires clasp | Direct `git commit` |
| Timezone | ET via GAS built-in | `zoneinfo` stdlib |

## Scheduled Sessions (Optional)

To run sessions automatically, add to crontab (ET times):
```cron
# Run on US market trading days — times are ET
15 9  * * 1-5  cd /path/to/quant_trader && python -c "import app; app.run_trade_session('premarket','grok')"
0  10 * * 1-5  cd /path/to/quant_trader && python -c "import app; app.run_trade_session('opening','grok')"
0  12 * * 1-5  cd /path/to/quant_trader && python -c "import app; app.run_trade_session('mid','grok')"
30 15 * * 1-5  cd /path/to/quant_trader && python -c "import app; app.run_trade_session('closing','grok')"
```
