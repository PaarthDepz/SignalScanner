import os
import time
import traceback
from flask import Flask, render_template, jsonify

try:
from dotenv import load_dotenv
load_dotenv()
except Exception:
pass

try:
import scanner
SCANNER_OK = True
SCANNER_ERROR = None
except Exception as e:
SCANNER_OK = False
SCANNER_ERROR = traceback.format_exc()

app = Flask(**name**)

_cache = {}
CACHE_TTL = int(os.environ.get(“CACHE_TTL_SECONDS”, “300”))
BT_CACHE_TTL = 3600

def get_cached(key):
entry = _cache.get(key)
if entry and (time.time() - entry[“ts”]) < entry.get(“ttl”, CACHE_TTL):
return entry[“data”]
return None

def set_cache(key, data, ttl=None):
_cache[key] = {“data”: data, “ts”: time.time(), “ttl”: ttl or CACHE_TTL}

@app.route(”/”)
def index():
if not SCANNER_OK:
return “<pre>Scanner failed to load:\n\n{}</pre>”.format(SCANNER_ERROR), 500
return render_template(“index.html”)

@app.route(”/api/scan”, methods=[“POST”])
def api_scan():
if not SCANNER_OK:
return jsonify({“ok”: False, “error”: SCANNER_ERROR}), 500
try:
picks = scanner.run_top_picks_scan(include_reddit=False)
return jsonify({“ok”: True, “picks”: picks, “scanTime”: time.strftime(”%H:%M UTC”)})
except Exception:
return jsonify({“ok”: False, “error”: traceback.format_exc()}), 500

@app.route(”/api/scan/reddit”, methods=[“POST”])
def api_scan_reddit():
if not SCANNER_OK:
return jsonify({“ok”: False, “error”: SCANNER_ERROR}), 500
try:
picks = scanner.run_top_picks_scan(include_reddit=True)
return jsonify({“ok”: True, “picks”: picks, “scanTime”: time.strftime(”%H:%M UTC”)})
except Exception:
return jsonify({“ok”: False, “error”: traceback.format_exc()}), 500

@app.route(”/api/ticker/<ticker>”)
def api_ticker(ticker):
ticker = ticker.upper().strip()
cached = get_cached(“ticker_” + ticker)
if cached:
return jsonify({“ok”: True, “data”: cached, “cached”: True})
if not SCANNER_OK:
return jsonify({“ok”: False, “error”: SCANNER_ERROR}), 500
try:
data = scanner.analyze_ticker(ticker)
set_cache(“ticker_” + ticker, data)
return jsonify({“ok”: True, “data”: data, “cached”: False})
except Exception:
return jsonify({“ok”: False, “error”: traceback.format_exc()}), 500

@app.route(”/api/backtest/<ticker>”)
def api_backtest(ticker):
ticker = ticker.upper().strip()
cached = get_cached(“bt_” + ticker)
if cached:
return jsonify({“ok”: True, “data”: cached, “cached”: True})
if not SCANNER_OK:
return jsonify({“ok”: False, “error”: SCANNER_ERROR}), 500
try:
import yfinance as yf
import backtest as bt
tkr = yf.Ticker(ticker)
hist = None
for period in (“5y”, “3y”, “2y”):
try:
h = tkr.history(period=period)
if len(h) >= 260:
hist = h
break
except Exception:
continue
if hist is None or len(hist) < 260:
return jsonify({“ok”: False, “error”: “Need at least 1 year of history”}), 400
info = {}
try:
info = tkr.info or {}
except Exception:
pass
fund = scanner.compute_fundamentals(ticker, info, tkr)
tech = scanner.compute_technicals(hist)
overall = scanner.compute_overall(50, fund[“fundamentals_score”], tech[“technical_score”])
report = bt.run_full_analysis(ticker, hist, fund, tech, overall)
set_cache(“bt_” + ticker, report, ttl=BT_CACHE_TTL)
return jsonify({“ok”: True, “data”: report, “cached”: False})
except Exception:
return jsonify({“ok”: False, “error”: traceback.format_exc()}), 500

@app.route(”/api/daytrading/<ticker>”)
def api_daytrading(ticker):
ticker = ticker.upper().strip()
cached = get_cached(“dt_” + ticker)
if cached:
return jsonify({“ok”: True, “data”: cached, “cached”: True})
try:
import daytrader as dt
data = dt.analyze_daytrading(ticker)
set_cache(“dt_” + ticker, data, ttl=60)
return jsonify({“ok”: True, “data”: data, “cached”: False})
except Exception:
return jsonify({“ok”: False, “error”: traceback.format_exc()}), 500

@app.route(”/api/reddit/trending”)
def api_reddit():
if not SCANNER_OK:
return jsonify({“ok”: False, “error”: SCANNER_ERROR}), 500
try:
tickers = scanner.get_reddit_trending()
return jsonify({“ok”: True, “tickers”: tickers})
except Exception:
return jsonify({“ok”: False, “error”: traceback.format_exc()}), 500

@app.route(”/health”)
def health():
return jsonify({“status”: “ok” if SCANNER_OK else “scanner_error”, “scanner_error”: SCANNER_ERROR})

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, “5000”))
app.run(host=“0.0.0.0”, port=port, debug=False)
