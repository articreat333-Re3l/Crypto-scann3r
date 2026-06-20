import os
import json
import logging
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import time
from collections import Counter
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# =========================
# 🔐 KEYS
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variables"
    )

# Safety: auto-swap if the user pasted the values into the wrong slots.
# Bot tokens contain ':'; chat IDs are numeric (optionally with a leading '-').
if ":" in CHAT_ID and ":" not in TELEGRAM_TOKEN:
    TELEGRAM_TOKEN, CHAT_ID = CHAT_ID, TELEGRAM_TOKEN
    log.warning("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID looked swapped — auto-corrected.")

# =========================
# 🔁 HTTP SESSION WITH RETRY
# =========================
_retry = Retry(
    total=3,
    backoff_factor=1,  # waits 1s, 2s, 4s between attempts
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=_retry))

symbols = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "TRUMPUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "TONUSDT",
    "AAVEUSDT",
    "TAOUSDT",
    "SUIUSDT",
]

# Suppress duplicate alerts for the same symbol+direction for this many seconds.
ALERT_COOLDOWN_SECONDS = 3 * 60 * 60  # 3 hours
MIN_RR = 2.0  # minimum reward:risk ratio required before firing an alert
VOL_LOOKBACK = 20  # candles used to compute average volume for the volume filter
HEARTBEAT_INTERVAL = 60 * 60  # 1 hour
last_alert_time: dict = {}  # loaded from JSON below; key: f"{symbol}:{direction}" -> unix ts
last_heartbeat = 0
bot_start_time = time.time()
last_loop_time: datetime | None = None

# =========================
# DAILY SUMMARY
# =========================
DAILY_SUMMARY_HOUR_UTC = 8
SIGNAL_LOG_FILE = "signals.csv"


def save_history(*args, **kwargs):
    pass


alert_history = []
last_summary_date = ""
last_alert_time = {}


def record_alert(symbol, direction):
    global last_summary_date, last_alert_time
    alert_history.append(
        {
            "symbol": symbol,
            "direction": direction,
            "ts": time.time(),
        }
    )
    save_history(alert_history, last_summary_date, last_alert_time)


def build_daily_summary():
    cutoff = time.time() - 24 * 60 * 60
    recent = [a for a in alert_history if a.get("ts", 0) >= cutoff]
    total = len(recent)
    bullish = sum(1 for a in recent if a["direction"] == "bullish")
    bearish = total - bullish

    if total == 0:
        return (
            "🌅 <b>Daily Summary</b> — last 24h\n"
            "No alerts triggered. Markets were quiet (or filters did their job)."
        )

    by_symbol = Counter(a["symbol"] for a in recent).most_common(5)
    top_lines = "\n".join(f"  • <code>{s}</code> — {n}" for s, n in by_symbol)

    return (
        f"🌅 <b>Daily Summary</b> — last 24h\n"
        f"📊 Total alerts: <b>{total}</b>\n"
        f"🔺 Bullish: {bullish}   🔻 Bearish: {bearish}\n\n"
        f"<b>Most active:</b>\n{top_lines}"
    )


def log_signal(symbol, direction, price, target, stop_loss, rr, pattern):
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "target": target,
        "stop_loss": stop_loss,
        "rr": rr,
        "pattern": pattern,
    }
    df_row = pd.DataFrame([row])
    if os.path.exists(SIGNAL_LOG_FILE):
        df_row.to_csv(SIGNAL_LOG_FILE, mode="a", header=False, index=False)
    else:
        df_row.to_csv(SIGNAL_LOG_FILE, index=False)


def send_heartbeat():
    send_alert(
        f"💓 Bot Alive\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"📊 Watching {len(symbols)} symbols"
    )


def maybe_send_daily_summary():
    global last_summary_date
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    if now.hour >= DAILY_SUMMARY_HOUR_UTC and last_summary_date != today_str:
        send_alert(build_daily_summary())
        last_summary_date = today_str
        save_history(alert_history, last_summary_date, last_alert_time)
        log.info("Daily summary sent for %s", today_str)


def to_okx_inst(symbol):
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    return symbol


