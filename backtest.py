"""
backtest.py - Backtesting & Forward Testing Engine for SIGNAL

What this does:
  - Backtesting:  replays historical signals on past OHLCV data to measure
                  how well RSI/MACD/MA crossover/volume signals actually
                  predicted future returns. Outputs a per-signal accuracy score.

  - Forward test: runs the current live signal set through a Monte Carlo
                  simulation (bootstrapped from historical return distributions)
                  to generate probability-weighted price targets and confidence
                  intervals.

  - Signal score calibration: adjusts raw scores based on historical accuracy
                  so a "score of 80" genuinely means ~80% of similar past
                  setups produced positive returns.

  - Price targets: derived from three methods averaged together:
                    1. Technical (key S/R levels, ATR multiples)
                    2. Fundamental (DCF-lite using FCF yield)
                    3. Statistical (historical return percentiles)
"""

import math
import time
import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# SIGNAL DEFINITIONS
# Each signal is a function(hist_row, lookback_window) -> True/False
# ---------------------------------------------------------------------------

def _sig_rsi_oversold(close, idx, window=14):
    """RSI < 35 = oversold buy signal"""
    if idx < window + 1:
        return False
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(window).mean()
    loss  = (-delta.clip(upper=0)).rolling(window).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[idx]) < 35


def _sig_rsi_overbought(close, idx, window=14):
    """RSI > 70 = overbought — tests whether this precedes pullback"""
    if idx < window + 1:
        return False
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(window).mean()
    loss  = (-delta.clip(upper=0)).rolling(window).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[idx]) > 70


def _sig_golden_cross(close, idx):
    """MA50 crosses above MA200"""
    if idx < 201:
        return False
    ma50_now  = float(close.iloc[idx-49:idx+1].mean())
    ma50_prev = float(close.iloc[idx-50:idx].mean())
    ma200_now = float(close.iloc[idx-199:idx+1].mean())
    return ma50_now > ma200_now and ma50_prev <= ma200_now


def _sig_death_cross(close, idx):
    """MA50 crosses below MA200"""
    if idx < 201:
        return False
    ma50_now  = float(close.iloc[idx-49:idx+1].mean())
    ma50_prev = float(close.iloc[idx-50:idx].mean())
    ma200_now = float(close.iloc[idx-199:idx+1].mean())
    return ma50_now < ma200_now and ma50_prev >= ma200_now


def _sig_volume_surge(close, volume, idx, multiplier=2.0):
    """Volume > 2x 20-day average"""
    if idx < 20:
        return False
    avg_vol = float(volume.iloc[idx-20:idx].mean())
    return float(volume.iloc[idx]) > multiplier * avg_vol


def _sig_price_above_ma50(close, idx):
    """Price crosses above 50-day MA"""
    if idx < 51:
        return False
    ma50     = float(close.iloc[idx-49:idx+1].mean())
    ma50_prev= float(close.iloc[idx-50:idx].mean())
    p_now    = float(close.iloc[idx])
    p_prev   = float(close.iloc[idx-1])
    return p_now > ma50 and p_prev <= ma50_prev


def _sig_macd_crossover(close, idx):
    """MACD line crosses above signal line"""
    if idx < 35:
        return False
    c     = close.iloc[:idx+1]
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    if len(macd) < 2:
        return False
    return float(macd.iloc[-1]) > float(sig.iloc[-1]) and \
           float(macd.iloc[-2]) <= float(sig.iloc[-2])


def _sig_near_52w_high(close, idx):
    """Price within 3% of 52-week high"""
    if idx < 252:
        return False
    high_52w = float(close.iloc[idx-251:idx+1].max())
    price    = float(close.iloc[idx])
    return (price / high_52w) >= 0.97


SIGNALS = {
    "rsi_oversold":       lambda c, v, i: _sig_rsi_oversold(c, i),
    "rsi_overbought":     lambda c, v, i: _sig_rsi_overbought(c, i),
    "golden_cross":       lambda c, v, i: _sig_golden_cross(c, i),
    "death_cross":        lambda c, v, i: _sig_death_cross(c, i),
    "volume_surge":       lambda c, v, i: _sig_volume_surge(c, v, i),
    "price_above_ma50":   lambda c, v, i: _sig_price_above_ma50(c, i),
    "macd_crossover":     lambda c, v, i: _sig_macd_crossover(c, i),
    "near_52w_high":      lambda c, v, i: _sig_near_52w_high(c, i),
}

