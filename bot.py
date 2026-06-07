import requests
import pandas as pd
import time

# =========================
# 🔐 KEYS
# =========================
TELEGRAM_TOKEN = "8669291354:AAGhpgT8rnv4w5zUtoK3veDxPJtFkDKwqAc"
CHAT_ID = "5740206327"

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "TRUMPUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT", "AAVEUSDT", "TAOUSDT", "SUIUSDT"]

# =========================
# 📲 TELEGRAM
# =========================
def send_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# =========================
# 📊 GET DATA FROM BYBIT
# =========================
def get_data(symbol):
    url = "https://api.bybit.com/v5/market/kline"

    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "15",
        "limit": 200
    }

    res = requests.get(url, params=params)
    data = res.json()

    if "result" not in data or not data["result"]["list"]:
        return None  # ❗ no data

    df = pd.DataFrame(data["result"]["list"], columns=[
        "time","open","high","low","close","volume","turnover"
    ])

    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)

    df = df.sort_values("time")

    return df
# =========================
# 📐 STRUCTURE
# =========================
def detect_structure(df):
    resistance = df['high'].rolling(20).max()
    support = df['low'].rolling(20).min()
    return support, resistance

def count_touches(df, level, tolerance=0.002):
    touches = 0
    for price in df['close']:
        if abs(price - level) / level < tolerance:
            touches += 1
    return touches

# =========================
# 🚀 BREAKOUT
# =========================
def breakout(df, resistance):
    latest = df['close'].iloc[-1]
    prev_res = resistance.iloc[-2]
    return latest > prev_res * 1.002

def volume_confirm(df):
    avg_vol = df['volume'].rolling(20).mean().iloc[-1]
    latest_vol = df['volume'].iloc[-1]
    return latest_vol > avg_vol * 1.5

def detect_trendlines(df):
    highs = df['high'].tail(20).reset_index(drop=True)
    lows = df['low'].tail(20).reset_index(drop=True)

    high_points = []
    low_points = []

    for i in range(1, len(highs)-1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            high_points.append((i, highs[i]))

        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            low_points.append((i, lows[i]))

    return high_points, low_points


def is_triangle(high_points, low_points):
    if len(high_points) < 2 or len(low_points) < 2:
        return False

    h_slope = (high_points[-1][1] - high_points[0][1]) / (high_points[-1][0] - high_points[0][0] + 1e-6)
    l_slope = (low_points[-1][1] - low_points[0][1]) / (low_points[-1][0] - low_points[0][0] + 1e-6)

    return h_slope < 0 and l_slope > 0


def triangle_breakout(df, high_points, low_points):
    latest_close = df['close'].iloc[-1]

    last_high = high_points[-1][1]
    last_low = low_points[-1][1]

    if latest_close > last_high:
        return "bullish"
    elif latest_close < last_low:
        return "bearish"

    return None
# =========================
# 🔁 LOOP
# =========================
send_alert("Bot is live 🚀")
while True:
    for symbol in symbols:
        try:
            df = get_data(symbol)

            if df is None or len(df) < 50:
                print(f"Skipping {symbol}")
                continue

            # ✅ STEP 1: DETECT TRENDLINES
            high_points, low_points = detect_trendlines(df)

            # ✅ STEP 2: ENSURE ENOUGH POINTS
            if len(high_points) < 2 or len(low_points) < 2:
                continue

            # ✅ STEP 3: CHECK TRIANGLE
            if is_triangle(high_points, low_points):
                direction = triangle_breakout(df, high_points, low_points)

                if direction == "bearish":
                    send_alert(f"{symbol} 🔻 Triangle Breakdown detected")

                elif direction == "bullish":
                    send_alert(f"{symbol} 🔺 Triangle Breakout detected")

        except Exception as e:
            print(f"Error on {symbol}: {e}")

    print("Scanning...")
    time.sleep(420)