# =========================
# 📲 TELEGRAM
# =========================
def send_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        body = r.json()
        if not body.get("ok"):
            log.error("Telegram error: %s", body)
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def send_reply(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        session.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        log.error("send_reply failed: %s", e)


def poll_commands():
    offset = 0
    while True:
        try:
            r = session.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                },
                timeout=40,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = msg.get("chat", {}).get("id")
                if not chat_id:
                    continue
                if text in (
                    "/status",
                    "/status@" + TELEGRAM_TOKEN.split(":")[1]
                    if ":" in TELEGRAM_TOKEN
                    else "/status",
                ):
                    _handle_status(chat_id)
        except Exception as e:
            log.error("poll_commands error: %s", e)
            time.sleep(10)


def _handle_status(chat_id):
    now = time.time()
    uptime_secs = int(now - bot_start_time)
    days, rem = divmod(uptime_secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    uptime_str = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"

    loop_str = (
        last_loop_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        if last_loop_time
        else "not yet run"
    )

    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    today_signals = sum(1 for a in alert_history if a.get("ts", 0) >= today_start)

    send_reply(
        chat_id,
        f"📡 <b>Bot Status</b>\n"
        f"⏱ Uptime: <b>{uptime_str}</b>\n"
        f"🔄 Last loop: <code>{loop_str}</code>\n"
        f"🔔 Signals today: <b>{today_signals}</b>\n"
        f"👁 Watching: <b>{len(symbols)} symbols</b>",
    )


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def tradingview_link(symbol):
    return f"https://www.tradingview.com/chart/?symbol=OKX%3A{symbol}.P&interval=15"


def build_alert(
    symbol,
    direction,
    price,
    level,
    vol_ratio,
    target,
    stop_loss,
    struct_invalidation,
    triangle_height,
    rr,
    pattern="symmetric",
):
    if direction == "bullish":
        emoji = "🔺"
        level_label = "Resistance"
    else:
        emoji = "🔻"
        level_label = "Support"

    pattern_labels = {
        "symmetric": "Symmetric Triangle",
        "ascending": "Ascending Triangle",
        "descending": "Descending Triangle",
    }
    title = f"{pattern_labels.get(pattern, 'Triangle')} {'Breakout' if direction == 'bullish' else 'Breakdown'}"

    move_pct = (price - level) / level * 100
    target_pct = (target - price) / price * 100
    stop_pct = (stop_loss - price) / price * 100
    struct_pct = (struct_invalidation - price) / price * 100

    link = tradingview_link(symbol)
    vol_line = f"📊 Volume: {vol_ratio:.2f}x avg" if vol_ratio else ""
    return (
        f"{emoji} <b>{symbol}</b> — {title}\n"
        f"💰 Price: <code>{fmt_price(price)}</code>\n"
        f"📐 {level_label}: <code>{fmt_price(level)}</code> ({move_pct:+.2f}%)\n"
        f"🎯 Target: <code>{fmt_price(target)}</code> ({target_pct:+.2f}%)\n"
        f"🛑 Stop Loss: <code>{fmt_price(stop_loss)}</code> ({stop_pct:+.2f}%)\n"
        f"⚠️ Struct. fail: <code>{fmt_price(struct_invalidation)}</code> ({struct_pct:+.2f}%)\n"
        f"⚖️ R:R ≈ {rr:.2f}\n"
        f"⏱ Timeframe: 15m\n"
        f"{vol_line}\n"
        f'<a href="{link}">📈 Open chart</a>'
    )


# =========================
# 📊 GET DATA FROM OKX
# =========================
def get_data(symbol):
    url = "https://www.okx.com/api/v5/market/candles"

    params = {"instId": to_okx_inst(symbol), "bar": "15m", "limit": 200}

    res = session.get(url, params=params, timeout=10)
    data = res.json()

    if data.get("code") != "0" or not data.get("data"):
        return None  # ❗ no data

    df = pd.DataFrame(
        data["data"],
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "volCcy",
            "volCcyQuote",
            "confirm",
        ],
    )

    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)

    df = df.sort_values("time")

    return df


# =========================
# 📐 STRUCTURE
# =========================
def count_touches(df, level, col, tolerance=0.002):
    """Count how many candles tested `level` within tolerance.
    Use col='high' for resistance levels, col='low' for support levels.
    """
    touches = 0
    for price in df[col]:
        if abs(price - level) / level < tolerance:
            touches += 1
    return touches