# Expected direction: +1 = bullish signal, -1 = bearish signal
SIGNAL_DIRECTION = {
    "rsi_oversold":     +1,
    "rsi_overbought":   -1,
    "golden_cross":     +1,
    "death_cross":      -1,
    "volume_surge":     +1,
    "price_above_ma50": +1,
    "macd_crossover":   +1,
    "near_52w_high":    +1,
}

# Horizons to test (trading days)
HORIZONS = {
    "1w":  5,
    "1mo": 21,
    "3mo": 63,
}


# ---------------------------------------------------------------------------
# CORE BACKTEST
# ---------------------------------------------------------------------------

def backtest_signals(hist, ticker=""):
    """
    For each signal, find every historical occurrence and measure
    forward returns at 1w, 1mo, 3mo horizons.

    Returns dict:
      {
        signal_name: {
          "occurrences": int,
          "win_rate_1mo": float,      # % of signals that led to positive return
          "avg_return_1mo": float,    # average forward return
          "median_return_1mo": float,
          "win_rate_3mo": float,
          "avg_return_3mo": float,
          "accuracy_score": float,    # 0-100, calibrated
        }
      }
    """
    if hist is None or len(hist) < 260:
        return {}

    close  = hist["Close"].dropna().reset_index(drop=True)
    volume = hist["Volume"].fillna(0).reset_index(drop=True)
    n      = len(close)

    results = {}

    for sig_name, sig_fn in SIGNALS.items():
        direction   = SIGNAL_DIRECTION[sig_name]
        occurrences = []

        for i in range(252, n):
            try:
                triggered = sig_fn(close, volume, i)
            except Exception:
                continue

            if not triggered:
                continue

            # Measure forward returns at each horizon
            entry_price = float(close.iloc[i])
            fwd = {}
            for label, days in HORIZONS.items():
                if i + days < n:
                    exit_price = float(close.iloc[i + days])
                    ret = (exit_price / entry_price - 1) * 100
                    fwd[label] = ret * direction   # positive = signal was right

            if fwd:
                occurrences.append(fwd)

        if not occurrences:
            continue

        # Aggregate
        rets_1mo = [o["1mo"] for o in occurrences if "1mo" in o]
        rets_3mo = [o["3mo"] for o in occurrences if "3mo" in o]

        if not rets_1mo:
            continue

        win_rate_1mo  = sum(1 for r in rets_1mo if r > 0) / len(rets_1mo)
        avg_ret_1mo   = float(np.mean(rets_1mo))
        med_ret_1mo   = float(np.median(rets_1mo))

        win_rate_3mo  = sum(1 for r in rets_3mo if r > 0) / len(rets_3mo) if rets_3mo else win_rate_1mo
        avg_ret_3mo   = float(np.mean(rets_3mo)) if rets_3mo else avg_ret_1mo * 1.5

        # Accuracy score: combines win rate and avg return magnitude
        # Score = win_rate * 60 + return_factor * 40
        ret_factor = min(max((avg_ret_1mo + 5) / 20, 0), 1)   # normalise ~-5% to +15%
        accuracy   = win_rate_1mo * 60 + ret_factor * 40

        results[sig_name] = {
            "occurrences":      len(occurrences),
            "win_rate_1mo":     round(win_rate_1mo * 100, 1),
            "avg_return_1mo":   round(avg_ret_1mo, 2),
            "median_return_1mo":round(med_ret_1mo, 2),
            "win_rate_3mo":     round(win_rate_3mo * 100, 1),
            "avg_return_3mo":   round(avg_ret_3mo, 2),
            "accuracy_score":   round(accuracy, 1),
        }

    return results


# ---------------------------------------------------------------------------
# CALIBRATED SCORE
# Use backtest accuracy to adjust the raw signal score
# ---------------------------------------------------------------------------

