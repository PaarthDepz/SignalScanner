"""
scanner.py - Core data engine for SIGNAL
Data: Yahoo Finance (free), Reddit public JSON (free), Anthropic (optional)
"""

import os
import re
import time
import math
import requests
from collections import Counter

import yfinance as yf
import pandas as pd
import numpy as np

try:
    import ta
    TA_AVAILABLE = True
except Exception:
    TA_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = bool(os.environ.get("ANTHROPIC_API_KEY"))
except Exception:
    ANTHROPIC_AVAILABLE = False


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

SUBREDDITS = [
    "wallstreetbets", "stocks", "investing",
    "pennystocks", "StockMarket", "options"
]

BLACKLIST = {
    "A","B","C","D","E","F","G","H","I","J","K","L","M",
    "N","O","P","Q","R","S","T","U","V","W","X","Y","Z",
    "THE","FOR","ARE","IS","BE","OR","AND","IN","ON","AT","TO","OF",
    "IT","AN","IF","SO","UP","DO","GO","NOW","ALL","ITS","BY","NEW","OUT",
    "CEO","IPO","AI","ETF","PE","EPS","YTD","DD","WSB","IMO","ATH",
    "PM","AM","USD","US","USA","UK","EU","GDP","CPI","SEC","FED",
    "ATM","OTM","ITM","DCA","HODL","YOLO","FOMO","OP","RH","TD",
    "WS","PR","IR","ER","QE","RE","AS","HE","WE","MY","NO","ME",
    "HI","OK","YO","SP","EV","VC","IPO","NFT","TLDR","EOD","EOW",
}

TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')

REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SIGNAL-Scanner/1.0)",
    "Accept": "application/json",
}

BASE_WATCHLIST = [
    "AMD", "NVDA", "MSFT", "AAPL", "META",
    "GOOGL", "AMZN", "TSLA", "INTC", "PLTR",
]


# ---------------------------------------------------------------------------
# REDDIT  (no API key needed)
# ---------------------------------------------------------------------------

def _fetch_sub_posts(subreddit, limit=100):
    """Fetch posts from a subreddit via public JSON endpoint."""
    posts = []
    after = None
    fetched = 0

    while fetched < limit:
        url = "https://www.reddit.com/r/{}/new.json".format(subreddit)
        params = {"limit": min(100, limit - fetched), "raw_json": 1}
        if after:
            params["after"] = after
        try:
            resp = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=8)
            if resp.status_code == 429:
                time.sleep(3)
                continue
            if resp.status_code != 200:
                break
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            if not children:
                break
            for child in children:
                posts.append(child.get("data", {}))
            after = data.get("data", {}).get("after")
            fetched += len(children)
            if not after:
                break
            time.sleep(0.5)
        except Exception:
            break

    return posts


def get_reddit_trending(limit_per_sub=75, top_n=20):
    """
    Scan investing subreddits for ticker mentions.
    Returns list of dicts: {ticker, mentions, avg_upvotes, sample_posts}
    No API key required.
    """
    counts = Counter()
    titles = {}
    scores = {}

    for sub in SUBREDDITS:
        try:
            posts = _fetch_sub_posts(sub, limit=limit_per_sub)
        except Exception:
            continue

        for post in posts:
            title = post.get("title", "")
            body = post.get("selftext", "")
            score = int(post.get("score", 0))
            text = (title + " " + body).upper()
            found = set(TICKER_RE.findall(text)) - BLACKLIST
            for ticker in found:
                counts[ticker] += 1
                titles.setdefault(ticker, []).append(title[:80])
                scores.setdefault(ticker, []).append(score)

    results = []
    for ticker, count in counts.most_common(top_n * 4):
        if count < 2:
            continue
        try:
            fi = yf.Ticker(ticker).fast_info
            price = getattr(fi, "last_price", None)
            if not price:
                continue
        except Exception:
            continue

        avg_upvotes = 0
        sc = scores.get(ticker, [])
        if sc:
            avg_upvotes = round(sum(sc) / len(sc), 1)

        sample = list(dict.fromkeys(titles.get(ticker, [])))[:3]
        results.append({
            "ticker": ticker,
            "mentions": count,
            "avg_upvotes": avg_upvotes,
            "sample_posts": sample,
        })
        if len(results) >= top_n:
            break

    return results


