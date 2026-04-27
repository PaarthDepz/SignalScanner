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
    import backtest as bt
    BACKTEST_AVAILABLE = True
except Exception:
    BACKTEST_AVAILABLE = False

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
    "HI","OK","YO","SP","EV","VC","NFT","TLDR","EOD","EOW",
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
# REDDIT
# ---------------------------------------------------------------------------

def _fetch_sub_posts(subreddit, limit=75):
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
            body  = post.get("selftext", "")
            score = int(post.get("score", 0))
            text  = (title + " " + body).upper()
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
            if not getattr(fi, "last_price", None):
                continue
        except Exception:
            continue
        sc = scores.get(ticker, [])
        results.append({
            "ticker":       ticker,
            "mentions":     count,
            "avg_upvotes":  round(sum(sc) / len(sc), 1) if sc else 0,
            "sample_posts": list(dict.fromkeys(titles.get(ticker, [])))[:3],
        })
        if len(results) >= top_n:
            break
    return results


# ---------------------------------------------------------------------------
# TECHNICALS  — granular scoring, not just binary signals
# ---------------------------------------------------------------------------

def compute_technicals(hist):
    if hist is None or len(hist) < 30:
        return {"technical_score": 50}

    close  = hist["Close"].dropna()
    high   = hist["High"].dropna()
    low    = hist["Low"].dropna()
    volume = hist["Volume"].dropna()

    if len(close) < 20:
        return {"technical_score": 50}

    out   = {}
    price = float(close.iloc[-1])
    out["price"] = round(price, 2)

    # Moving averages
    ma50 = ma200 = None
    if len(close) >= 50:
        ma50 = float(close.rolling(50).mean().iloc[-1])
        out["ma50"]    = round(ma50, 2)
        out["vs_ma50"] = round((price / ma50 - 1) * 100, 1)
    if len(close) >= 200:
        ma200 = float(close.rolling(200).mean().iloc[-1])
        out["ma200"]    = round(ma200, 2)
        out["vs_ma200"] = round((price / ma200 - 1) * 100, 1)

    out["golden_cross"] = bool(ma50 and ma200 and ma50 > ma200)
    out["death_cross"]  = bool(ma50 and ma200 and ma50 < ma200)

    # 52-week
    yr = close.tail(252)
    out["week52_low"]      = round(float(yr.min()), 2)
    out["week52_high"]     = round(float(yr.max()), 2)
    out["pct_from_52high"] = round((price / float(yr.max()) - 1) * 100, 1)

    # Support / resistance
    out["support"]    = round(float(close.tail(60).min()), 2)
    out["resistance"] = round(float(close.tail(60).max()), 2)

    # Returns
    def pct_ret(n):
        if len(close) > n:
            prev = float(close.iloc[-(n + 1)])
            return round((price / prev - 1) * 100, 1) if prev else None
        return None

    out["return_5d"]  = pct_ret(5)
    out["return_1mo"] = pct_ret(21)
    out["return_3mo"] = pct_ret(63)

    # Volume ratio
    if len(volume) >= 20:
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        out["volume_ratio"] = round(float(volume.iloc[-1]) / avg_vol, 2) if avg_vol > 0 else 1.0

    # TA indicators
    if TA_AVAILABLE and len(close) >= 26:
        try:
            v = float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1])
            if not math.isnan(v): out["rsi"] = round(v, 1)
        except Exception: pass

        try:
            mo = ta.trend.MACD(close)
            m, ms = float(mo.macd().iloc[-1]), float(mo.macd_signal().iloc[-1])
            if not (math.isnan(m) or math.isnan(ms)):
                out["macd"]         = round(m, 3)
                out["macd_signal"]  = round(ms, 3)
                out["macd_hist"]    = round(m - ms, 3)
                out["macd_bullish"] = bool(m > ms)
        except Exception: pass

        try:
            bb = ta.volatility.BollingerBands(close)
            bp = float(bb.bollinger_pband().iloc[-1])
            if not math.isnan(bp):
                out["bb_upper"] = round(float(bb.bollinger_hband().iloc[-1]), 2)
                out["bb_lower"] = round(float(bb.bollinger_lband().iloc[-1]), 2)
                out["bb_pct"]   = round(bp, 3)
        except Exception: pass

        try:
            v = float(ta.momentum.StochRSIIndicator(close).stochrsi().iloc[-1])
            if not math.isnan(v): out["stoch_rsi"] = round(v, 3)
        except Exception: pass

        try:
            v = float(ta.momentum.WilliamsRIndicator(high, low, close).williams_r().iloc[-1])
            if not math.isnan(v): out["williams_r"] = round(v, 1)
        except Exception: pass

        try:
            v = float(ta.volatility.AverageTrueRange(high, low, close).average_true_range().iloc[-1])
            if not math.isnan(v): out["atr"] = round(v, 2)
        except Exception: pass

    # -----------------------------------------------------------------------
    # GRANULAR SCORING — uses actual numeric values not just binary flags
    # -----------------------------------------------------------------------
    score = 50

    # RSI: exact value matters
    rsi = out.get("rsi")
    if rsi is not None:
        if rsi < 30:        score -= 15   # oversold
        elif rsi < 40:      score -= 5
        elif rsi < 50:      score += 3
        elif rsi < 60:      score += 8
        elif rsi < 65:      score += 12
        elif rsi < 70:      score += 10
        elif rsi < 75:      score += 6
        else:               score += 2    # very overbought — diminishing returns

    # MACD histogram magnitude matters
    macd_hist = out.get("macd_hist")
    if macd_hist is not None:
        if macd_hist > 2:   score += 12
        elif macd_hist > 0.5: score += 8
        elif macd_hist > 0:   score += 4
        elif macd_hist > -0.5: score -= 4
        elif macd_hist > -2:   score -= 8
        else:                  score -= 12

    # % above/below 50-day MA — magnitude matters
    vs50 = out.get("vs_ma50")
    if vs50 is not None:
        if vs50 > 20:       score += 5    # very extended — not great
        elif vs50 > 10:     score += 10
        elif vs50 > 3:      score += 8
        elif vs50 > 0:      score += 5
        elif vs50 > -5:     score -= 3
        elif vs50 > -15:    score -= 8
        else:               score -= 14

    # % above/below 200-day MA
    vs200 = out.get("vs_ma200")
    if vs200 is not None:
        if vs200 > 0:       score += 7
        elif vs200 > -10:   score -= 5
        else:               score -= 12

    # Golden/death cross
    if out.get("golden_cross"):  score += 6
    if out.get("death_cross"):   score -= 10

    # Volume ratio
    vr = out.get("volume_ratio", 1)
    if vr > 3:      score += 12
    elif vr > 2:    score += 8
    elif vr > 1.5:  score += 4
    elif vr < 0.5:  score -= 4

    # Bollinger %B
    bb_pct = out.get("bb_pct")
    if bb_pct is not None:
        if bb_pct > 0.9:    score -= 5   # very extended
        elif bb_pct > 0.7:  score += 3
        elif bb_pct > 0.4:  score += 6
        elif bb_pct > 0.2:  score += 3
        else:               score -= 5   # near lower band

    # Distance from 52-week high
    pct_high = out.get("pct_from_52high", 0)
    if pct_high > -3:       score += 8
    elif pct_high > -10:    score += 4
    elif pct_high > -25:    score -= 3
    elif pct_high > -50:    score -= 8
    else:                   score -= 14

    # 1-month return momentum
    r1m = out.get("return_1mo") or 0
    if r1m > 25:    score += 8
    elif r1m > 15:  score += 6
    elif r1m > 8:   score += 4
    elif r1m > 0:   score += 2
    elif r1m < -20: score -= 8
    elif r1m < -10: score -= 5
    elif r1m < 0:   score -= 2

    out["technical_score"] = max(0, min(100, score))
    return out