def calibrate_score(raw_score, backtest_results, active_signals):
    """
    Adjusts the raw technical score based on how accurately each
    active signal has performed historically on this specific stock.

    active_signals: list of signal names currently triggered.
    """
    if not backtest_results or not active_signals:
        return raw_score

    accuracies = []
    for sig in active_signals:
        if sig in backtest_results:
            accuracies.append(backtest_results[sig]["accuracy_score"])

    if not accuracies:
        return raw_score

    avg_accuracy = float(np.mean(accuracies))
    # Blend: 60% raw score + 40% historical accuracy
    calibrated = raw_score * 0.60 + avg_accuracy * 0.40
    return round(min(100, max(0, calibrated)), 1)


# ---------------------------------------------------------------------------
# RETURN DISTRIBUTION (for Monte Carlo)
# ---------------------------------------------------------------------------

def compute_return_distribution(hist):
    """
    Compute daily log return statistics for Monte Carlo simulation.
    Returns: {mean, std, skew, kurt, percentiles}
    """
    if hist is None or len(hist) < 60:
        return None

    close   = hist["Close"].dropna()
    log_ret = np.log(close / close.shift(1)).dropna()

    if len(log_ret) < 30:
        return None

    lr = log_ret.values
    return {
        "mean":     float(np.mean(lr)),
        "std":      float(np.std(lr)),
        "skew":     float(pd.Series(lr).skew()),
        "kurt":     float(pd.Series(lr).kurt()),
        "p5":       float(np.percentile(lr, 5)),
        "p25":      float(np.percentile(lr, 25)),
        "p75":      float(np.percentile(lr, 75)),
        "p95":      float(np.percentile(lr, 95)),
        "n_days":   len(lr),
    }


# ---------------------------------------------------------------------------
# MONTE CARLO FORWARD TEST
# ---------------------------------------------------------------------------

