"""
Crypto Telegram Alert Bot — Upgraded
======================================
Features:
  - Fear & Greed Index
  - Top hourly gainers & losers
  - Trending coins
  - BTC/Macro headlines
  - 5%+ pump alerts
  - Breakout & EMA rejection signals
  - Multiple Telegram chat IDs support
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

# Multiple IDs supported — comma separated in secret
# e.g. "123456789,987654321"
CHAT_IDS_RAW   = os.environ.get("CHAT_ID", "")
CHAT_IDS       = [cid.strip() for cid in CHAT_IDS_RAW.split(",") if cid.strip()]

PUMP_THRESHOLD  = 5.0
EMA_PERIODS     = [20, 50]
EMA_TOUCH_PCT   = 0.003
TOP_COINS       = 100
EMA_SCAN_LIMIT  = 25    # EMA check only top 25 (rate limit)

COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
FNG_URL         = "https://api.alternative.me/fng/"
NEWS_URL        = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC,Blockchain,Trading&sortOrder=latest&limit=5"

# Malaysia timezone = UTC+8
MYT = timezone(timedelta(hours=8))

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        payload = {
            "chat_id"                  : chat_id,
            "text"                     : message,
            "parse_mode"               : "Markdown",
            "disable_web_page_preview" : True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                print(f"Telegram error [{chat_id}]: {resp.text}")
        except Exception as e:
            print(f"Telegram send failed [{chat_id}]: {e}")
        time.sleep(0.3)

# ── Fear & Greed ──────────────────────────────────────────────────────────────

def get_fear_greed():
    try:
        resp = requests.get(FNG_URL, timeout=10)
        data = resp.json()
        value       = int(data["data"][0]["value"])
        label       = data["data"][0]["value_classification"]

        if value >= 75:
            emoji = "🤑"
        elif value >= 55:
            emoji = "😊"
        elif value >= 45:
            emoji = "😐"
        elif value >= 25:
            emoji = "😨"
        else:
            emoji = "😱"

        return f"{emoji} {value}/100 - {label}"
    except Exception:
        return "N/A"

# ── CoinGecko Data ────────────────────────────────────────────────────────────

def get_top_coins():
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency"             : "usd",
        "order"                   : "market_cap_desc",
        "per_page"                : TOP_COINS,
        "page"                    : 1,
        "sparkline"               : False,
        "price_change_percentage" : "1h,24h",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_trending():
    try:
        url  = f"{COINGECKO_BASE}/search/trending"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        coins = data.get("coins", [])[:7]
        lines = []
        for c in coins:
            item   = c["item"]
            name   = item.get("name", "")
            symbol = item.get("symbol", "").upper()
            rank   = item.get("market_cap_rank", "?")
            lines.append(f"⭐ {symbol} ({name}) - #{rank}")
        return lines
    except Exception:
        return []


def get_btc_headlines():
    try:
        resp  = requests.get(NEWS_URL, timeout=10)
        data  = resp.json()
        items = data.get("Data", [])[:4]
        lines = []
        for item in items:
            title  = item.get("title", "")
            source = item.get("source_info", {}).get("name", "")
            # Truncate long titles
            if len(title) > 80:
                title = title[:77] + "..."
            lines.append(f"📰 {title} — _{source}_")
        return lines
    except Exception:
        return []


def get_ohlc(coin_id, days=2):
    url    = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": days}
    resp   = requests.get(url, params=params, timeout=10)
    if resp.status_code != 200:
        return []
    return resp.json()

# ── Indicators ────────────────────────────────────────────────────────────────

def calc_ema(prices, period):
    if len(prices) < period:
        return []
    k        = 2.0 / (period + 1)
    ema_vals = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))
    pad = [None] * (len(prices) - len(ema_vals))
    return pad + ema_vals

# ── Signal Checks ─────────────────────────────────────────────────────────────

def check_pump(coin):
    symbol    = coin.get("symbol", "").upper()
    change_1h = coin.get("price_change_percentage_1h_in_currency")
    price     = coin.get("current_price", 0)
    if change_1h is None:
        return None
    if change_1h >= PUMP_THRESHOLD:
        return (
            f"🚀 *PUMP* `{symbol}` +{change_1h:.2f}% (1h) | ${price:,.4g}"
        )
    return None


def check_breakout(coin):
    symbol   = coin.get("symbol", "").upper()
    price    = coin.get("current_price", 0)
    high_24h = coin.get("high_24h", 0)
    low_24h  = coin.get("low_24h", 0)
    change24 = coin.get("price_change_percentage_24h_in_currency") or 0
    if not high_24h or not price:
        return None
    dist_pct = ((high_24h - price) / high_24h) * 100
    if dist_pct <= 0.5 and change24 >= 3.0:
        return (
            f"⚡ *BREAKOUT* `{symbol}` near 24h high ${high_24h:,.4g} | Now ${price:,.4g}"
        )
    return None


def check_ema_rejection(coin_id, symbol):
    raw = get_ohlc(coin_id, days=2)
    if not raw or len(raw) < 52:
        return []
    closes = [c[4] for c in raw]
    highs  = [c[2] for c in raw]
    lows   = [c[3] for c in raw]
    alerts = []
    for period in EMA_PERIODS:
        ema_series = calc_ema(closes, period)
        if not ema_series or ema_series[-2] is None:
            continue
        ema_val = ema_series[-2]
        c_close = closes[-2]
        c_low   = lows[-2]
        c_high  = highs[-2]
        if c_low <= ema_val * (1 + EMA_TOUCH_PCT) and c_close > ema_val:
            alerts.append(
                f"📈 *EMA{period} Bullish Rejection* `{symbol}` | EMA: ${ema_val:,.4g} Close: ${c_close:,.4g}"
            )
        if c_high >= ema_val * (1 - EMA_TOUCH_PCT) and c_close < ema_val:
            alerts.append(
                f"📉 *EMA{period} Bearish Rejection* `{symbol}` | EMA: ${ema_val:,.4g} Close: ${c_close:,.4g}"
            )
    return alerts

# ── Format Helpers ────────────────────────────────────────────────────────────

def format_price(p):
    if p >= 1:
        return f"${p:,.4f}"
    elif p >= 0.01:
        return f"${p:.5f}"
    else:
        return f"${p:.8f}"

def format_change(c):
    if c is None:
        return "N/A"
    arrow = "🟢" if c >= 0 else "🔴"
    sign  = "+" if c >= 0 else ""
    return f"{arrow} {sign}{c:.1f}%"

# ── Main Report ───────────────────────────────────────────────────────────────

def run_scan():
    now_myt = datetime.now(MYT).strftime("%d %B %Y, %H:%M +08")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_utc}] Starting scan...")

    # ── Fetch all data ────────────────────────────────────────────────────────
    try:
        coins = get_top_coins()
    except Exception as e:
        send_telegram(f"❌ API error: {e}")
        return

    fng_text  = get_fear_greed()
    trending  = get_trending()
    headlines = get_btc_headlines()

    # ── Gainers & Losers ──────────────────────────────────────────────────────
    valid = [c for c in coins if c.get("price_change_percentage_1h_in_currency") is not None]
    gainers = sorted(valid, key=lambda x: x["price_change_percentage_1h_in_currency"], reverse=True)[:5]
    losers  = sorted(valid, key=lambda x: x["price_change_percentage_1h_in_currency"])[:5]

    # ── Signals ───────────────────────────────────────────────────────────────
    pump_alerts      = []
    breakout_alerts  = []
    ema_alerts       = []

    for i, coin in enumerate(coins):
        symbol  = coin.get("symbol", "").upper()
        coin_id = coin.get("id", "")

        p = check_pump(coin)
        if p:
            pump_alerts.append(p)

        b = check_breakout(coin)
        if b:
            breakout_alerts.append(b)

        if i < EMA_SCAN_LIMIT:
            try:
                ea = check_ema_rejection(coin_id, symbol)
                ema_alerts.extend(ea)
            except Exception:
                pass
            time.sleep(1.2)

    # ── Build Message ─────────────────────────────────────────────────────────
    lines = []
    lines.append(f"⏰ *HOURLY CRYPTO SIGNALS*")
    lines.append(f"📅 {now_myt}\n")

    # Fear & Greed
    lines.append(f"📊 *FEAR & GREED INDEX*")
    lines.append(f"{fng_text}\n")

    # Top Gainers
    lines.append(f"📈 *TOP HOURLY GAINERS:*")
    for c in gainers:
        sym    = c.get("symbol","").upper()
        price  = format_price(c.get("current_price", 0))
        chg    = c.get("price_change_percentage_1h_in_currency", 0)
        lines.append(f"🟢 {sym} - {price} (+{chg:.1f}%)")
    lines.append("")

    # Top Losers
    lines.append(f"📉 *TOP HOURLY LOSERS:*")
    for c in losers:
        sym   = c.get("symbol","").upper()
        price = format_price(c.get("current_price", 0))
        chg   = c.get("price_change_percentage_1h_in_currency", 0)
        lines.append(f"🔴 {sym} - {price} ({chg:.1f}%)")
    lines.append("")

    # Trending
    if trending:
        lines.append(f"🔥 *TRENDING NOW:*")
        lines.extend(trending)
        lines.append("")

    # Signals
    has_signals = pump_alerts or breakout_alerts or ema_alerts
    if has_signals:
        lines.append(f"🎯 *SIGNALS DETECTED:*")
        for a in pump_alerts:
            lines.append(a)
        for a in breakout_alerts:
            lines.append(a)
        for a in ema_alerts:
            lines.append(a)
        lines.append("")

    # BTC Headlines
    if headlines:
        lines.append(f"📰 *BTC & MACRO NEWS:*")
        lines.extend(headlines)
        lines.append("")

    lines.append(f"⏰ _Next update in 1 hour_")

    message = "\n".join(lines)

    # Telegram 4096 char limit — split if needed
    if len(message) > 4000:
        send_telegram("\n".join(lines[:40]))
        send_telegram("\n".join(lines[40:]))
    else:
        send_telegram(message)

    print(f"Done. Pump:{len(pump_alerts)} Breakout:{len(breakout_alerts)} EMA:{len(ema_alerts)}")


if __name__ == "__main__":
    run_scan()