# ---------------------------------------------------------------------------
# FUNDAMENTALS — granular scoring
# ---------------------------------------------------------------------------

def _safe_float(info, key):
    val = info.get(key)
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


def compute_fundamentals(ticker_str, info, tkr_obj):
    # Also try fast_info
    fi = {}
    try:
        fast = tkr_obj.fast_info
        fi["market_cap"]  = getattr(fast, "market_cap", None)
        fi["week52_high"] = getattr(fast, "year_high", None)
        fi["week52_low"]  = getattr(fast, "year_low", None)
    except Exception:
        pass

    out = {
        "company":          info.get("longName") or info.get("shortName", ticker_str),
        "sector":           info.get("sector", ""),
        "industry":         info.get("industry", ""),
        "market_cap":       _safe_float(info, "marketCap") or fi.get("market_cap"),
        "revenue_ttm":      _safe_float(info, "totalRevenue"),
        "revenue_growth":   _safe_float(info, "revenueGrowth"),
        "gross_margin":     _safe_float(info, "grossMargins"),
        "operating_margin": _safe_float(info, "operatingMargins"),
        "profit_margin":    _safe_float(info, "profitMargins"),
        "eps_ttm":          _safe_float(info, "trailingEps"),
        "eps_forward":      _safe_float(info, "forwardEps"),
        "pe_trailing":      _safe_float(info, "trailingPE"),
        "pe_forward":       _safe_float(info, "forwardPE"),
        "peg_ratio":        _safe_float(info, "pegRatio"),
        "price_to_book":    _safe_float(info, "priceToBook"),
        "debt_equity":      _safe_float(info, "debtToEquity"),
        "current_ratio":    _safe_float(info, "currentRatio"),
        "free_cash_flow":   _safe_float(info, "freeCashflow"),
        "cash":             _safe_float(info, "totalCash"),
        "beta":             _safe_float(info, "beta"),
        "week52_high":      _safe_float(info, "fiftyTwoWeekHigh") or fi.get("week52_high"),
        "week52_low":       _safe_float(info, "fiftyTwoWeekLow")  or fi.get("week52_low"),
        "analyst_target":   _safe_float(info, "targetMeanPrice"),
        "analyst_low":      _safe_float(info, "targetLowPrice"),
        "analyst_high":     _safe_float(info, "targetHighPrice"),
        "analyst_count":    _safe_float(info, "numberOfAnalystOpinions"),
        "recommendation":   info.get("recommendationKey", ""),
        "earnings_date":    None,
        "dividend_yield":   _safe_float(info, "dividendYield"),
    }

    # Fallback revenue from income statement
    if out["revenue_ttm"] is None:
        try:
            inc = tkr_obj.income_stmt
            if inc is not None and not inc.empty and "Total Revenue" in inc.index:
                out["revenue_ttm"] = float(inc.loc["Total Revenue"].iloc[0])
        except Exception:
            pass

    # Earnings date
    try:
        ts = info.get("earningsTimestamps") or []
        if ts:
            from datetime import datetime
            out["earnings_date"] = datetime.fromtimestamp(ts[0]).strftime("%b %d, %Y")
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # GRANULAR SCORING
    # -----------------------------------------------------------------------
    score = 50
    signals = 0

    # Revenue growth — most differentiating signal
    rg = out.get("revenue_growth")
    if rg is not None:
        signals += 1
        if rg > 0.40:    score += 25
        elif rg > 0.25:  score += 18
        elif rg > 0.15:  score += 12
        elif rg > 0.05:  score += 5
        elif rg > 0:     score += 2
        elif rg > -0.05: score -= 3
        elif rg > -0.15: score -= 10
        else:            score -= 18

    # Gross margin — quality of business
    gm = out.get("gross_margin")
    if gm is not None:
        signals += 1
        if gm > 0.70:    score += 14
        elif gm > 0.50:  score += 10
        elif gm > 0.35:  score += 6
        elif gm > 0.20:  score += 2
        elif gm > 0.10:  score -= 3
        else:            score -= 10

    # Profit margin
    pm = out.get("profit_margin")
    if pm is not None:
        signals += 1
        if pm > 0.25:    score += 12
        elif pm > 0.15:  score += 8
        elif pm > 0.05:  score += 4
        elif pm > 0:     score += 1
        elif pm > -0.10: score -= 5
        else:            score -= 12

    # Forward P/E — valuation
    fpe = out.get("pe_forward")
    if fpe and fpe > 0:
        signals += 1
        if fpe < 12:     score += 12
        elif fpe < 18:   score += 8
        elif fpe < 25:   score += 5
        elif fpe < 35:   score += 2
        elif fpe < 50:   score -= 2
        elif fpe < 80:   score -= 6
        else:            score -= 10

    # PEG ratio
    peg = out.get("peg_ratio")
    if peg and peg > 0:
        signals += 1
        if peg < 0.8:    score += 10
        elif peg < 1.5:  score += 6
        elif peg < 2.5:  score += 2
        elif peg < 4:    score -= 3
        else:            score -= 7

    # Debt/equity
    de = out.get("debt_equity")
    if de is not None:
        signals += 1
        if de < 20:      score += 8
        elif de < 60:    score += 5
        elif de < 120:   score += 2
        elif de < 200:   score -= 3
        else:            score -= 8

    # Free cash flow
    fcf = out.get("free_cash_flow")
    mc  = out.get("market_cap")
    if fcf is not None:
        signals += 1
        if fcf > 0:
            score += 8
            # FCF yield bonus if we have market cap
            if mc and mc > 0:
                fcf_yield = fcf / mc
                if fcf_yield > 0.05:   score += 6
                elif fcf_yield > 0.02: score += 3
        else:
            score -= 8

    # Analyst consensus
    rec = (out.get("recommendation") or "").lower()
    if rec:
        signals += 1
        if "strong_buy" in rec:  score += 12
        elif "buy" in rec:       score += 8
        elif "hold" in rec:      score += 0
        elif "underperform" in rec: score -= 6
        elif "sell" in rec:      score -= 10

    # Analyst target upside
    target = out.get("analyst_target")
    price  = None
    if target:
        try:
            fast = tkr_obj.fast_info
            price = getattr(fast, "last_price", None)
        except Exception:
            pass
        if price and price > 0:
            signals += 1
            upside = (target / price - 1) * 100
            if upside > 30:      score += 10
            elif upside > 15:    score += 6
            elif upside > 5:     score += 3
            elif upside > 0:     score += 1
            elif upside > -10:   score -= 3
            else:                score -= 8

    out["signals_found"] = signals

    # If we have almost no data, return 50 with warning
    if signals < 2:
        out["fundamentals_score"] = 50
        out["data_warning"] = "Insufficient data from Yahoo Finance"
    else:
        out["fundamentals_score"] = max(0, min(100, score))

    return out