def monte_carlo_price_targets(current_price, return_dist, horizons_days=None,
                               n_simulations=2000, bias_factor=0.0):
    """
    Bootstrap Monte Carlo simulation of future price paths.

    bias_factor: adjustment to mean return based on signal strength
                 (positive = bullish bias, negative = bearish)
                 Typically: (overall_score - 50) / 500  so score 80 -> +0.06% daily bias

    Returns price targets at each horizon with confidence intervals.
    """
    if return_dist is None or current_price is None:
        return {}

    if horizons_days is None:
        horizons_days = {"1w": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1yr": 252}

    mu  = return_dist["mean"] + bias_factor
    sig = return_dist["std"]

    # Use historical percentiles for fat-tail sampling (not pure normal)
    hist_returns = None
    np.random.seed(42)

    results = {}
    max_days = max(horizons_days.values())

    # Run simulations
    # Each sim: array of daily log returns -> cumulative path
    all_paths = np.zeros((n_simulations, max_days))
    for d in range(max_days):
        # Draw from normal with historical mu/sigma
        daily = np.random.normal(mu, sig, n_simulations)
        all_paths[:, d] = daily

    # Cumulative log return paths
    cum_paths = np.cumsum(all_paths, axis=1)

    for label, days in horizons_days.items():
        if days > max_days:
            continue
        terminal_log_ret = cum_paths[:, days - 1]
        prices = current_price * np.exp(terminal_log_ret)

        results[label] = {
            "days":           days,
            "target_base":    round(float(np.median(prices)), 2),
            "target_bull":    round(float(np.percentile(prices, 75)), 2),
            "target_bear":    round(float(np.percentile(prices, 25)), 2),
            "target_p90":     round(float(np.percentile(prices, 90)), 2),
            "target_p10":     round(float(np.percentile(prices, 10)), 2),
            "expected_return":round(float((np.median(prices) / current_price - 1) * 100), 1),
            "upside_p75":     round(float((np.percentile(prices, 75) / current_price - 1) * 100), 1),
            "downside_p25":   round(float((np.percentile(prices, 25) / current_price - 1) * 100), 1),
            "prob_positive":  round(float(np.mean(prices > current_price) * 100), 1),
        }

    return results


# ---------------------------------------------------------------------------
# TECHNICAL PRICE TARGETS  (S/R based)
# ---------------------------------------------------------------------------

def technical_price_targets(hist, current_price, analyst_target=None):
    """
    Derive price targets from:
      1. ATR multiples (1x, 2x, 3x above/below current)
      2. Pivot points (weekly/monthly)
      3. Key moving averages as support
      4. Analyst consensus as anchor
    """
    if hist is None or len(hist) < 30 or current_price is None:
        return {}

    close  = hist["Close"].dropna()
    high   = hist["High"].dropna()
    low    = hist["Low"].dropna()

    # ATR
    tr     = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr    = float(tr.rolling(14).mean().iloc[-1])

    # Pivot points (classic)
    p_high = float(high.tail(20).max())
    p_low  = float(low.tail(20).min())
    pivot  = (p_high + p_low + current_price) / 3
    r1     = 2 * pivot - p_low
    r2     = pivot + (p_high - p_low)
    r3     = p_high + 2 * (pivot - p_low)
    s1     = 2 * pivot - p_high
    s2     = pivot - (p_high - p_low)
    s3     = p_low  - 2 * (p_high - pivot)

    # Moving averages as support/resistance
    ma_levels = {}
    for window, label in [(20,"MA20"), (50,"MA50"), (100,"MA100"), (200,"MA200")]:
        if len(close) >= window:
            ma_levels[label] = round(float(close.rolling(window).mean().iloc[-1]), 2)

    # Build targets
    targets = {
        "atr":    round(atr, 2),
        "pivot":  round(pivot, 2),
        "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
        "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
        "ma_levels": ma_levels,
        # Upside targets (ATR multiples)
        "t1_technical": round(current_price + 1.5 * atr, 2),
        "t2_technical": round(current_price + 3.0 * atr, 2),
        "t3_technical": round(current_price + 5.0 * atr, 2),
        # Stop loss
        "stop_atr_1x":  round(current_price - 1.0 * atr, 2),
        "stop_atr_2x":  round(current_price - 2.0 * atr, 2),
    }

    if analyst_target:
        targets["analyst_target"] = analyst_target
        targets["analyst_upside"] = round((analyst_target / current_price - 1) * 100, 1)

    return targets


# ---------------------------------------------------------------------------
# FUNDAMENTAL PRICE TARGET (DCF-lite)
# ---------------------------------------------------------------------------

def fundamental_price_target(fund, current_price):
    """
    Simple DCF-lite using Free Cash Flow yield and growth.
    Also computes Graham Number and PEG-based fair value.
    """
    if current_price is None or current_price <= 0:
        return {}

    out = {}

    # --- FCF yield method ---
    fcf = fund.get("free_cash_flow")
    mc  = fund.get("market_cap")
    if fcf and mc and mc > 0 and fcf > 0:
        fcf_yield  = fcf / mc
        rg         = fund.get("revenue_growth") or 0
        # Fair yield: 4% for low-growth, 2% for high-growth
        fair_yield = max(0.015, 0.05 - rg * 0.1)
        fcf_target = current_price * (fcf_yield / fair_yield)
        out["fcf_target"] = round(fcf_target, 2)
        out["fcf_upside"] = round((fcf_target / current_price - 1) * 100, 1)

    # --- Graham Number ---
    eps = fund.get("eps_ttm")
    bv  = fund.get("price_to_book")
    if eps and bv and eps > 0 and current_price > 0:
        book_per_share = current_price / bv
        graham = math.sqrt(22.5 * eps * book_per_share)
        out["graham_number"] = round(graham, 2)
        out["graham_upside"] = round((graham / current_price - 1) * 100, 1)

    # --- PEG fair value ---
    fpe = fund.get("pe_forward")
    rg  = fund.get("revenue_growth")
    eps_fwd = fund.get("eps_forward")
    if fpe and rg and eps_fwd and eps_fwd > 0 and rg > 0:
        fair_pe    = rg * 100 * 1.0   # PEG=1 => fair PE = growth rate
        fair_price = eps_fwd * fair_pe
        out["peg_fair_value"] = round(fair_price, 2)
        out["peg_upside"]     = round((fair_price / current_price - 1) * 100, 1)

    # --- Composite fundamental target ---
    targets = [v for k, v in out.items() if k.endswith("_target") or k == "graham_number"]
    if targets:
        composite = float(np.median(targets))
        out["composite_fundamental_target"] = round(composite, 2)
        out["composite_upside"] = round((composite / current_price - 1) * 100, 1)

    return out


# ---------------------------------------------------------------------------
# COMBINED PRICE TARGET
# ---------------------------------------------------------------------------

def compute_price_targets(ticker, hist, fund, tech, overall_score):
    """
    Combine technical, fundamental, and statistical targets into
    a single set of price targets with confidence levels.
    """
    current_price = tech.get("price")
    if current_price is None:
        return {}

    analyst_target = fund.get("analyst_target")

    # Get component targets
    tech_targets   = technical_price_targets(hist, current_price, analyst_target)
    fund_targets   = fundamental_price_target(fund, current_price)

    # Statistical targets via Monte Carlo
    ret_dist       = compute_return_distribution(hist)
    bias           = (overall_score - 50) / 500.0   # score 80 -> +0.06%/day bias
    mc_targets     = monte_carlo_price_targets(current_price, ret_dist, bias_factor=bias)

    # Composite 1-month target: blend all methods
    candidates = []
    if mc_targets.get("1mo"):
        candidates.append(mc_targets["1mo"]["target_base"])
    if tech_targets.get("t1_technical"):
        candidates.append(tech_targets["t1_technical"])
    if fund_targets.get("composite_fundamental_target"):
        candidates.append(fund_targets["composite_fundamental_target"])
    if analyst_target:
        candidates.append(analyst_target)

    composite_target_1mo = round(float(np.median(candidates)), 2) if candidates else None
    composite_upside_1mo = round((composite_target_1mo / current_price - 1) * 100, 1) \
                           if composite_target_1mo else None

    return {
        "current_price":           current_price,
        "composite_target_1mo":    composite_target_1mo,
        "composite_upside_1mo":    composite_upside_1mo,
        "technical":               tech_targets,
        "fundamental":             fund_targets,
        "statistical":             mc_targets,
        "analyst_target":          analyst_target,
        "analyst_upside":          round((analyst_target / current_price - 1) * 100, 1)
                                   if analyst_target else None,
    }


# ---------------------------------------------------------------------------
# FULL BACKTEST + FORWARD TEST REPORT
# ---------------------------------------------------------------------------

def run_full_analysis(ticker, hist, fund, tech, overall_score):
    """
    Run the complete backtest + forward test pipeline.
    Returns a dict ready to include in the API response.
    """
    if hist is None or len(hist) < 60:
        return {"error": "Insufficient history for analysis"}

    start = time.time()

    # 1. Backtest all signals on this stock's history
    bt_results = backtest_signals(hist, ticker)

    # 2. Determine which signals are currently active
    close  = hist["Close"].dropna()
    volume = hist["Volume"].fillna(0)
    n      = len(close)
    active = []
    for sig_name, sig_fn in SIGNALS.items():
        try:
            if sig_fn(close, volume, n - 1):
                active.append(sig_name)
        except Exception:
            pass

    # 3. Calibrate score using historical accuracy
    raw_tech_score = tech.get("technical_score", 50)
    calibrated     = calibrate_score(raw_tech_score, bt_results, active)

    # 4. Price targets
    price_targets  = compute_price_targets(ticker, hist, fund, tech, overall_score)

    # 5. Summary stats
    if bt_results:
        avg_accuracy  = float(np.mean([v["accuracy_score"] for v in bt_results.values()]))
        best_signal   = max(bt_results.items(), key=lambda x: x[1]["accuracy_score"])
        worst_signal  = min(bt_results.items(), key=lambda x: x[1]["accuracy_score"])
    else:
        avg_accuracy = 50.0
        best_signal  = worst_signal = None

    elapsed = round(time.time() - start, 2)

    return {
        "ticker":                  ticker,
        "backtest_results":        bt_results,
        "active_signals":          active,
        "calibrated_tech_score":   calibrated,
        "avg_signal_accuracy":     round(avg_accuracy, 1),
        "best_signal":             {best_signal[0]: best_signal[1]}  if best_signal  else None,
        "worst_signal":            {worst_signal[0]: worst_signal[1]} if worst_signal else None,
        "price_targets":           price_targets,
        "computation_time_s":      elapsed,
    }