# ---------------------------------------------------------------------------
# TECHNICALS
# ---------------------------------------------------------------------------

def compute_technicals(hist):
    """Compute technical indicators from OHLCV DataFrame."""
    if hist is None or len(hist) < 30:
        return {"technical_score": 50}

    close = hist["Close"].dropna()
    high = hist["High"].dropna()
    low = hist["Low"].dropna()
    volume = hist["Volume"].dropna()

    if len(close) < 20:
        return {"technical_score": 50}

    out = {}
    price = float(close.iloc[-1])
    out["price"] = round(price, 2)

    # Moving averages
    if len(close) >= 50:
        ma50 = float(close.rolling(50).mean().iloc[-1])
        out["ma50"] = round(ma50, 2)
        out["vs_ma50"] = round((price / ma50 - 1) * 100, 1)
    else:
        ma50 = None

    if len(close) >= 200:
        ma200 = float(close.rolling(200).mean().iloc[-1])
        out["ma200"] = round(ma200, 2)
        out["vs_ma200"] = round((price / ma200 - 1) * 100, 1)
    else:
        ma200 = None

    out["golden_cross"] = bool(ma50 and ma200 and ma50 > ma200)
    out["death_cross"] = bool(ma50 and ma200 and ma50 < ma200)

    # 52-week range
    yr = close.tail(252)
    out["week52_low"] = round(float(yr.min()), 2)
    out["week52_high"] = round(float(yr.max()), 2)
    out["pct_from_52high"] = round((price / float(yr.max()) - 1) * 100, 1)

    # Support / resistance (60-day)
    recent = close.tail(60)
    out["support"] = round(float(recent.min()), 2)
    out["resistance"] = round(float(recent.max()), 2)

    # Returns
    if len(close) >= 6:
        out["return_5d"] = round((price / float(close.iloc[-6]) - 1) * 100, 1)
    if len(close) >= 22:
        out["return_1mo"] = round((price / float(close.iloc[-22]) - 1) * 100, 1)
    if len(close) >= 63:
        out["return_3mo"] = round((price / float(close.iloc[-63]) - 1) * 100, 1)

    # Volume ratio
    if len(volume) >= 20:
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        last_vol = float(volume.iloc[-1])
        out["volume_ratio"] = round(last_vol / avg_vol, 2) if avg_vol > 0 else 1.0

    # TA library indicators
    if TA_AVAILABLE and len(close) >= 26:
        try:
            rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
            out["rsi"] = round(float(rsi.iloc[-1]), 1)
        except Exception:
            pass

        try:
            macd_obj = ta.trend.MACD(close)
            out["macd"] = round(float(macd_obj.macd().iloc[-1]), 3)
            out["macd_signal"] = round(float(macd_obj.macd_signal().iloc[-1]), 3)
            out["macd_bullish"] = bool(out["macd"] > out["macd_signal"])
        except Exception:
            pass

        try:
            bb = ta.volatility.BollingerBands(close)
            out["bb_upper"] = round(float(bb.bollinger_hband().iloc[-1]), 2)
            out["bb_lower"] = round(float(bb.bollinger_lband().iloc[-1]), 2)
            out["bb_pct"] = round(float(bb.bollinger_pband().iloc[-1]), 3)
        except Exception:
            pass

        try:
            stoch = ta.momentum.StochRSIIndicator(close)
            out["stoch_rsi"] = round(float(stoch.stochrsi().iloc[-1]), 3)
        except Exception:
            pass

        try:
            wr = ta.momentum.WilliamsRIndicator(high, low, close)
            out["williams_r"] = round(float(wr.williams_r().iloc[-1]), 1)
        except Exception:
            pass

        try:
            atr = ta.volatility.AverageTrueRange(high, low, close)
            out["atr"] = round(float(atr.average_true_range().iloc[-1]), 2)
        except Exception:
            pass

    # Score
    score = 50
    rsi = out.get("rsi", 50)
    if 30 < rsi < 60:
        score += 10
    elif 60 <= rsi < 70:
        score += 15
    elif rsi >= 70:
        score += 5
    elif rsi <= 30:
        score -= 10

    if out.get("macd_bullish"):
        score += 10
    if out.get("golden_cross"):
        score += 10
    if out.get("death_cross"):
        score -= 15
    if out.get("volume_ratio", 1) > 2:
        score += 8

    bb_pct = out.get("bb_pct", 0.5)
    if 0.2 < bb_pct < 0.8:
        score += 5

    if ma50 and price > ma50:
        score += 8
    if ma200 and price > ma200:
        score += 7

    pct_high = out.get("pct_from_52high", 0)
    if pct_high > -5:
        score += 5
    elif pct_high < -30:
        score -= 8

    out["technical_score"] = max(0, min(100, score))
    return out