def detect_trendlines(df, window=30):
    if len(df) < 3:
        return [], []
    highs = df["high"].tail(window).reset_index(drop=True)
    lows = df["low"].tail(window).reset_index(drop=True)

    high_points = []
    low_points = []

    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            high_points.append((i, highs[i]))

        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            low_points.append((i, lows[i]))

    return high_points, low_points


def detect_pattern(high_points, low_points):
    """
    Detects all 3 triangle types. Returns pattern name or None.
      symmetric  — falling highs + rising lows (converging)
      ascending  — flat highs + rising lows   (bullish bias)
      descending — falling highs + flat lows  (bearish bias)
    Slopes are normalised by price so thresholds work across all coins.
    """
    if len(high_points) < 2 or len(low_points) < 2:
        return None

    mid_price = (high_points[-1][1] + low_points[-1][1]) / 2
    h_slope = (high_points[-1][1] - high_points[0][1]) / (
        high_points[-1][0] - high_points[0][0] + 1e-6
    )
    l_slope = (low_points[-1][1] - low_points[0][1]) / (
        low_points[-1][0] - low_points[0][0] + 1e-6
    )

    h_pct = h_slope / mid_price
    l_pct = l_slope / mid_price

    FLAT = 0.00020  # < 0.02 % per candle = flat
    MOVING = 0.00008  # > 0.008% per candle = trending

    if h_pct < -MOVING and l_pct > MOVING:
        return "symmetric"
    if abs(h_pct) < FLAT and l_pct > MOVING:
        return "ascending"
    if h_pct < -MOVING and abs(l_pct) < FLAT:
        return "descending"

    return None


