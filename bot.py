"""
Crypto Telegram Alert Bot (CoinGecko API)
==========================================
Works in Malaysia — CoinGecko is a data aggregator, not an exchange.

Checks every hour:
  1. 5%+ pump (1h price change)
  2. 24h Breakout (price near 24h high)
  3. EMA rejection (EMA20/50 on 30min candles)
"""

import os
import time
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

PUMP_THRESHOLD  = 5.0
EMA_PERIODS     = [20, 50]
EMA_TOUCH_PCT   = 0.003
TOP_COINS       = 100   # top 100 coins by market cap

COINGECKO_BASE  = "https://api.coingecko.com/api/v3"

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram error: {resp.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")

# ── CoinGecko Data ────────────────────────────────────────────────────────────

def get_top_coins():
    """
    Fetch top coins with 1h, 24h price change data.
    Returns list of coin dicts.
    """
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency"                    : "usd",
        "order"                          : "market_cap_desc",
        "per_page"                       : TOP_COINS,
        "page"                           : 1,
        "sparkline"                      : False,
        "price_change_percentage"        : "1h,24h",
        "locale"                         : "en",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_ohlc(coin_id, days=1):
    """
    Fetch OHLC candles for a coin.
    days=1 → 30min candles (~48 candles)
    days=2 → 30min candles (~96 candles)
    Returns list of [timestamp, open, high, low, close]
    """
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": days}
    resp = requests.get(url, params=params, timeout=10)
    if resp.status_code != 200:
        return []
    return resp.json()


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    ema_vals = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))
    pad = [None] * (len(prices) - len(ema_vals))
    return pad + ema_vals


# ── Alert Checks ──────────────────────────────────────────────────────────────

def check_pump(coin):
    """5%+ gain in last 1 hour."""
    symbol     = coin.get("symbol", "").upper()
    change_1h  = coin.get("price_change_percentage_1h_in_currency")
    price      = coin.get("current_price", 0)

    if change_1h is None:
        return None
    if change_1h >= PUMP_THRESHOLD:
        return (
            f"🚀 *PUMP ALERT*\n"
            f"Coin   : `{symbol}/USDT`\n"
            f"Change : *+{change_1h:.2f}%* (1h)\n"
            f"Price  : ${price:,.4g}\n"
        )
    return None


def check_breakout(coin):
    """
    Price within 0.5% of 24h high = breakout zone.
    Simple but effective for quick scan.
    """
    symbol   = coin.get("symbol", "").upper()
    price    = coin.get("current_price", 0)
    high_24h = coin.get("high_24h", 0)
    low_24h  = coin.get("low_24h", 0)

    if not high_24h or not price:
        return None

    # Price within 0.5% of 24h high
    distance_pct = ((high_24h - price) / high_24h) * 100

    # Also check: price broke above and is at new high
    change_24h = coin.get("price_change_percentage_24h_in_currency", 0) or 0

    if distance_pct <= 0.5 and change_24h >= 3.0:
        range_pct = ((high_24h - low_24h) / low_24h) * 100
        return (
            f"⚡ *BREAKOUT ZONE*\n"
            f"Coin       : `{symbol}/USDT`\n"
            f"Price      : ${price:,.4g}\n"
            f"24h High   : ${high_24h:,.4g} (only {distance_pct:.2f}% away)\n"
            f"24h Range  : {range_pct:.1f}%\n"
        )
    return None


def check_ema_rejection(coin_id, symbol):
    """
    EMA20/50 rejection on 30min candles.
    Wick touches EMA, candle closes on other side.
    """
    raw = get_ohlc(coin_id, days=2)
    if not raw or len(raw) < 52:
        return []

    opens  = [c[1] for c in raw]
    highs  = [c[2] for c in raw]
    lows   = [c[3] for c in raw]
    closes = [c[4] for c in raw]

    alerts = []

    for period in EMA_PERIODS:
        ema_series = calc_ema(closes, period)
        if not ema_series or ema_series[-2] is None:
            continue

        ema_val    = ema_series[-2]
        c_close    = closes[-2]
        c_low      = lows[-2]
        c_high     = highs[-2]

        # Bullish rejection: wick below EMA, close above
        if c_low <= ema_val * (1 + EMA_TOUCH_PCT) and c_close > ema_val:
            alerts.append(
                f"📈 *EMA{period} BULLISH REJECTION* (30m)\n"
                f"Coin   : `{symbol}/USDT`\n"
                f"EMA{period}  : ${ema_val:,.4g}\n"
                f"Candle : Low ${c_low:,.4g} → Close ${c_close:,.4g}\n"
                f"Signal : Wick kissed EMA, closed above ✅\n"
            )

        # Bearish rejection: wick above EMA, close below
        if c_high >= ema_val * (1 - EMA_TOUCH_PCT) and c_close < ema_val:
            alerts.append(
                f"📉 *EMA{period} BEARISH REJECTION* (30m)\n"
                f"Coin   : `{symbol}/USDT`\n"
                f"EMA{period}  : ${ema_val:,.4g}\n"
                f"Candle : High ${c_high:,.4g} → Close ${c_close:,.4g}\n"
                f"Signal : Wick kissed EMA, closed below ⚠️\n"
            )

    return alerts


# ── Main Scan ─────────────────────────────────────────────────────────────────

def run_scan():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_utc}] Starting crypto scan...")

    try:
        coins = get_top_coins()
    except Exception as e:
        send_telegram(f"❌ CoinGecko API error: {e}")
        return

    print(f"Scanning {len(coins)} coins...")
    all_alerts = []

    for i, coin in enumerate(coins):
        symbol  = coin.get("symbol", "").upper()
        coin_id = coin.get("id", "")

        # 1. Pump check
        pump = check_pump(coin)
        if pump:
            all_alerts.append(pump)

        # 2. Breakout check
        bo = check_breakout(coin)
        if bo:
            all_alerts.append(bo)

        # 3. EMA rejection (only for top 30 to avoid rate limit)
        if i < 30:
            try:
                ema_alerts = check_ema_rejection(coin_id, symbol)
                all_alerts.extend(ema_alerts)
            except Exception:
                pass
            time.sleep(1.5)  # CoinGecko free tier: 30 req/min

    # ── Send results ──────────────────────────────────────────────────────────
    if all_alerts:
        header = f"🔔 *Crypto Alerts* | {now_utc}\n{'─'*28}\n"
        chunk_size = 8
        for i in range(0, len(all_alerts), chunk_size):
            chunk = all_alerts[i:i + chunk_size]
            body  = "\n".join(chunk)
            send_telegram(header + body)
            time.sleep(1)
        print(f"Sent {len(all_alerts)} alerts.")
    else:
        send_telegram(f"✅ *Scan complete* | {now_utc}\nNo significant signals this hour.")
        print("No alerts this scan.")


if __name__ == "__main__":
    run_scan()
