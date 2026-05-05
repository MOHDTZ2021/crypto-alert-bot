"""
Crypto Telegram Alert Bot
=========================
Checks every hour:
  1. 5%+ pump (1h candle change)
  2. Breakout (price breaks 20-candle high)
  3. EMA rejection (price bounces off EMA20 or EMA50)

Data source : Binance Public API (no key needed)
Notification : Telegram Bot API
"""

import os
import time
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")   # set in GitHub Secrets
CHAT_ID        = os.environ.get("CHAT_ID", "")          # your personal chat id

PUMP_THRESHOLD  = 5.0     # % gain in last 1 hour to trigger pump alert
EMA_PERIODS     = [20, 50]
KLINE_LIMIT     = 55      # enough candles for EMA50
EMA_TOUCH_PCT   = 0.003   # wick must come within 0.3% of EMA to count as "touch"
TOP_PAIRS_LIMIT = 80      # only scan top 80 USDT pairs by 24h volume (speed)

BINANCE_BASE    = "https://api.binance.com/api/v3"

# ── Helpers ───────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    """Send a Telegram message (silently fail if token not set)."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[WARN] TELEGRAM_TOKEN or CHAT_ID not set — printing to console.")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id"    : CHAT_ID,
        "text"       : message,
        "parse_mode" : "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[ERROR] Telegram API: {resp.text}")
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


def get_top_usdt_pairs() -> list[dict]:
    """
    Fetch 24h ticker for all USDT spot pairs.
    Returns top N by quoteVolume (USDT traded), sorted descending.
    """
    url = f"{BINANCE_BASE}/ticker/24hr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    usdt_pairs = [
        d for d in data
        if d["symbol"].endswith("USDT")
        and not any(x in d["symbol"] for x in ["UP", "DOWN", "BEAR", "BULL"])  # no leveraged tokens
    ]
    # Sort by 24h USDT volume, take top N
    usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    return usdt_pairs[:TOP_PAIRS_LIMIT]


def get_klines(symbol: str, interval: str = "1h", limit: int = KLINE_LIMIT) -> list:
    """Fetch OHLCV candles from Binance. Returns list of lists."""
    url = f"{BINANCE_BASE}/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=10)
    if resp.status_code != 200:
        return []
    return resp.json()


# ── Indicator Calculations ────────────────────────────────────────────────────