def atr(df, period=14):
    """Average True Range — measure of recent volatility per candle."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def last_closed_candle(df):
    """Return (candle, integer_position) for the most recent CLOSED candle."""
    if "confirm" in df.columns and len(df) >= 2:
        try:
            if str(df["confirm"].iloc[-1]) == "0":
                return df.iloc[-2], len(df) - 2
        except Exception:
            pass
    return df.iloc[-1], len(df) - 1


def triangle_breakout(df, high_points, low_points):
    """Require the LAST CLOSED candle to close beyond the trendline.
    Returns (direction, candle, idx) so callers reuse the candle fetch.
    """
    candle, idx = last_closed_candle(df)
    close_px = float(candle["close"])

    last_high = high_points[-1][1]
    last_low = low_points[-1][1]

    if close_px > last_high:
        return "bullish", candle, idx
    elif close_px < last_low:
        return "bearish", candle, idx

    return None, candle, idx


# =========================
# 🔁 LOOP
# =========================
if os.environ.get("STARTUP_ALERT", "1") != "0":
    send_alert("🚀 Bot started")
log.info("Bot started — watching %d symbols", len(symbols))

threading.Thread(target=poll_commands, daemon=True, name="cmd-poller").start()
log.info("Command listener started — send /status in Telegram to ping the bot")

while True:
    last_loop_time = datetime.now(timezone.utc)
    log.info("Loop running at %s", last_loop_time)
    print(f"Loop running at {last_loop_time}", flush=True)
    for symbol in symbols:
        try:
            df = get_data(symbol)

            if df is None or len(df) < 50:
                log.debug("Skipping %s — insufficient data", symbol)
                continue

            # ✅ STEP 1: DETECT TRENDLINES
            high_points, low_points = detect_trendlines(df)

            # ✅ STEP 2: ENSURE ENOUGH POINTS
            if len(high_points) < 2 or len(low_points) < 2:
                continue

            # ✅ STEP 3: DETECT PATTERN (symmetric, ascending, descending)
            pattern = detect_pattern(high_points, low_points)
            if pattern:
                direction, candle, closed_idx = triangle_breakout(
                    df, high_points, low_points
                )

                if direction in ("bullish", "bearish"):
                    close_px = float(candle["close"])
                    candle_vol = float(candle["volume"])
                    prior = df.iloc[max(0, closed_idx - VOL_LOOKBACK) : closed_idx]
                    avg_vol = float(prior["volume"].mean()) if len(prior) > 0 else 0.0
                    vol_ratio = candle_vol / avg_vol if avg_vol > 0 else 0.0

                    last_high = high_points[-1][1]
                    last_low = low_points[-1][1]
                    triangle_height = last_high - last_low

                    # Level-quality filter: breakout level must have been tested ≥ 2 times
                    # Use wicks (high/low) not closes — a wick touch confirms the level
                    breakout_level = last_high if direction == "bullish" else last_low
                    touch_col = "high" if direction == "bullish" else "low"
                    touches = count_touches(df, breakout_level, col=touch_col)
                    if touches < 2:
                        log.debug(
                            "%s %s [%s] — level only touched %d time(s) — skipping",
                            symbol,
                            direction,
                            pattern,
                            touches,
                        )
                        continue

                    atr_series = atr(df, period=14)
                    current_atr = (
                        float(atr_series.iloc[-2]) if len(atr_series) >= 2 else 0.0
                    )

                    # ATR filter 1: triangle must be at least 0.5x ATR (avoids micro chop)
                    if current_atr > 0 and triangle_height < 0.5 * current_atr:
                        ratio = triangle_height / current_atr
                        log.debug(
                            "%s %s [%s] — triangle too tight (%.2fx ATR) — skipping",
                            symbol,
                            direction,
                            pattern,
                            ratio,
                        )
                        continue

                    level_for_strength = (
                        last_high if direction == "bullish" else last_low
                    )
                    breakout_strength = abs(close_px - level_for_strength)

                    # ATR filter 2: breakout must clear level by at least 0.1x ATR
                    if current_atr > 0 and breakout_strength < 0.1 * current_atr:
                        ratio = breakout_strength / current_atr
                        log.debug(
                            "%s %s [%s] — weak breakout (%.2fx ATR) — skipping",
                            symbol,
                            direction,
                            pattern,
                            ratio,
                        )
                        continue

                    # Volume filter: breakout candle must be at least 1.1x average
                    if vol_ratio < 1.1:
                        log.debug(
                            "%s %s [%s] — weak volume (%.2fx) — skipping",
                            symbol,
                            direction,
                            pattern,
                            vol_ratio,
                        )
                        continue

                    key = f"{symbol}:{direction}"
                    now = time.time()
                    last = last_alert_time.get(key, 0)
                    if now - last < ALERT_COOLDOWN_SECONDS:
                        remaining = int((ALERT_COOLDOWN_SECONDS - (now - last)) / 60)
                        log.debug(
                            "Cooldown active for %s (%d min left) — skipping",
                            key,
                            remaining,
                        )
                        continue

                    price = close_px
                    candle_low = float(candle["low"])
                    candle_high = float(candle["high"])

                    if direction == "bullish":
                        level = last_high
                        target = last_high + triangle_height
                        stop_loss = candle_low  # tight stop: breakout candle low
                        struct_invalidation = last_low  # structural fail level
                    else:
                        level = last_low
                        target = last_low - triangle_height
                        stop_loss = candle_high  # tight stop: breakout candle high
                        struct_invalidation = last_high  # structural fail level

                    reward = abs(target - price)
                    risk = abs(price - stop_loss)
                    rr = reward / risk if risk > 0 else 0.0
                    if rr < MIN_RR:
                        log.debug(
                            "%s %s [%s] — R:R too low (%.2f) — skipping",
                            symbol,
                            direction,
                            pattern,
                            rr,
                        )
                        continue

                    send_alert(
                        build_alert(
                            symbol,
                            direction,
                            price,
                            level,
                            vol_ratio,
                            target,
                            stop_loss,
                            struct_invalidation,
                            triangle_height,
                            rr,
                            pattern,
                        )
                    )
                    log_signal(symbol, direction, price, target, stop_loss, rr, pattern)
                    last_alert_time[key] = now
                    record_alert(symbol, direction)

        except Exception as e:
            log.error("Error on %s: %s", symbol, e)

        time.sleep(0.15)

    try:
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            send_heartbeat()
            last_heartbeat = now

        maybe_send_daily_summary()
    except Exception as e:
        log.error("Post-scan error: %s", e, exc_info=True)

    log.info("Scanning...")
    time.sleep(420)