# ---------------------------------------------------------------------------
# FUNDAMENTALS
# ---------------------------------------------------------------------------

def compute_fundamentals(info):
    """Extract fundamentals from yfinance info dict and score them."""
    def safe(key):
        val = info.get(key)
        if val is None:
            return None
        try:
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except Exception:
            return val

    out = {
        "company":          info.get("longName") or info.get("shortName", ""),
        "sector":           info.get("sector", ""),
        "industry":         info.get("industry", ""),
        "market_cap":       safe("marketCap"),
        "revenue_ttm":      safe("totalRevenue"),
        "revenue_growth":   safe("revenueGrowth"),
        "gross_margin":     safe("grossMargins"),
        "operating_margin": safe("operatingMargins"),
        "profit_margin":    safe("profitMargins"),
        "eps_ttm":          safe("trailingEps"),
        "eps_forward":      safe("forwardEps"),
        "pe_trailing":      safe("trailingPE"),
        "pe_forward":       safe("forwardPE"),
        "peg_ratio":        safe("pegRatio"),
        "price_to_book":    safe("priceToBook"),
        "debt_equity":      safe("debtToEquity"),
        "current_ratio":    safe("currentRatio"),
        "free_cash_flow":   safe("freeCashflow"),
        "cash":             safe("totalCash"),
        "beta":             safe("beta"),
        "week52_high":      safe("fiftyTwoWeekHigh"),
        "week52_low":       safe("fiftyTwoWeekLow"),
        "analyst_target":   safe("targetMeanPrice"),
        "analyst_low":      safe("targetLowPrice"),
        "analyst_high":     safe("targetHighPrice"),
        "analyst_count":    safe("numberOfAnalystOpinions"),
        "recommendation":   info.get("recommendationKey", ""),
        "earnings_date":    None,
    }

    try:
        timestamps = info.get("earningsTimestamps") or []
        if timestamps:
            from datetime import datetime
            out["earnings_date"] = datetime.fromtimestamp(timestamps[0]).strftime("%b %d, %Y")
    except Exception:
        pass

    # Score
    score = 50
    rg = out.get("revenue_growth")
    if rg is not None:
        if rg > 0.30:   score += 20
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

    pe = out.get("pe_forward") or out.get("pe_trailing")
    if pe and pe > 0:
        if pe < 15:   score += 10
        elif pe < 25: score += 5
        elif pe > 80: score -= 5

    peg = out.get("peg_ratio")
    if peg and 0 < peg < 1.5:
        score += 8

    de = out.get("debt_equity")
    if de is not None:
        if de < 50:    score += 8
        elif de > 200: score -= 5

    fcf = out.get("free_cash_flow")
    if fcf and fcf > 0:
        score += 8

    rec = (out.get("recommendation") or "").lower()
    if "buy" in rec:  score += 8
    elif "sell" in rec: score -= 8

    out["fundamentals_score"] = max(0, min(100, score))
    return out


# ---------------------------------------------------------------------------
# MOMENTUM
# ---------------------------------------------------------------------------