def calc_ema(prices: list[float], period: int) -> list[float]:
    """Calculate EMA for a price series. Returns same-length list."""
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    ema_values = [sum(prices[:period]) / period]  # seed with SMA
    for price in prices[period:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    # Pad front with None to keep index alignment
    pad = [None] * (len(prices) - len(ema_values))
    return pad + ema_values


def parse_klines(raw: list) -> dict:
    """Extract OHLCV lists from Binance kline response."""
    return {
        "open"  : [float(k[1]) for k in raw],
        "high"  : [float(k[2]) for k in raw],
        "low"   : [float(k[3]) for k in raw],
        "close" : [float(k[4]) for k in raw],
        "volume": [float(k[5]) for k in raw],
    }


# ── Alert Checks ──────────────────────────────────────────────────────────────

def check_pump_1h(ticker: dict) -> str | None:
    """
    1-hour pump check.
    Compares current price vs open price of the latest closed 1h candle.
    We use Binance's 1h priceChange which is actually 24h — so we pull klines instead.
    """
    symbol = ticker["symbol"]
    raw = get_klines(symbol, "1h", 3)
    if len(raw) < 2:
        return None

    candle   = raw[-2]           # last *closed* candle
    o        = float(candle[1])  # open
    c        = float(candle[4])  # close
    change_pct = ((c - o) / o) * 100

    if change_pct >= PUMP_THRESHOLD:
        return (
            f"🚀 *PUMP ALERT*\n"
            f"Coin   : `{symbol}`\n"
            f"Change : *+{change_pct:.2f}%* (last 1h candle)\n"
            f"Open   : {o:.6g}  →  Close: {c:.6g}\n"
        )
    return None


def check_breakout(symbol: str, ohlcv: dict) -> str | None:
    """
    Breakout: last closed candle closes ABOVE the highest high
    of the prior 20 candles (resistance break).
    """
    closes = ohlcv["close"]
    highs  = ohlcv["high"]

    if len(closes) < 22:
        return None

    # Last closed candle = index -2 (index -1 is the forming candle)
    prev_high_window = highs[-22:-2]   # 20 candles before last closed
    resistance       = max(prev_high_window)
    current_close    = closes[-2]
    prev_close       = closes[-3]

    if current_close > resistance and prev_close <= resistance:
        breakout_pct = ((current_close - resistance) / resistance) * 100
        return (
            f"⚡ *BREAKOUT ALERT*\n"
            f"Coin       : `{symbol}`\n"
            f"Broke      : {resistance:.6g} resistance\n"
            f"Close      : {current_close:.6g} (+{breakout_pct:.2f}%)\n"
            f"Lookback   : 20 candles (1h)\n"
        )
    return None


def check_ema_rejection(symbol: str, ohlcv: dict) -> list[str]:
    """
    EMA Rejection (SMC confluence):
    - Price wicks INTO EMA20 or EMA50
    - Candle closes back above/below the EMA (rejection)
    - Confirms trend continuation setup
    """
    closes = ohlcv["close"]
    highs  = ohlcv["high"]
    lows   = ohlcv["low"]
    alerts = []

    if len(closes) < KLINE_LIMIT:
        return alerts

    for period in EMA_PERIODS:
        ema_series = calc_ema(closes, period)
        if not ema_series or ema_series[-2] is None:
            continue

        ema_val      = ema_series[-2]   # EMA at last closed candle
        c_close      = closes[-2]
        c_low        = lows[-2]
        c_high       = highs[-2]
        prev_close   = closes[-3]

        # ── Bullish rejection (wick below EMA, close above) ──
        wick_touched_below = c_low <= ema_val * (1 + EMA_TOUCH_PCT)
        close_above        = c_close > ema_val
        was_bearish_before = prev_close < ema_val  # came from below, now rejected up

        if wick_touched_below and close_above:
            alerts.append(
                f"📈 *EMA{period} BULLISH REJECTION*\n"
                f"Coin   : `{symbol}`\n"
                f"EMA{period}  : {ema_val:.6g}\n"
                f"Candle : Low {c_low:.6g} → Close {c_close:.6g}\n"
                f"Signal : Wick kissed EMA, closed above → Bullish\n"
            )

        # ── Bearish rejection (wick above EMA, close below) ──
        wick_touched_above = c_high >= ema_val * (1 - EMA_TOUCH_PCT)
        close_below        = c_close < ema_val

        if wick_touched_above and close_below:
            alerts.append(
                f"📉 *EMA{period} BEARISH REJECTION*\n"
                f"Coin   : `{symbol}`\n"
                f"EMA{period}  : {ema_val:.6g}\n"
                f"Candle : High {c_high:.6g} → Close {c_close:.6g}\n"
                f"Signal : Wick kissed EMA, closed below → Bearish\n"
            )

    return alerts


# ── Main Scan ─────────────────────────────────────────────────────────────────

def run_scan() -> None:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_utc}] Starting crypto scan...")

    tickers = get_top_usdt_pairs()
    print(f"Scanning {len(tickers)} USDT pairs...")

    all_alerts: list[str] = []

    for i, ticker in enumerate(tickers):
        symbol = ticker["symbol"]

        # ── 1. Pump check (uses 1h kline, fetched inside function) ──
        pump_alert = check_pump_1h(ticker)
        if pump_alert:
            all_alerts.append(pump_alert)

        # ── 2. Breakout + EMA rejection (shared kline fetch) ──
        raw = get_klines(symbol, "1h", KLINE_LIMIT)
        if raw:
            ohlcv = parse_klines(raw)

            bo = check_breakout(symbol, ohlcv)
            if bo:
                all_alerts.append(bo)

            ema_alerts = check_ema_rejection(symbol, ohlcv)
            all_alerts.extend(ema_alerts)

        # Be polite to Binance — avoid rate limit (1200 req/min weight limit)
        if i % 10 == 9:
            time.sleep(0.5)

    # ── Send results ──────────────────────────────────────────────────────────
    if all_alerts:
        header = f"🔔 *Crypto Alerts* | {now_utc}\n{'─'*30}\n"
        # Split into chunks of 10 alerts to avoid Telegram 4096 char limit
        chunk_size = 10
        for chunk_start in range(0, len(all_alerts), chunk_size):
            chunk = all_alerts[chunk_start:chunk_start + chunk_size]
            body  = "\n".join(chunk)
            send_telegram(header + body)
            time.sleep(1)  # avoid Telegram flood
        print(f"Sent {len(all_alerts)} alerts.")
    else:
        # Send a heartbeat so you know bot is alive
        send_telegram(f"✅ *Scan complete* | {now_utc}\nNo significant signals this hour.")
        print("No alerts this scan.")


if __name__ == "__main__":
    run_scan()
