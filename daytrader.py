"""
daytrader.py - Day Trading Signal Engine for SIGNAL

Differences from swing/position trading (scanner.py + backtest.py):
  - Uses intraday + short-term data (1m, 5m, 15m, 1h, 1d timeframes)
  - VWAP (Volume Weighted Average Price) as primary reference
  - Intraday momentum: opening range breakout, pre-market gap, first 30min range
  - Short-term RSI (2-period, 5-period) not 14-period
  - ATR-based intraday targets (tighter: 0.5x, 1x, 1.5x ATR)
  - Volume profile: relative volume vs average for time of day
  - Pre-market and after-hours activity
  - Float / short float considerations
  - Catalyst check: earnings today, news today, FDA events
  - Backtest: replay signals on daily data using same-day exit rules
"""

import math
import time
import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# INTRADAY DATA FETCH
# ---------------------------------------------------------------------------

def fetch_intraday(ticker, interval="5m", period="5d"):
    """Fetch intraday OHLCV. Returns DataFrame or None."""
    try:
        tkr  = yf.Ticker(ticker)
        hist = tkr.history(period=period, interval=interval)
        if hist is not None and len(hist) > 10:
            return hist
    except Exception:
        pass
    return None


def fetch_daily(ticker, period="1y"):
    """Fetch daily OHLCV for short-term technical analysis."""
    try:
        tkr  = yf.Ticker(ticker)
        hist = tkr.history(period=period)
        if hist is not None and len(hist) > 20:
            return hist
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# VWAP CALCULATION
# ---------------------------------------------------------------------------

def compute_vwap(hist):
    """
    Compute VWAP from intraday OHLCV data.
    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    """
    if hist is None or len(hist) < 2:
        return None

    typical = (hist["High"] + hist["Low"] + hist["Close"]) / 3
    volume  = hist["Volume"].fillna(0)

    # Only use today's data for VWAP (reset at market open)
    today = hist.index.normalize()
    if hasattr(today, 'date'):
        try:
            last_date = hist.index[-1].date()
            today_mask = hist.index.date == last_date
            tp_today  = typical[today_mask]
            vol_today = volume[today_mask]
            if len(tp_today) > 0 and vol_today.sum() > 0:
                vwap = (tp_today * vol_today).cumsum() / vol_today.cumsum()
                return float(vwap.iloc[-1])
        except Exception:
            pass

    # Fallback: full period VWAP
    if volume.sum() > 0:
        return float((typical * volume).cumsum().iloc[-1] / volume.cumsum().iloc[-1])
    return None


# ---------------------------------------------------------------------------
# DAY TRADING SIGNALS
# ---------------------------------------------------------------------------

