"""
SIGNAL - Live Stock Market Scanner
"""
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
    if not SCANNER_OK:
        return f"<pre>Scanner failed to load:\n\n{SCANNER_ERROR}</pre>", 500
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if not SCANNER_OK:
        return jsonify({"ok": False, "error": SCANNER_ERROR}), 500
    try:
        picks = scanner.run_top_picks_scan()
        return jsonify({"ok": True, "picks": picks, "scanTime": time.strftime("%H:%M UTC")})
    except Exception as e:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/ticker/<ticker>")
def api_ticker(ticker):
    if not SCANNER_OK:
        return jsonify({"ok": False, "error": SCANNER_ERROR}), 500
    ticker = ticker.upper().strip()
    cached = get_cached(ticker)
    if cached:
        return jsonify({"ok": True, "data": cached, "cached": True})
    try:
        data = scanner.analyze_ticker(ticker)
        set_cache(ticker, data)
        return jsonify({"ok": True, "data": data, "cached": False})
    except Exception as e:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/api/reddit/trending")
def api_reddit():
    if not SCANNER_OK:
        return jsonify({"ok": False, "error": SCANNER_ERROR}), 500
    try:
        tickers = scanner.get_reddit_trending()
        return jsonify({"ok": True, "tickers": tickers})
    except Exception as e:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/health")
def health():
    return jsonify({
        "status": "ok" if SCANNER_OK else "scanner_error",
        "scanner_error": SCANNER_ERROR
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
