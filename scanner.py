"""
scanner.py — Core data engine for SIGNAL

Data sources:
  yfinance   : price, OHLCV, fundamentals, options chain
  Reddit     : public JSON endpoints — NO API KEY NEEDED
  anthropic  : AI narrative scoring & analysis text (optional)
  ta         : Technical analysis indicators (RSI, MACD, Bollinger, etc.)
"""

import os, re, time, math, requests
from datetime import datetime, timedelta
from typing import Optional, List
from collections import Counter

import yfinance as yf
import pandas as pd
import numpy as np

try:
    import ta
    TA_AVAILABLE = True
except ImportError:
    TA_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = bool(os.getenv("ANTHROPIC_API_KEY"))
except ImportError:
    ANTHROPIC_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# REDDIT SCANNING  (no API key — uses public JSON endpoints)
# ─────────────────────────────────────────────────────────────────────────────

SUBREDDITS = ["wallstreetbets", "stocks", "investing", "pennystocks", "StockMarket", "options"]

BLACKLIST = {
    "A","I","THE","FOR","ARE","IS","BE","OR","AND","IN","ON","AT","TO","OF",
    "IT","AN","IF","SO","UP","DO","GO","NOW","ALL","ITS","BY","NEW","OUT",
    "CEO","IPO","AI","ETF","PE","EPS","YTD","DD","WSB","IMO","ATH","PM","AM",
    "USD","US","USA","UK","EU","GDP","CPI","SEC","FED","ATM","OTM","ITM",
    "DCA","HODL","YOLO","FOMO","OP","RH","TD","WS","PR","IR","ER","QE",
    "RE","IT","AS","HE","WE","MY","NO","ME","HI","OK","SO","YO","GO",
}

TICKER_RE   = re.compile(r'\b([A-Z]{2,5})\b')
REDDIT_HDRS = {
    "User-Agent": "Mozilla/5.0 (compatible; SIGNAL-Scanner/1.0; +https://github.com/signal)",
    "Accept": "application/json",
}

def _fetch_sub(subreddit: str, limit: int = 100) -> list:
    """
    Fetch recent posts from a subreddit using Reddit's public .json endpoint.
    No authentication required — Reddit allows read-only public access.
    Returns list of post dicts.
    """
    posts = []
    after = None
    fetched = 0
    per_page = min(limit, 100)   # Reddit caps at 100 per request

    while fetched < limit:
        url    = f"https://www.reddit.com/r/{subreddit}/new.json"
        params = {"limit": per_page, "raw_json": 1}
        if after:
            params["after"] = after

        try:
            resp = requests.get(url, headers=REDDIT_HDRS, params=params, timeout=10)
            if resp.status_code == 429:
                time.sleep(2)   # rate limit — back off
                continue
            if resp.status_code != 200:
                break
            data     = resp.json()
            children = data.get("data", {}).get("children", [])
            if not children:
                break
            for child in children:
                posts.append(child.get("data", {}))
            after    = data.get("data", {}).get("after")
            fetched += len(children)
            if not after:
                break
            time.sleep(0.5)   # polite delay between pages
        except Exception:
            break

    return posts


def get_reddit_trending(limit_per_sub: int = 100, top_n: int = 20) -> list:
    """
    Scan recent posts across investing subreddits using public Reddit JSON.
    No API key needed.
    Returns list of {ticker, mentions, avg_upvotes, sample_posts}.
    """
    counts = Counter()
    titles = {}   # ticker -> [post title, ...]
    scores = {}   # ticker -> [upvote score, ...]

    for sub in SUBREDDITS:
        try:
            posts = _fetch_sub(sub, limit=limit_per_sub)
        except Exception as e:
            print(f"[reddit] {sub} failed: {e}")
            continue

        for post in posts:
            title   = post.get("title", "")
            selftext = post.get("selftext", "")
            score   = post.get("score", 0)
            text    = (title + " " + selftext).upper()
            found   = set(TICKER_RE.findall(text)) - BLACKLIST
            for ticker in found:
                counts[ticker] += 1
                titles.setdefault(ticker, []).append(title[:80])
                scores.setdefault(ticker, []).append(score)

    # Validate: only keep tickers resolvable by yfinance
    results = []
    for ticker, count in counts.most_common(top_n * 4):
        if count < 2:
            continue
        try:
            fi = yf.Ticker(ticker).fast_info
            if not getattr(fi, "last_price", None):
                continue
        except Exception:
            continue

        avg_upvotes = round(
            sum(scores.get(ticker, [0])) / max(len(scores.get(ticker, [1])), 1), 1
        )
        sample = list(dict.fromkeys(titles.get(ticker, [])))[:3]   # deduped
        results.append({
            "ticker":       ticker,
            "mentions":     count,
            "avg_upvotes":  avg_upvotes,
            "sample_posts": sample,
        })
        if len(results) >= top_n:
            break

    return results