def compute_daytrading_signals(ticker, intraday_5m, daily):
    """
    Compute all day-trading specific signals.
    Returns dict of signals and their values.
    """
    signals = {}

    # ── VWAP ─────────────────────────────────────────────────────────────────
    vwap = compute_vwap(intraday_5m)
    signals["vwap"] = round(vwap, 2) if vwap else None

    current_price = None
    if intraday_5m is not None and len(intraday_5m) > 0:
        current_price = float(intraday_5m["Close"].iloc[-1])
    elif daily is not None and len(daily) > 0:
        current_price = float(daily["Close"].iloc[-1])

    signals["current_price"] = round(current_price, 2) if current_price else None

    if vwap and current_price:
        signals["vs_vwap"]      = round((current_price / vwap - 1) * 100, 2)
        signals["above_vwap"]   = bool(current_price > vwap)

    # ── Short-term RSI (2, 5, 9 period) ──────────────────────────────────────
    if daily is not None and len(daily) >= 10:
        close = daily["Close"].dropna()
        for period in [2, 5, 9]:
            try:
                delta = close.diff()
                gain  = delta.clip(lower=0).rolling(period).mean()
                loss  = (-delta.clip(upper=0)).rolling(period).mean()
                rs    = gain / loss.replace(0, np.nan)
                rsi   = 100 - (100 / (1 + rs))
                val   = float(rsi.iloc[-1])
                if not math.isnan(val):
                    signals["rsi_" + str(period)] = round(val, 1)
            except Exception:
                pass

    # ── Intraday ATR (14-period on 5m data) ───────────────────────────────────
    if intraday_5m is not None and len(intraday_5m) >= 15:
        high   = intraday_5m["High"]
        low    = intraday_5m["Low"]
        close  = intraday_5m["Close"]
        try:
            tr  = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low  - close.shift(1)).abs()
            ], axis=1).max(axis=1)
            atr_intraday = float(tr.rolling(14).mean().iloc[-1])
            signals["atr_intraday"] = round(atr_intraday, 3)
        except Exception:
            pass

    # ── Daily ATR ─────────────────────────────────────────────────────────────
    if daily is not None and len(daily) >= 15:
        high   = daily["High"]
        low    = daily["Low"]
        close  = daily["Close"]
        try:
            tr  = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low  - close.shift(1)).abs()
            ], axis=1).max(axis=1)
            atr_daily = float(tr.rolling(14).mean().iloc[-1])
            signals["atr_daily"] = round(atr_daily, 2)
        except Exception:
            pass

    # ── Opening Range Breakout ────────────────────────────────────────────────
    if intraday_5m is not None:
        try:
            today_data = _get_todays_data(intraday_5m)
            if today_data is not None and len(today_data) >= 6:
                # First 30 mins = first 6 x 5min candles
                orb_data     = today_data.iloc[:6]
                orb_high     = float(orb_data["High"].max())
                orb_low      = float(orb_data["Low"].min())
                signals["orb_high"]   = round(orb_high, 2)
                signals["orb_low"]    = round(orb_low, 2)
                signals["orb_range"]  = round(orb_high - orb_low, 2)
                if current_price:
                    signals["orb_breakout_up"]   = bool(current_price > orb_high)
                    signals["orb_breakdown"]      = bool(current_price < orb_low)
                    signals["orb_pct_above_high"] = round((current_price / orb_high - 1) * 100, 2) if current_price > orb_high else 0
        except Exception:
            pass

    # ── Pre-market gap ────────────────────────────────────────────────────────
    if daily is not None and len(daily) >= 2:
        try:
            prev_close = float(daily["Close"].iloc[-2])
            today_open = float(daily["Open"].iloc[-1])
            gap_pct    = (today_open / prev_close - 1) * 100
            signals["gap_pct"]    = round(gap_pct, 2)
            signals["gap_up"]     = bool(gap_pct > 1)
            signals["gap_down"]   = bool(gap_pct < -1)
        except Exception:
            pass

    # ── Relative volume (today vs 20-day avg) ────────────────────────────────
    if daily is not None and len(daily) >= 21:
        try:
            avg_vol      = float(daily["Volume"].iloc[-21:-1].mean())
            today_vol    = float(daily["Volume"].iloc[-1])
            signals["relative_volume"] = round(today_vol / avg_vol, 2) if avg_vol > 0 else 1.0
            signals["avg_volume"]      = round(avg_vol, 0)
            signals["today_volume"]    = round(today_vol, 0)
        except Exception:
            pass

    # ── Short-term momentum (5-day, 10-day EMA crossover) ────────────────────
    if daily is not None and len(daily) >= 11:
        close = daily["Close"].dropna()
        try:
            ema5  = float(close.ewm(span=5,  adjust=False).mean().iloc[-1])
            ema10 = float(close.ewm(span=10, adjust=False).mean().iloc[-1])
            ema5_prev  = float(close.ewm(span=5,  adjust=False).mean().iloc[-2])
            ema10_prev = float(close.ewm(span=10, adjust=False).mean().iloc[-2])
            signals["ema5"]          = round(ema5, 2)
            signals["ema10"]         = round(ema10, 2)
            signals["ema5_above_10"] = bool(ema5 > ema10)
            signals["ema_crossover"] = bool(ema5 > ema10 and ema5_prev <= ema10_prev)
        except Exception:
            pass

    # ── Intraday trend (5m candles: last 12 candles = 1 hour trend) ──────────
    if intraday_5m is not None and len(intraday_5m) >= 12:
        try:
            last_hour = intraday_5m["Close"].iloc[-12:]
            lh_start  = float(last_hour.iloc[0])
            lh_end    = float(last_hour.iloc[-1])
            signals["1h_trend_pct"] = round((lh_end / lh_start - 1) * 100, 2)
            signals["1h_trending_up"] = bool(lh_end > lh_start)
        except Exception:
            pass

    # ── Candle pattern (last 5m candle) ──────────────────────────────────────
    if intraday_5m is not None and len(intraday_5m) >= 2:
        try:
            last  = intraday_5m.iloc[-1]
            prev  = intraday_5m.iloc[-2]
            body  = abs(float(last["Close"]) - float(last["Open"]))
            range_= float(last["High"]) - float(last["Low"])
            if range_ > 0:
                signals["last_candle_body_pct"] = round(body / range_ * 100, 1)
                signals["last_candle_bullish"]  = bool(float(last["Close"]) > float(last["Open"]))
                signals["last_candle_doji"]     = bool(body / range_ < 0.1)
        except Exception:
            pass

    return signals