# ---------------------------------------------------------------------------
# MOMENTUM
# ---------------------------------------------------------------------------

def compute_momentum(ticker_str, tech, reddit_data=None):
    out   = {}
    score = 50

    # Options chain — call/put ratio
    try:
        tkr   = yf.Ticker(ticker_str)
        dates = tkr.options
        if dates:
            chain    = tkr.option_chain(dates[0])
            call_vol = int(chain.calls["volume"].fillna(0).sum())
            put_vol  = int(chain.puts["volume"].fillna(0).sum())
            cp_ratio = round(call_vol / max(put_vol, 1), 2)
            out["call_volume"]    = call_vol
            out["put_volume"]     = put_vol
            out["call_put_ratio"] = cp_ratio
            if cp_ratio > 3:      score += 18
            elif cp_ratio > 2:    score += 12
            elif cp_ratio > 1.5:  score += 8
            elif cp_ratio > 1.2:  score += 4
            elif cp_ratio < 0.5:  score -= 10
            elif cp_ratio < 0.7:  score -= 6
    except Exception:
        pass

    # Reddit mentions
    if reddit_data:
        mentions = reddit_data.get("mentions", 0)
        out["reddit_mentions"] = mentions
        out["reddit_upvotes"]  = reddit_data.get("avg_upvotes", 0)
        out["reddit_posts"]    = reddit_data.get("sample_posts", [])
        if mentions > 200:    score += 22
        elif mentions > 100:  score += 16
        elif mentions > 50:   score += 10
        elif mentions > 20:   score += 5
        elif mentions > 5:    score += 2
    else:
        out["reddit_mentions"] = 0
        out["reddit_posts"]    = []

    # Volume surge
    vr = tech.get("volume_ratio", 1)
    if vr > 4:      score += 14
    elif vr > 3:    score += 10
    elif vr > 2:    score += 6
    elif vr > 1.5:  score += 3
    elif vr < 0.6:  score -= 4

    # 1-month price momentum
    r1m = tech.get("return_1mo") or 0
    if r1m > 30:    score += 12
    elif r1m > 20:  score += 9
    elif r1m > 12:  score += 6
    elif r1m > 6:   score += 4
    elif r1m > 2:   score += 2
    elif r1m < -25: score -= 12
    elif r1m < -15: score -= 8
    elif r1m < -8:  score -= 4
    elif r1m < -3:  score -= 2

    # Distance from 52-week high
    pct = tech.get("pct_from_52high", -50)
    if pct > -2:     score += 10
    elif pct > -8:   score += 6
    elif pct > -20:  score += 2
    elif pct > -40:  score -= 4
    else:            score -= 8

    # 3-month return adds context
    r3m = tech.get("return_3mo") or 0
    if r3m > 40:    score += 8
    elif r3m > 20:  score += 5
    elif r3m > 10:  score += 2
    elif r3m < -20: score -= 6
    elif r3m < -10: score -= 3

    out["momentum_score"] = max(0, min(100, score))
    return out


