"""
config.py — API keys and model configuration for Quant AI Stock Trader.

Keys are read from environment variables (or a .env file if python-dotenv
is installed).  Nothing is hard-coded here — fill in the real values in
your .env file (copy from .env.example).
"""

import os
import logging

logger = logging.getLogger(__name__)

# ── Load .env file if present ─────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("Loaded environment from .env file")
except ImportError:
    pass  # python-dotenv is optional; set env vars directly in production


# ── Helper ────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    """Return the environment variable or an empty string."""
    return os.environ.get(key, default)


# ── Data / market feeds ───────────────────────────────────────────
FINNHUB_KEY  = _env("FINNHUB_KEY")   # Stock quotes & news  — finnhub.io
NEWSAPI_KEY  = _env("NEWSAPI_KEY")   # Crypto news          — newsapi.org

# ── AI providers ──────────────────────────────────────────────────
CLAUDE_KEY   = _env("CLAUDE_KEY")    # Anthropic Claude     — console.anthropic.com
GROK_KEY     = _env("GROK_KEY")      # xAI Grok             — console.x.ai
DEEPSEEK_KEY = _env("DEEPSEEK_KEY")  # DeepSeek             — platform.deepseek.com

# ── Optional extras ───────────────────────────────────────────────
SERPAPI_KEY  = _env("SERPAPI_KEY")   # SerpAPI (not currently used)
PORT         = int(_env("PORT", "5000"))

# ── AI model identifiers ──────────────────────────────────────────
# Change these if you want to pin to a specific model version.
MODELS = {
    "claude": {
        "model":    "claude-sonnet-4-20250514",
        "api_url":  "https://api.anthropic.com/v1/messages",
        "env_key":  "CLAUDE_KEY",
        "display":  "Claude",
    },
    "grok": {
        "model":    "grok-3",          # STRATEGY-4: upgraded from grok-3-mini; full model
        "api_url":  "https://api.x.ai/v1/chat/completions",  # produces better multi-stock analysis
        "env_key":  "GROK_KEY",
        "display":  "Grok",
    },
    "deepseek": {
        "model":    "deepseek-chat",
        "api_url":  "https://api.deepseek.com/v1/chat/completions",
        "env_key":  "DEEPSEEK_KEY",
        "display":  "DeepSeek",
    },
}

MAX_TOKENS = 4000   # max tokens for every AI call (STRATEGY-3: raised from 2000 to avoid
                    # response truncation mid-DECISION when analysing 6-stock watchlist)

# TOKEN-1: session-specific output token caps.
# premarket is analysis-only (no DECISION blocks) so 1500 tokens is enough.
# closing needs SELLs only — fewer decisions than a full opening session.
# opening/mid need full SCORE + DECISION for all watchlist stocks → keep at 4000.
SESSION_MAX_TOKENS: dict = {
    "premarket": 1500,
    "opening":   4000,
    "mid":       4000,
    "closing":   2000,
}

# ── Data feed URLs ────────────────────────────────────────────────
FINNHUB_QUOTE_URL   = "https://finnhub.io/api/v1/quote"
FINNHUB_NEWS_URL    = "https://finnhub.io/api/v1/company-news"
COINGECKO_COIN_URL  = "https://api.coingecko.com/api/v3/coins/{coin_id}"
NEWSAPI_URL         = "https://newsapi.org/v2/everything"

# ── Cache TTLs (seconds) ──────────────────────────────────────────
PRICE_CACHE_TTL     = 300    # 5 min for stock prices
CRYPTO_CACHE_TTL    = 60     # 1 min for crypto prices
NEWS_CACHE_TTL      = 900    # 15 min for news

# ── Runtime check: warn about missing keys at startup ─────────────
def check_config() -> list[str]:
    """
    Return a list of warning strings for missing keys.
    Called once at startup so missing keys surface immediately in logs.
    """
    warnings = []
    if not FINNHUB_KEY:
        warnings.append("FINNHUB_KEY not set — stock quotes and news will fail")
    if not any([CLAUDE_KEY, GROK_KEY, DEEPSEEK_KEY]):
        warnings.append("No AI key set — set at least one of CLAUDE_KEY / GROK_KEY / DEEPSEEK_KEY")
    if not NEWSAPI_KEY:
        warnings.append("NEWSAPI_KEY not set — crypto news will be unavailable")
    return warnings