def _get_todays_data(hist):
    """Filter intraday dataframe to today's candles only."""
    if hist is None or len(hist) == 0:
        return None
    try:
        last_date = hist.index[-1].date()
        mask = hist.index.date == last_date
        today = hist[mask]
        return today if len(today) > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DAY TRADING SCORE
# ---------------------------------------------------------------------------

def compute_daytrading_score(signals, info):
    """
    Score a stock for day trading on a 0-100 scale.
    Very different weighting from swing trading:
      - VWAP position = 25 pts
      - Intraday momentum = 20 pts
      - Relative volume = 20 pts
      - Short RSI = 15 pts
      - ORB = 10 pts
      - Gap = 10 pts
    """
    score = 50

    # VWAP (25 pts max)
    vs_vwap = signals.get("vs_vwap")
    if vs_vwap is not None:
        if vs_vwap > 2:       score += 20
        elif vs_vwap > 1:     score += 15
        elif vs_vwap > 0.3:   score += 10
        elif vs_vwap > 0:     score += 5
        elif vs_vwap > -0.3:  score -= 5
        elif vs_vwap > -1:    score -= 12
        else:                 score -= 20

    # Relative volume (20 pts max) — CRITICAL for day trading
    rvol = signals.get("relative_volume", 1)
    if rvol > 5:       score += 20
    elif rvol > 3:     score += 15
    elif rvol > 2:     score += 10
    elif rvol > 1.5:   score += 5
    elif rvol < 0.7:   score -= 8   # low volume = avoid

    # Short RSI-2 (15 pts) — mean reversion signal for day trading
    rsi2 = signals.get("rsi_2")
    if rsi2 is not None:
        if rsi2 < 10:      score += 15   # extreme oversold = long setup
        elif rsi2 < 20:    score += 10
        elif rsi2 < 30:    score += 5
        elif rsi2 > 90:    score -= 10   # extreme overbought = short setup
        elif rsi2 > 75:    score -= 5

    # RSI-5 confirmation
    rsi5 = signals.get("rsi_5")
    if rsi5 is not None:
        if rsi5 < 25:      score += 8
        elif rsi5 < 40:    score += 4
        elif rsi5 > 75:    score -= 6

    # Opening Range Breakout (10 pts)
    if signals.get("orb_breakout_up"):
        score += 12
        pct = signals.get("orb_pct_above_high", 0)
        if pct > 1:   score += 5   # strong breakout
    if signals.get("orb_breakdown"):
        score -= 8

    # Gap up (10 pts) — gaps often continue intraday
    gap = signals.get("gap_pct", 0)
    if gap > 5:        score += 12
    elif gap > 2:      score += 8
    elif gap > 1:      score += 4
    elif gap < -5:     score -= 10
    elif gap < -2:     score -= 6

    # EMA crossover (short-term trend)
    if signals.get("ema_crossover"):    score += 8
    if signals.get("ema5_above_10"):    score += 4

    # 1-hour intraday trend
    trend1h = signals.get("1h_trend_pct", 0)
    if trend1h > 1:    score += 6
    elif trend1h > 0:  score += 3
    elif trend1h < -1: score -= 6
    elif trend1h < 0:  score -= 2

    # Last candle bullish
    if signals.get("last_candle_bullish"):  score += 3
    if signals.get("last_candle_doji"):     score -= 2   # indecision

    # Penalise low float — harder to trade
    shares = info.get("sharesOutstanding")
    float_val = info.get("floatShares")
    if float_val:
        if float_val < 5_000_000:    score += 10   # low float = more volatile = day traders love it
        elif float_val < 20_000_000: score += 5
        elif float_val > 5e9:        score -= 5    # mega float = slower moves

    return max(0, min(100, round(score)))


