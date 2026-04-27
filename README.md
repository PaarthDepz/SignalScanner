"""
SIGNAL - Live Stock Market Scanner
"""
import os
import time
from flask import Flask, render_template, jsonify

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import scanner

app = Flask(__name__)

_cache = {}
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "300"))


def get_cached(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None


def set_cache(key, data):
    _cache[key] = {"data": data, "ts": time.time()}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        picks = scanner.run_top_picks_scan()
        return jsonify({"ok": True, "picks": picks, "scanTime": time.strftime("%H:%M UTC")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/ticker/<ticker>")
def api_ticker(ticker):
    ticker = ticker.upper().strip()
    cached = get_cached(ticker)
    if cached:
        return jsonify({"ok": True, "data": cached, "cached": True})
    try:
        data = scanner.analyze_ticker(ticker)
        set_cache(ticker, data)
        return jsonify({"ok": True, "data": data, "cached": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/reddit/trending")
def api_reddit():
    try:
        tickers = scanner.get_reddit_trending()
        return jsonify({"ok": True, "tickers": tickers})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