def compute_momentum(ticker_str, tech, reddit_data=None):
    """Score momentum from options flow, Reddit, volume, and price action."""
    out = {}
    score = 50

    # Options chain
    try:
        tkr = yf.Ticker(ticker_str)
        dates = tkr.options
        if dates:
            chain = tkr.option_chain(dates[0])
            call_vol = int(chain.calls["volume"].fillna(0).sum())
            put_vol  = int(chain.puts["volume"].fillna(0).sum())
            cp_ratio = round(call_vol / max(put_vol, 1), 2)
            out["call_volume"] = call_vol
            out["put_volume"] = put_vol
            out["call_put_ratio"] = cp_ratio
            if cp_ratio > 2:     score += 15
            elif cp_ratio > 1.2: score += 8
            elif cp_ratio < 0.7: score -= 8
    except Exception:
        pass

    # Reddit
    if reddit_data:
        mentions = reddit_data.get("mentions", 0)
        out["reddit_mentions"] = mentions
        out["reddit_upvotes"]  = reddit_data.get("avg_upvotes", 0)
        out["reddit_posts"]    = reddit_data.get("sample_posts", [])
        if mentions > 100:   score += 20
        elif mentions > 50:  score += 12
        elif mentions > 20:  score += 6
        elif mentions > 5:   score += 2
    else:
        out["reddit_mentions"] = 0
        out["reddit_posts"] = []

    # Volume
    vol_ratio = tech.get("volume_ratio", 1)
    if vol_ratio > 3:     score += 12
    elif vol_ratio > 2:   score += 8
    elif vol_ratio > 1.5: score += 4

    # Price momentum
    r1m = tech.get("return_1mo") or 0
    if r1m > 20:    score += 10
    elif r1m > 10:  score += 6
    elif r1m > 5:   score += 3
    elif r1m < -20: score -= 10
    elif r1m < -10: score -= 5

    # Near 52-week high
    pct = tech.get("pct_from_52high", -50)
    if pct > -5:    score += 8
    elif pct < -50: score -= 5

    out["momentum_score"] = max(0, min(100, score))
    return out


# ---------------------------------------------------------------------------
# AI ANALYSIS TEXT
# ---------------------------------------------------------------------------

def get_ai_analysis(ticker, tech, fund, mom):
    """Generate analysis text. Uses Claude if key available, else template."""
    if ANTHROPIC_AVAILABLE:
        try:
            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            prompt = (
                "You are a stock analyst. Write 2-3 sentences about {} based on: "
                "Price ${}, RSI {}, vs 50-day MA {}%, "
                "Revenue growth {}%, Forward P/E {}, "
                "Reddit mentions {}, Call/Put ratio {}, "
                "Analyst recommendation: {}, target ${}. "
                "Be specific and highlight the single most important signal."
            ).format(
                ticker,
                tech.get("price", "N/A"),
                tech.get("rsi", "N/A"),
                tech.get("vs_ma50", "N/A"),
                round((fund.get("revenue_growth") or 0) * 100, 1),
                fund.get("pe_forward", "N/A"),
                mom.get("reddit_mentions", 0),
                mom.get("call_put_ratio", "N/A"),
                fund.get("recommendation", "N/A"),
                fund.get("analyst_target", "N/A"),
            )
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.content[0].text.strip()
        except Exception:
            pass

    return _template_analysis(ticker, tech, fund, mom)


def _template_analysis(ticker, tech, fund, mom):
    rsi    = tech.get("rsi", 50)
    r1m    = tech.get("return_1mo") or 0
    target = fund.get("analyst_target")
    price  = tech.get("price") or 0
    rec    = (fund.get("recommendation") or "hold").replace("_", " ").title()
    upside = round((target / price - 1) * 100, 1) if (target and price) else None
    rsi_str = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"
    trend = "bullish" if tech.get("golden_cross") else "bearish" if tech.get("death_cross") else "mixed"
    parts = ["{} showing {} RSI ({}) with {} trend.".format(ticker, rsi_str, rsi, trend)]
    if r1m:
        parts.append("{:+.1f}% over the past month.".format(r1m))
    if upside and target:
        parts.append("Analysts rate {} with ${} target ({:+.1f}% upside).".format(rec, target, upside))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# OVERALL SCORE
# ---------------------------------------------------------------------------

def compute_overall(mom_score, fund_score, tech_score):
    return round(mom_score * 0.35 + fund_score * 0.40 + tech_score * 0.25)