# ---------------------------------------------------------------------------
# INTRADAY PRICE TARGETS
# ---------------------------------------------------------------------------

def compute_intraday_targets(signals, current_price):
    """
    ATR-based intraday targets.
    Day traders use much tighter targets than swing traders.
    """
    if not current_price:
        return {}

    atr = signals.get("atr_intraday") or (signals.get("atr_daily", 0) / 5)
    if not atr or atr == 0:
        atr = current_price * 0.005   # 0.5% fallback

    vwap = signals.get("vwap")
    orb_high = signals.get("orb_high")
    orb_low  = signals.get("orb_low")

    targets = {
        "atr_intraday": round(atr, 3),
        # Long targets
        "long_t1":  round(current_price + 0.5 * atr, 2),
        "long_t2":  round(current_price + 1.0 * atr, 2),
        "long_t3":  round(current_price + 2.0 * atr, 2),
        # Long stops
        "long_stop_tight":  round(current_price - 0.5 * atr, 2),
        "long_stop_normal": round(current_price - 1.0 * atr, 2),
        # Short targets
        "short_t1": round(current_price - 0.5 * atr, 2),
        "short_t2": round(current_price - 1.0 * atr, 2),
        # Risk/reward
        "rr_t1": "1:1",
        "rr_t2": "2:1",
        "rr_t3": "4:1",
    }

    if vwap:
        targets["vwap"] = round(vwap, 2)
        targets["long_entry_vwap_bounce"] = round(vwap * 1.001, 2)
        targets["long_stop_vwap"]         = round(vwap * 0.998, 2)

    if orb_high:
        targets["orb_breakout_entry"] = round(orb_high * 1.001, 2)
        targets["orb_stop"]           = round(orb_low  * 0.999, 2) if orb_low else round(orb_high - atr, 2)
        targets["orb_target"]         = round(orb_high + (orb_high - (orb_low or orb_high - atr)), 2)

    return targets


# ---------------------------------------------------------------------------
# DAY TRADING BACKTEST  (daily candles, same-day exit logic)
# ---------------------------------------------------------------------------