# ---------------------------------------------------------------------------
# AI TEXT
# ---------------------------------------------------------------------------

def get_ai_analysis(ticker, tech, fund, mom):
    if ANTHROPIC_AVAILABLE:
        try:
            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            prompt = (
                "You are a stock analyst. Write 2-3 sentences about {} based on: "
                "Price ${}, RSI {}, vs 50-day MA {}%, Revenue growth {}%, "
                "Forward P/E {}, Reddit mentions {}, Call/Put ratio {}, "
                "Analyst recommendation: {}, target ${}. "
                "Be specific and highlight the single most important signal."
            ).format(
                ticker,
                tech.get("price","N/A"), tech.get("rsi","N/A"),
                tech.get("vs_ma50","N/A"),
                round((fund.get("revenue_growth") or 0)*100, 1),
                fund.get("pe_forward","N/A"), mom.get("reddit_mentions",0),
                mom.get("call_put_ratio","N/A"),
                fund.get("recommendation","N/A"), fund.get("analyst_target","N/A"),
            )
            resp = client.messages.create(
                model="claude-haiku-4-5", max_tokens=200,
                messages=[{"role":"user","content":prompt}]
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
    rec    = (fund.get("recommendation") or "hold").replace("_"," ").title()
    upside = round((target/price-1)*100,1) if (target and price) else None
    rsi_s  = "overbought" if rsi>70 else "oversold" if rsi<30 else "neutral"
    trend  = "bullish" if tech.get("golden_cross") else "bearish" if tech.get("death_cross") else "mixed"
    parts  = ["{} showing {} RSI ({}) with {} trend.".format(ticker, rsi_s, rsi, trend)]
    if r1m: parts.append("{:+.1f}% over the past month.".format(r1m))
    if upside and target:
        parts.append("Analysts rate {} with ${} target ({:+.1f}% upside).".format(rec, target, upside))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# OVERALL
# ---------------------------------------------------------------------------

def compute_overall(mom_score, fund_score, tech_score):
    return round(mom_score * 0.35 + fund_score * 0.40 + tech_score * 0.25)


# ---------------------------------------------------------------------------
# SINGLE TICKER
# ---------------------------------------------------------------------------

def analyze_ticker(ticker, reddit_map=None):
    tkr = yf.Ticker(ticker)

    # Fetch info with retry
    info = {}
    for attempt in range(2):
        try:
            info = tkr.info or {}
            if info.get("regularMarketPrice") or info.get("currentPrice") or info.get("longName"):
                break
            time.sleep(1)
        except Exception:
            time.sleep(1)

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

    tech    = compute_technicals(hist)
    fund    = compute_fundamentals(ticker, info, tkr)
    rdata   = (reddit_map or {}).get(ticker)
    mom     = compute_momentum(ticker, tech, rdata)
    setup   = _build_trade_setup(tech, fund)
    blurb   = get_ai_analysis(ticker, tech, fund, mom)
    overall = compute_overall(
        mom["momentum_score"],
        fund["fundamentals_score"],
        tech["technical_score"]
    )
    tag, tag_color = _assign_tag(mom, fund, tech, overall)

    # Backtest + forward test (if enough history)
    backtest_data = {}
    price_targets = {}
    calibrated_tech = tech["technical_score"]
    if BACKTEST_AVAILABLE and hist is not None and len(hist) >= 260:
        try:
            bt_report      = bt.run_full_analysis(ticker, hist, fund, tech, overall)
            backtest_data  = bt_report
            price_targets  = bt_report.get("price_targets", {})
            calibrated_tech = bt_report.get("calibrated_tech_score", calibrated_tech)
            # Recompute overall with calibrated tech score
            overall = compute_overall(
                mom["momentum_score"],
                fund["fundamentals_score"],
                calibrated_tech
            )
            tag, tag_color = _assign_tag(mom, fund, tech, overall)
        except Exception as e:
            backtest_data = {"error": str(e)}

    return {
        "ticker":    ticker,
        "name":      fund.get("company", ticker),
        "sector":    fund.get("sector", ""),
        "price":     tech.get("price"),
        "change_1d": tech.get("return_5d"),
        "scores": {
            "momentum":     mom["momentum_score"],
            "fundamentals": fund["fundamentals_score"],
            "technical":    calibrated_tech,
            "overall":      overall,
        },
        "tag":       tag,
        "tagColor":  tag_color,
        "blurb":     blurb,
        "tech":      tech,
        "fund":      fund,
        "mom":       mom,
        "setup":     setup,
        "backtest":  backtest_data,
        "price_targets": price_targets,
        "updatedAt": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# TOP PICKS SCAN
# ---------------------------------------------------------------------------

def run_top_picks_scan(include_reddit=False):
    reddit_map    = {}
    extra_tickers = []

    if include_reddit:
        try:
            reddit_tickers = get_reddit_trending(limit_per_sub=50, top_n=15)
            reddit_map     = {r["ticker"]: r for r in reddit_tickers}
            extra_tickers  = [r["ticker"] for r in reddit_tickers]
        except Exception as e:
            print("Reddit skipped: {}".format(e))

    all_tickers = list(dict.fromkeys(BASE_WATCHLIST + extra_tickers))[:20]

    results = []
    for ticker in all_tickers:
        try:
            data = analyze_ticker(ticker, reddit_map)
            results.append(data)
            time.sleep(0.5)
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
    risk       = abs(price - stop)
    reward     = abs(t1 - price)
    rr         = "{}:1".format(round(reward/risk,1)) if risk>0 else "N/A"

    if rsi > 75:    bias = "WAIT FOR PULLBACK"
    elif rsi < 30:  bias = "OVERSOLD — WATCH FOR REVERSAL"
    elif tech.get("golden_cross") and tech.get("macd_bullish"): bias = "BULLISH"
    elif tech.get("death_cross"):  bias = "BEARISH — CAUTION"
    else:           bias = "NEUTRAL"

    return {
        "bias":  bias,
        "entry": "${}-${}".format(entry_low, entry_high),
        "stop":  "<${}".format(stop),
        "t1":    "${}".format(t1),
        "t2":    "${}".format(t2),
        "rr":    rr,
    }


def _assign_tag(mom, fund, tech, overall):
    rsi = tech.get("rsi", 50)
    r1m = tech.get("return_1mo") or 0
    rg  = fund.get("revenue_growth") or 0
    cp  = mom.get("call_put_ratio") or 1

    if rsi > 70 and r1m > 15:  return "MOMENTUM",    "#00e87a"
    if cp > 2.5:               return "OPTIONS FLOW", "#f5c518"
    if rg > 0.40:              return "HIGH GROWTH",  "#38b6ff"
    if overall >= 75:          return "STRONG BUY",   "#00e87a"
    if overall >= 60:          return "BULLISH",      "#f5c518"
    if overall < 40:           return "HIGH RISK",    "#ff8c42"
    return "NEUTRAL", "#4a6380"