# ---------------------------------------------------------------------------
# SINGLE TICKER ANALYSIS
# ---------------------------------------------------------------------------

def analyze_ticker(ticker, reddit_map=None):
    """Full analysis for one ticker. Returns dict ready for the API."""
    tkr  = yf.Ticker(ticker)
    info = {}
    try:
        info = tkr.info or {}
    except Exception:
        pass

    # History
    hist = None
    for period in ("2y", "1y", "6mo", "3mo"):
        try:
            h = tkr.history(period=period)
            if len(h) >= 30:
                hist = h
                break
        except Exception:
            continue

    tech  = compute_technicals(hist)
    fund  = compute_fundamentals(info)
    rdata = (reddit_map or {}).get(ticker)
    mom   = compute_momentum(ticker, tech, rdata)
    setup = _build_trade_setup(tech, fund)
    blurb = get_ai_analysis(ticker, tech, fund, mom)
    overall = compute_overall(mom["momentum_score"], fund["fundamentals_score"], tech["technical_score"])
    tag, tag_color = _assign_tag(mom, fund, tech, overall)

    return {
        "ticker":     ticker,
        "name":       fund.get("company", ticker),
        "sector":     fund.get("sector", ""),
        "price":      tech.get("price"),
        "change_1d":  tech.get("return_5d"),
        "scores": {
            "momentum":     mom["momentum_score"],
            "fundamentals": fund["fundamentals_score"],
            "technical":    tech["technical_score"],
            "overall":      overall,
        },
        "tag":       tag,
        "tagColor":  tag_color,
        "blurb":     blurb,
        "tech":      tech,
        "fund":      fund,
        "mom":       mom,
        "setup":     setup,
        "updatedAt": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# TOP PICKS SCAN
# ---------------------------------------------------------------------------

def run_top_picks_scan(include_reddit=False):
    """
    Score BASE_WATCHLIST tickers using Yahoo Finance.
    Reddit scanning is optional (slow) — enabled via include_reddit=True.
    """
    reddit_map = {}
    extra_tickers = []

    if include_reddit:
        try:
            reddit_tickers = get_reddit_trending(limit_per_sub=50, top_n=15)
            reddit_map = {r["ticker"]: r for r in reddit_tickers}
            extra_tickers = [r["ticker"] for r in reddit_tickers]
        except Exception as e:
            print("Reddit scan skipped: {}".format(e))

    all_tickers = list(dict.fromkeys(BASE_WATCHLIST + extra_tickers))[:20]

    results = []
    for ticker in all_tickers:
        try:
            data = analyze_ticker(ticker, reddit_map)
            results.append(data)
            time.sleep(0.2)
        except Exception as e:
            print("Skipped {}: {}".format(ticker, e))

    results.sort(key=lambda x: x["scores"]["overall"], reverse=True)
    return results[:10]


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _build_trade_setup(tech, fund):
    price  = tech.get("price") or 0
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
    rr     = "{}:1".format(round(reward / risk, 1)) if risk > 0 else "N/A"

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
        "bias":  bias,
        "entry": "${}-${}".format(entry_low, entry_high),
        "stop":  "<${}".format(stop),
        "t1":    "${}".format(t1),
        "t2":    "${}".format(t2),
        "rr":    rr,
    }


def _assign_tag(mom, fund, tech, overall):
    rsi  = tech.get("rsi", 50)
    r1m  = tech.get("return_1mo") or 0
    rg   = fund.get("revenue_growth") or 0
    cp   = mom.get("call_put_ratio") or 1

    if rsi > 70 and r1m > 15:
        return "MOMENTUM",    "#00e87a"
    if cp > 2.5:
        return "OPTIONS FLOW","#f5c518"
    if rg > 0.40:
        return "HIGH GROWTH", "#38b6ff"
    if overall >= 75:
        return "STRONG BUY",  "#00e87a"
    if overall >= 60:
        return "BULLISH",     "#f5c518"
    if overall < 40:
        return "HIGH RISK",   "#ff8c42"
    return "NEUTRAL", "#4a6380"