def backtest_daytrading_signals(daily):
    """
    Backtest intraday signals using daily OHLCV data.

    Since we don't have true intraday history, we simulate:
      - Entry: open price when signal triggers
      - Exit: end-of-day close (or stop if daily low hits stop)
      - Signals tested: gap up, ORB approximation, VWAP cross (approx)

    Returns per-signal stats.
    """
    if daily is None or len(daily) < 60:
        return {}

    close  = daily["Close"].dropna().reset_index(drop=True)
    open_  = daily["Open"].dropna().reset_index(drop=True)
    high   = daily["High"].dropna().reset_index(drop=True)
    low    = daily["Low"].dropna().reset_index(drop=True)
    volume = daily["Volume"].fillna(0).reset_index(drop=True)
    n      = len(close)

    results = {}

    # Signal 1: Gap Up > 2% → buy open, sell close
    gap_trades = []
    for i in range(1, n):
        prev_c = float(close.iloc[i-1])
        this_o = float(open_.iloc[i])
        gap    = (this_o / prev_c - 1) * 100
        if gap > 2:
            entry  = this_o
            exit_  = float(close.iloc[i])
            stop   = this_o * 0.98
            # Check if stop was hit (daily low hit stop)
            if float(low.iloc[i]) <= stop:
                ret = (stop / entry - 1) * 100
            else:
                ret = (exit_ / entry - 1) * 100
            gap_trades.append(ret)

    if gap_trades:
        results["gap_up_2pct"] = _summarise_trades(gap_trades, "Gap Up >2%")

    # Signal 2: Gap Down > 2% → fade the gap (sell open, cover at close)
    gap_down_trades = []
    for i in range(1, n):
        prev_c = float(close.iloc[i-1])
        this_o = float(open_.iloc[i])
        gap    = (this_o / prev_c - 1) * 100
        if gap < -2:
            entry = this_o
            exit_ = float(close.iloc[i])
            # Short: profit if price falls further
            ret   = (entry / exit_ - 1) * 100
            gap_down_trades.append(ret)

    if gap_down_trades:
        results["gap_fade_short"] = _summarise_trades(gap_down_trades, "Gap Fade Short")

    # Signal 3: High relative volume day (>2x) → buy open, sell close
    rvol_trades = []
    for i in range(20, n):
        avg_vol  = float(volume.iloc[i-20:i].mean())
        this_vol = float(volume.iloc[i])
        if avg_vol > 0 and this_vol / avg_vol > 2:
            entry = float(open_.iloc[i])
            exit_ = float(close.iloc[i])
            stop  = entry * 0.98
            if float(low.iloc[i]) <= stop:
                ret = (stop / entry - 1) * 100
            else:
                ret = (exit_ / entry - 1) * 100
            rvol_trades.append(ret)

    if rvol_trades:
        results["high_rvol_day"] = _summarise_trades(rvol_trades, "High Rel. Volume Day")

    # Signal 4: RSI-2 oversold (<15) → buy open next day, sell close
    rsi2_long_trades = []
    for i in range(5, n - 1):
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(2).mean()
        loss  = (-delta.clip(upper=0)).rolling(2).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi2  = (100 - (100 / (1 + rs))).iloc[i]
        if not math.isnan(rsi2) and rsi2 < 15:
            entry = float(open_.iloc[i+1])
            exit_ = float(close.iloc[i+1])
            stop  = entry * 0.985
            if float(low.iloc[i+1]) <= stop:
                ret = (stop / entry - 1) * 100
            else:
                ret = (exit_ / entry - 1) * 100
            rsi2_long_trades.append(ret)

    if rsi2_long_trades:
        results["rsi2_oversold_long"] = _summarise_trades(rsi2_long_trades, "RSI-2 Oversold Long")

    # Signal 5: RSI-2 overbought (>85) → short open next day, cover close
    rsi2_short_trades = []
    for i in range(5, n - 1):
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(2).mean()
        loss  = (-delta.clip(upper=0)).rolling(2).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi2  = (100 - (100 / (1 + rs))).iloc[i]
        if not math.isnan(rsi2) and rsi2 > 85:
            entry = float(open_.iloc[i+1])
            exit_ = float(close.iloc[i+1])
            # Short: profit if price falls
            ret   = (entry / exit_ - 1) * 100
            rsi2_short_trades.append(ret)

    if rsi2_short_trades:
        results["rsi2_overbought_short"] = _summarise_trades(rsi2_short_trades, "RSI-2 Overbought Short")

    # Signal 6: EMA 5 crosses above EMA 10 → buy open, sell after 1 day
    ema_trades = []
    ema5  = close.ewm(span=5,  adjust=False).mean()
    ema10 = close.ewm(span=10, adjust=False).mean()
    for i in range(11, n - 1):
        if ema5.iloc[i] > ema10.iloc[i] and ema5.iloc[i-1] <= ema10.iloc[i-1]:
            entry = float(open_.iloc[i+1])
            exit_ = float(close.iloc[i+1])
            stop  = entry * 0.985
            if float(low.iloc[i+1]) <= stop:
                ret = (stop / entry - 1) * 100
            else:
                ret = (exit_ / entry - 1) * 100
            ema_trades.append(ret)

    if ema_trades:
        results["ema5_cross_ema10"] = _summarise_trades(ema_trades, "EMA5 x EMA10 Long")

    return results


def _summarise_trades(returns, label):
    if not returns:
        return {}
    arr = np.array(returns)
    wins = sum(1 for r in returns if r > 0)
    return {
        "label":          label,
        "total_trades":   len(returns),
        "win_rate":       round(wins / len(returns) * 100, 1),
        "avg_return":     round(float(np.mean(arr)), 2),
        "median_return":  round(float(np.median(arr)), 2),
        "best_trade":     round(float(np.max(arr)), 2),
        "worst_trade":    round(float(np.min(arr)), 2),
        "profit_factor":  round(float(arr[arr>0].sum() / abs(arr[arr<0].sum())), 2) if arr[arr<0].sum() != 0 else 99.0,
        "accuracy_score": round(wins / len(returns) * 60 + min(max((float(np.mean(arr)) + 1) / 4, 0), 1) * 40, 1),
    }