# ─────────────────────────────────────────────────────────────────────────────
# TECHNICAL ANALYSIS  (uses `ta` library)
# ─────────────────────────────────────────────────────────────────────────────

def compute_technicals(hist: pd.DataFrame) -> dict:
    """
    Given a yfinance history DataFrame (OHLCV), compute all key indicators.
    Returns a dict of indicator values and a technical_score 0-100.
    """
    if hist is None or len(hist) < 30:
        return {"error": "insufficient history", "technical_score": 50}

    close  = hist["Close"]
    high   = hist["High"]
    low    = hist["Low"]
    volume = hist["Volume"]

    out = {}

    # ── Moving averages ──────────────────────────────────────────────────────
    ma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    price = float(close.iloc[-1])
    out["price"]  = round(price, 2)
    out["ma50"]   = round(ma50, 2)  if ma50  else None
    out["ma200"]  = round(ma200, 2) if ma200 else None
    out["vs_ma50"]  = round((price / ma50  - 1) * 100, 1) if ma50  else None
    out["vs_ma200"] = round((price / ma200 - 1) * 100, 1) if ma200 else None
    out["golden_cross"] = bool(ma50 and ma200 and ma50 > ma200)
    out["death_cross"]  = bool(ma50 and ma200 and ma50 < ma200)

    if TA_AVAILABLE:
        # ── RSI ──────────────────────────────────────────────────────────────
        rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi()
        out["rsi"] = round(float(rsi_series.iloc[-1]), 1)

        # ── MACD ─────────────────────────────────────────────────────────────
        macd_obj    = ta.trend.MACD(close)
        out["macd"]        = round(float(macd_obj.macd().iloc[-1]), 3)
        out["macd_signal"] = round(float(macd_obj.macd_signal().iloc[-1]), 3)
        out["macd_hist"]   = round(float(macd_obj.macd_diff().iloc[-1]), 3)
        out["macd_bullish"] = bool(out["macd"] > out["macd_signal"])

        # ── Bollinger Bands ───────────────────────────────────────────────────
        bb = ta.volatility.BollingerBands(close)
        out["bb_upper"]  = round(float(bb.bollinger_hband().iloc[-1]), 2)
        out["bb_lower"]  = round(float(bb.bollinger_lband().iloc[-1]), 2)
        out["bb_middle"] = round(float(bb.bollinger_mavg().iloc[-1]), 2)
        out["bb_pct"]    = round(float(bb.bollinger_pband().iloc[-1]), 3)  # 0=lower,1=upper

        # ── ATR (volatility) ──────────────────────────────────────────────────
        atr = ta.volatility.AverageTrueRange(high, low, close)
        out["atr"] = round(float(atr.average_true_range().iloc[-1]), 2)

        # ── Stochastic RSI ────────────────────────────────────────────────────
        stoch = ta.momentum.StochRSIIndicator(close)
        out["stoch_rsi"] = round(float(stoch.stochrsi().iloc[-1]), 3)

        # ── Williams %R ───────────────────────────────────────────────────────
        wr = ta.momentum.WilliamsRIndicator(high, low, close)
        out["williams_r"] = round(float(wr.williams_r().iloc[-1]), 1)

        # ── Volume trend ──────────────────────────────────────────────────────
        avg_vol_20 = float(volume.rolling(20).mean().iloc[-1])
        last_vol   = float(volume.iloc[-1])
        out["volume_ratio"] = round(last_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

    # ── Support / Resistance (simple swing highs/lows) ────────────────────────
    recent = close.tail(60)
    out["support"]    = round(float(recent.min()), 2)
    out["resistance"] = round(float(recent.max()), 2)

    # ── 52-week range ─────────────────────────────────────────────────────────
    yr = close.tail(252)
    out["week52_low"]  = round(float(yr.min()), 2)
    out["week52_high"] = round(float(yr.max()), 2)
    out["pct_from_52high"] = round((price / float(yr.max()) - 1) * 100, 1)

    # ── Price performance ─────────────────────────────────────────────────────
    if len(close) >= 5:
        out["return_5d"]  = round((price / float(close.iloc[-6])  - 1) * 100, 1)
    if len(close) >= 22:
        out["return_1mo"] = round((price / float(close.iloc[-23]) - 1) * 100, 1)
    if len(close) >= 63:
        out["return_3mo"] = round((price / float(close.iloc[-64]) - 1) * 100, 1)
    if len(close) >= 252:
        out["return_1yr"] = round((price / float(close.iloc[-253]) - 1) * 100, 1)

    # ── TECHNICAL SCORE ───────────────────────────────────────────────────────
    score  = 50
    if TA_AVAILABLE:
        rsi = out.get("rsi", 50)
        if   30  < rsi < 60: score += 10   # healthy zone
        elif 60  <= rsi < 70: score += 15  # bullish
        elif rsi >= 70: score += 5         # overbought — slight penalty
        elif rsi <= 30: score -= 10        # oversold

        if out.get("macd_bullish"):        score += 10
        if out.get("golden_cross"):        score += 10
        if out.get("death_cross"):         score -= 15
        if out.get("volume_ratio", 1) > 2: score += 8
        bb_pct = out.get("bb_pct", 0.5)
        if 0.2 < bb_pct < 0.8:            score += 5   # price inside bands, not extended

    if ma50 and price > ma50:   score += 8
    if ma200 and price > ma200: score += 7
    pct_from_high = out.get("pct_from_52high", 0)
    if pct_from_high > -5:  score += 5   # near 52-week high
    if pct_from_high < -30: score -= 8   # far below 52-week high

    out["technical_score"] = max(0, min(100, score))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# FUNDAMENTALS  (yfinance)
# ─────────────────────────────────────────────────────────────────────────────

def compute_fundamentals(info: dict) -> dict:
    """
    Extract key fundamental metrics from yfinance .info dict.
    Returns cleaned dict + fundamentals_score 0-100.
    """
    def safe(key, default=None):
        val = info.get(key, default)
        return None if val in (None, "N/A", float("inf"), float("-inf"), math.nan) else val

    out = {
        "company":           safe("longName") or safe("shortName", "Unknown"),
        "sector":            safe("sector", "—"),
        "industry":          safe("industry", "—"),
        "market_cap":        safe("marketCap"),
        "revenue_ttm":       safe("totalRevenue"),
        "revenue_growth":    safe("revenueGrowth"),    # YoY decimal
        "gross_margin":      safe("grossMargins"),     # decimal
        "operating_margin":  safe("operatingMargins"),
        "profit_margin":     safe("profitMargins"),
        "eps_ttm":           safe("trailingEps"),
        "eps_forward":       safe("forwardEps"),
        "pe_trailing":       safe("trailingPE"),
        "pe_forward":        safe("forwardPE"),
        "peg_ratio":         safe("pegRatio"),
        "price_to_book":     safe("priceToBook"),
        "ev_to_ebitda":      safe("enterpriseToEbitda"),
        "debt_equity":       safe("debtToEquity"),
        "current_ratio":     safe("currentRatio"),
        "free_cash_flow":    safe("freeCashflow"),
        "cash":              safe("totalCash"),
        "dividend_yield":    safe("dividendYield"),
        "beta":              safe("beta"),
        "52wk_high":         safe("fiftyTwoWeekHigh"),
        "52wk_low":          safe("fiftyTwoWeekLow"),
        "analyst_target":    safe("targetMeanPrice"),
        "analyst_low":       safe("targetLowPrice"),
        "analyst_high":      safe("targetHighPrice"),
        "analyst_count":     safe("numberOfAnalystOpinions"),
        "recommendation":    safe("recommendationKey", "—"),
        "earnings_date":     None,
    }

    # Next earnings date
    try:
        cal = info.get("earningsTimestamps") or []
        if cal:
            ts  = cal[0]
            out["earnings_date"] = datetime.fromtimestamp(ts).strftime("%b %d, %Y")
    except Exception:
        pass

    # ── FUNDAMENTALS SCORE ────────────────────────────────────────────────────
    score = 50
    rg = out.get("revenue_growth")
    if rg is not None:
        if rg > 0.30:  score += 20
        elif rg > 0.15: score += 12
        elif rg > 0.05: score += 5
        elif rg < 0:    score -= 10

    gm = out.get("gross_margin")
    if gm is not None:
        if gm > 0.60:   score += 12
        elif gm > 0.35: score += 6
        elif gm < 0.10: score -= 8

    pm = out.get("profit_margin")
    if pm is not None:
        if pm > 0.20:   score += 10
        elif pm > 0.05: score += 5
        elif pm < 0:    score -= 10

    pe  = out.get("pe_forward") or out.get("pe_trailing")
    peg = out.get("peg_ratio")
    if pe is not None and pe > 0:
        if pe < 15:   score += 10
        elif pe < 25: score += 5
        elif pe > 80: score -= 5
    if peg is not None and 0 < peg < 1.5:
        score += 8

    de = out.get("debt_equity")
    if de is not None:
        if de < 0.5:  score += 8
        elif de > 2:  score -= 5

    fcf = out.get("free_cash_flow")
    if fcf is not None and fcf > 0: score += 8

    rec = (out.get("recommendation") or "").lower()
    if "buy" in rec or "strong_buy" in rec: score += 8
    elif "sell" in rec:                     score -= 8

    out["fundamentals_score"] = max(0, min(100, score))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MOMENTUM SCORING  (options flow + Reddit + news volume)
# ─────────────────────────────────────────────────────────────────────────────

def compute_momentum(ticker_str: str, tech: dict, reddit_data: reddit_data=None) -> dict:
    """
    Compute momentum score from available signals.
    reddit_data: optional dict from get_reddit_trending for this ticker.
    """
    out  = {}
    score = 50

    # ── Options flow (call/put ratio from yfinance) ───────────────────────────
    try:
        tkr    = yf.Ticker(ticker_str)
        dates  = tkr.options
        if dates:
            opt_date = dates[0]
            chain    = tkr.option_chain(opt_date)
            call_vol = int(chain.calls["volume"].sum())
            put_vol  = int(chain.puts["volume"].sum())
            cp_ratio = round(call_vol / max(put_vol, 1), 2)
            out["call_volume"]   = call_vol
            out["put_volume"]    = put_vol
            out["call_put_ratio"] = cp_ratio
            if cp_ratio > 2:    score += 15
            elif cp_ratio > 1.2: score += 8
            elif cp_ratio < 0.7: score -= 8
    except Exception:
        out["call_volume"] = None
        out["put_volume"]  = None

    # ── Reddit ────────────────────────────────────────────────────────────────
    if reddit_data:
        mentions = reddit_data.get("mentions", 0)
        out["reddit_mentions"]  = mentions
        out["reddit_upvotes"]   = reddit_data.get("avg_upvotes", 0)
        out["reddit_posts"]     = reddit_data.get("sample_posts", [])
        if mentions > 100: score += 20
        elif mentions > 50: score += 12
        elif mentions > 20: score += 6
        elif mentions > 5:  score += 2
    else:
        out["reddit_mentions"] = 0

    # ── Volume momentum ───────────────────────────────────────────────────────
    vol_ratio = tech.get("volume_ratio", 1)
    if vol_ratio > 3:   score += 12
    elif vol_ratio > 2: score += 8
    elif vol_ratio > 1.5: score += 4

    # ── Price momentum ────────────────────────────────────────────────────────
    r1m = tech.get("return_1mo", 0) or 0
    if r1m > 20:   score += 10
    elif r1m > 10: score += 6
    elif r1m > 5:  score += 3
    elif r1m < -20: score -= 10
    elif r1m < -10: score -= 5

    # ── Near 52-week high ─────────────────────────────────────────────────────
    pct_from_high = tech.get("pct_from_52high", -50)
    if pct_from_high > -5:   score += 8
    elif pct_from_high < -50: score -= 5

    out["momentum_score"] = max(0, min(100, score))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# AI NARRATIVE (optional Anthropic call)
# ─────────────────────────────────────────────────────────────────────────────

def get_ai_analysis(ticker: str, tech: dict, fund: dict, mom: dict) -> str:
    """
    Call Claude to write a short analysis paragraph.
    Falls back to a template string if no API key.
    """
    if not ANTHROPIC_AVAILABLE:
        return _template_analysis(ticker, tech, fund, mom)

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = f"""You are a stock analyst. Write a concise 2-3 sentence analysis for {ticker} based on these live metrics:

Price: ${tech.get('price')} | RSI: {tech.get('rsi')} | MACD bullish: {tech.get('macd_bullish')}
vs 50-day MA: {tech.get('vs_ma50')}% | vs 200-day MA: {tech.get('vs_ma200')}%
Revenue growth: {fund.get('revenue_growth')} | Forward P/E: {fund.get('pe_forward')} | Gross margin: {fund.get('gross_margin')}
Reddit mentions: {mom.get('reddit_mentions')} | Call/Put ratio: {mom.get('call_put_ratio')}
Recommendation: {fund.get('recommendation')} | Analyst target: ${fund.get('analyst_target')}

Be specific, factual, and highlight the single most important signal (bullish or bearish).
Respond with just the analysis paragraph, no intro."""

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()

def _template_analysis(ticker, tech, fund, mom):
    rsi   = tech.get("rsi", 50)
    r1m   = tech.get("return_1mo", 0) or 0
    fwd_pe = fund.get("pe_forward")
    target = fund.get("analyst_target")
    price  = tech.get("price", 0)
    rec    = (fund.get("recommendation") or "hold").replace("_", " ").title()
    upside = round((target / price - 1) * 100, 1) if target and price else None

    rsi_str = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"
    trend   = "up" if tech.get("golden_cross") else "down" if tech.get("death_cross") else "mixed"

    parts = [f"{ticker} is showing {rsi_str} RSI ({rsi}) with a {trend} trend."]
    if r1m:
        parts.append(f"The stock is {r1m:+.1f}% over the past month.")
    if upside:
        parts.append(f"Analysts rate it {rec} with ${target} target ({upside:+.1f}% upside).")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# OVERALL SCORE
# ─────────────────────────────────────────────────────────────────────────────

def compute_overall(mom_score, fund_score, tech_score):
    """Weighted average: momentum 35%, fundamentals 40%, technical 25%."""
    return round(mom_score * 0.35 + fund_score * 0.40 + tech_score * 0.25)


# ─────────────────────────────────────────────────────────────────────────────
# FULL SINGLE-TICKER ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str, reddit_map: reddit_data=None) -> dict:
    """
    Full analysis for one ticker.
    reddit_map: optional dict of {ticker: reddit_data} from a prior scan.
    """
    tkr  = yf.Ticker(ticker)
    info = tkr.info or {}

    # History — try 2yr first, fall back to 1yr, then 6mo
    hist = None
    for period in ("2y", "1y", "6mo"):
        try:
            hist = tkr.history(period=period)
            if len(hist) >= 30:
                break
        except Exception:
            pass

    tech  = compute_technicals(hist)
    fund  = compute_fundamentals(info)
    rdata = (reddit_map or {}).get(ticker)
    mom   = compute_momentum(ticker, tech, rdata)

    # Trade setup (simple rule-based)
    setup = _build_trade_setup(tech, fund)

    # AI narrative
    analysis = get_ai_analysis(ticker, tech, fund, mom)

    overall = compute_overall(mom["momentum_score"], fund["fundamentals_score"], tech["technical_score"])

    # Tag & color
    tag, tag_color = _assign_tag(mom, fund, tech, overall)

    return {
        "ticker":        ticker,
        "name":          fund.get("company", ticker),
        "sector":        fund.get("sector", "—"),
        "price":         tech.get("price"),
        "change_1d":     tech.get("return_5d"),     # rough proxy
        "scores": {
            "momentum":     mom["momentum_score"],
            "fundamentals": fund["fundamentals_score"],
            "technical":    tech["technical_score"],
            "overall":      overall,
        },
        "tag":       tag,
        "tagColor":  tag_color,
        "blurb":     analysis,
        "tech":      tech,
        "fund":      fund,
        "mom":       mom,
        "setup":     setup,
        "updatedAt": time.strftime("%H:%M:%S"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOP-PICKS SCAN
# ─────────────────────────────────────────────────────────────────────────────

# Watchlist to always score (add your own tickers here)
BASE_WATCHLIST = [
    "AMD", "NVDA", "INTC", "MSFT", "AAPL", "META", "GOOGL", "AMZN",
    "TSLA", "CRWV", "ONTO", "FIG", "DVLT",
]

def run_top_picks_scan():
    """
    1. Pull Reddit trending tickers
    2. Merge with base watchlist
    3. Score everything
    4. Return top 10 sorted by overall score
    """
    # Reddit
    reddit_tickers = []
    reddit_map     = {}
    try:
        reddit_tickers = get_reddit_trending(limit_per_sub=100, top_n=30)
        reddit_map     = {r["ticker"]: r for r in reddit_tickers}
    except Exception as e:
        print(f"[reddit] skipped: {e}")

    # Combine watchlist + reddit findings
    all_tickers = list(dict.fromkeys(BASE_WATCHLIST + [r["ticker"] for r in reddit_tickers]))

    results = []
    for ticker in all_tickers[:40]:   # cap at 40 to avoid rate limits
        try:
            data = analyze_ticker(ticker, reddit_map)
            results.append(data)
            time.sleep(0.3)   # be polite to yfinance
        except Exception as e:
            print(f"[scan] {ticker} failed: {e}")

    results.sort(key=lambda x: x["scores"]["overall"], reverse=True)
    return results[:10]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_trade_setup(tech, fund):
    price  = tech.get("price", 0)
    ma50   = tech.get("ma50")
    ma200  = tech.get("ma200")
    target = fund.get("analyst_target")
    rsi    = tech.get("rsi", 50)

    entry_low  = round(ma50  * 0.98, 2) if ma50  else round(price * 0.95, 2)
    entry_high = round(ma50  * 1.01, 2) if ma50  else round(price * 1.00, 2)
    stop       = round(ma200 * 0.97, 2) if ma200 else round(price * 0.90, 2)
    t1         = target if target else round(price * 1.15, 2)
    t2         = round(t1 * 1.10, 2)

    risk   = abs(price - stop)
    reward = abs(t1 - price)
    rr     = f"{round(reward / risk, 1)}:1" if risk > 0 else "N/A"

    if rsi > 75:
        bias = "WAIT FOR PULLBACK"
    elif rsi < 30:
        bias = "OVERSOLD — WATCH FOR REVERSAL"
    elif tech.get("golden_cross") and tech.get("macd_bullish"):
        bias = "BULLISH"
    elif tech.get("death_cross"):
        bias = "BEARISH — CAUTION"
    else:
        bias = "NEUTRAL"

    return {
        "bias":   bias,
        "entry":  f"${entry_low}–${entry_high}",
        "stop":   f"<${stop}",
        "t1":     f"${t1}",
        "t2":     f"${t2}",
        "rr":     rr,
    }

def _assign_tag(mom, fund, tech, overall):
    rsi   = tech.get("rsi", 50)
    r1m   = tech.get("return_1mo", 0) or 0
    rg    = fund.get("revenue_growth", 0) or 0
    cp    = mom.get("call_put_ratio", 1) or 1

    if rsi > 70 and r1m > 15:
        return "MOMENTUM",   "#00e87a"
    if cp > 2.5:
        return "OPTIONS FLOW", "#f5c518"
    if rg > 0.40:
        return "HIGH GROWTH",  "#38b6ff"
    if overall >= 75:
        return "STRONG BUY",   "#00e87a"
    if overall >= 60:
        return "BULLISH",      "#f5c518"
    if overall < 40:
        return "HIGH RISK",    "#ff8c42"
    return "NEUTRAL", "#4a6380"