# ---------------------------------------------------------------------------
# FULL DAY TRADING ANALYSIS
# ---------------------------------------------------------------------------

def analyze_daytrading(ticker):
    """
    Full day trading analysis for one ticker.
    Returns dict ready for the API.
    """
    start = time.time()

    # Fetch data
    intraday_5m = fetch_intraday(ticker, interval="5m",  period="5d")
    intraday_1m = fetch_intraday(ticker, interval="1m",  period="1d")
    daily       = fetch_daily(ticker, period="1y")

    info = {}
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        pass

    # Signals
    signals = compute_daytrading_signals(ticker, intraday_5m, daily)

    # Score
    dt_score = compute_daytrading_score(signals, info)

    # Targets
    price   = signals.get("current_price")
    targets = compute_intraday_targets(signals, price)

    # Backtest
    bt_results = {}
    try:
        bt_results = backtest_daytrading_signals(daily)
    except Exception as e:
        bt_results = {"error": str(e)}

    # Best/worst setups from backtest
    bt_list = [(k, v) for k, v in bt_results.items() if isinstance(v, dict) and "accuracy_score" in v]
    best_setup  = max(bt_list, key=lambda x: x[1]["accuracy_score"]) if bt_list else None
    worst_setup = min(bt_list, key=lambda x: x[1]["accuracy_score"]) if bt_list else None

    # Market session
    import datetime
    now_utc  = datetime.datetime.utcnow()
    now_et   = now_utc - datetime.timedelta(hours=4)  # rough EST
    market_open  = now_et.replace(hour=9,  minute=30, second=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0)
    if market_open <= now_et <= market_close and now_et.weekday() < 5:
        session = "MARKET OPEN"
    elif now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
        session = "PRE-MARKET"
    else:
        session = "AFTER HOURS"

    # Trading bias
    vs_vwap  = signals.get("vs_vwap", 0) or 0
    rvol     = signals.get("relative_volume", 1) or 1
    rsi2     = signals.get("rsi_2", 50) or 50
    gap      = signals.get("gap_pct", 0) or 0
    orb_bull = signals.get("orb_breakout_up", False)
    orb_bear = signals.get("orb_breakdown", False)

    if dt_score >= 70 and vs_vwap > 0 and rvol > 1.5:
        bias = "LONG BIAS"
        bias_color = "#00e87a"
    elif dt_score <= 35 or (vs_vwap < -1 and rvol > 1.5):
        bias = "SHORT BIAS"
        bias_color = "#ff4560"
    elif orb_bull:
        bias = "ORB LONG"
        bias_color = "#00e87a"
    elif orb_bear:
        bias = "ORB SHORT"
        bias_color = "#ff4560"
    elif rsi2 < 15:
        bias = "OVERSOLD — LONG SETUP"
        bias_color = "#38b6ff"
    elif rsi2 > 85:
        bias = "OVERBOUGHT — SHORT SETUP"
        bias_color = "#ff8c42"
    else:
        bias = "WAIT FOR SETUP"
        bias_color = "#f5c518"

    elapsed = round(time.time() - start, 2)

    return {
        "ticker":           ticker,
        "session":          session,
        "daytrading_score": dt_score,
        "bias":             bias,
        "bias_color":       bias_color,
        "signals":          signals,
        "targets":          targets,
        "backtest":         bt_results,
        "best_setup":       {best_setup[0]: best_setup[1]}  if best_setup  else None,
        "worst_setup":      {worst_setup[0]: worst_setup[1]} if worst_setup else None,
        "company":          info.get("longName") or info.get("shortName", ticker),
        "float_shares":     info.get("floatShares"),
        "short_float":      info.get("shortPercentOfFloat"),
        "avg_volume":       info.get("averageVolume"),
        "computation_s":    elapsed,
    }
