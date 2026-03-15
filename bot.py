import sys
sys.stdout.reconfigure(line_buffering=True)

import hmac
import hashlib
import json
import time
import requests

from config import COINDCX_KEY, COINDCX_SECRET

API_KEY = COINDCX_KEY
API_SECRET = COINDCX_SECRET

print("BOT STARTED")

if not API_KEY:
    raise Exception("COINDCX_KEY not set")

if not API_SECRET:
    raise Exception("COINDCX_SECRET not set")

BASE_URL = "https://api.coindcx.com"
PUBLIC_URL = "https://public.coindcx.com/market_data/candlesticks"

secret_bytes = bytes(API_SECRET, encoding="utf-8")


# ================= GET POSITIONS =================
def get_active_positions():

    timestamp = int(round(time.time() * 1000))

    body = {
        "timestamp": timestamp,
        "page": "1",
        "size": "50",
        "margin_currency_short_name": ["USDT"]
    }

    json_body = json.dumps(body, separators=(',', ':'))

    signature = hmac.new(
        secret_bytes,
        json_body.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": signature
    }

    url = BASE_URL + "/exchange/v1/derivatives/futures/positions"

    r = requests.post(url, data=json_body, headers=headers)

    return r.json()


# ================= EMA =================
def get_ema_200(pair):

    now = int(time.time())

    params = {
        "pair": pair,
        "from": now - 720000,
        "to": now,
        "resolution": "15",
        "pcode": "f"
    }

    r = requests.get(PUBLIC_URL, params=params)

    if r.status_code != 200:
        return None, None

    data = r.json()

    if data["s"] != "ok":
        return None, None

    candles = sorted(data["data"], key=lambda x: x["time"])

    closes = [float(c["close"]) for c in candles]

    if len(closes) < 200:
        return None, None

    period = 200
    multiplier = 2 / (period + 1)

    ema = sum(closes[:period]) / period

    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema

    current_price = closes[-1]

    return current_price, ema


# ================= UPDATE TPSL =================
def update_tpsl(position_id, sl_price, tp_price):

    timestamp = int(round(time.time() * 1000))

    body = {
        "timestamp": timestamp,
        "id": position_id,
        "take_profit": {
            "stop_price": str(tp_price),
            "order_type": "take_profit_market"
        },
        "stop_loss": {
            "stop_price": str(sl_price),
            "order_type": "stop_market"
        }
    }

    json_body = json.dumps(body, separators=(',', ':'))

    signature = hmac.new(
        secret_bytes,
        json_body.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": signature
    }

    url = BASE_URL + "/exchange/v1/derivatives/futures/positions/create_tpsl"

    r = requests.post(url, data=json_body, headers=headers)

    return r.json()


# ================= MAIN LOOP =================
while True:

    print("\nChecking active positions...\n")

    positions = get_active_positions()

    if not isinstance(positions, list):
        print("API error:", positions)
        time.sleep(300)
        continue

    for pos in positions:

        try:

            active_pos = float(pos.get("active_pos", 0))

            if active_pos == 0:
                continue

            pair = pos["pair"]
            entry_price = float(pos["avg_price"])
            position_id = pos["id"]

            existing_sl = pos.get("stop_loss_trigger")
            existing_tp = pos.get("take_profit_trigger")

            if existing_sl:
                existing_sl = float(existing_sl)

            if existing_tp:
                existing_tp = float(existing_tp)

            current_price, ema = get_ema_200(pair)

            if current_price is None:
                continue

            profit_percent = ((entry_price - current_price) / entry_price) * 100

            precision = len(str(entry_price).split(".")[1]) if "." in str(entry_price) else 0

            print(pair)
            print("Entry:", entry_price)
            print("Price:", current_price)
            print("Profit:", round(profit_percent, 3), "%")
            print("Existing SL:", existing_sl)
            print("Existing TP:", existing_tp)

            # ===== STAGE 1 BREAK EVEN =====
            if profit_percent >= 3:

                new_sl = entry_price

                if existing_sl is None or existing_sl > new_sl:

                    print("Moving SL → Break Even")

                    update_tpsl(position_id, new_sl, existing_tp)

                    time.sleep(1)
                    continue

            # ===== STAGE 2 LOCK PROFIT =====
            if profit_percent >= 5:

                candidate_sl = round(entry_price * 0.97, precision)

                if existing_sl and candidate_sl < existing_sl:

                    print("Locking 3% Profit")

                    update_tpsl(position_id, candidate_sl, existing_tp)

                    time.sleep(1)
                    continue

            # ===== STAGE 3 EMA TRAILING =====
            if profit_percent >= 6 and ema:

                candidate_sl = round(ema * 1.032, precision)

                if existing_sl and candidate_sl < existing_sl and candidate_sl < entry_price:

                    print("Trailing SL → EMA")

                    update_tpsl(position_id, candidate_sl, existing_tp)

            print("---------------------")

            time.sleep(1)

        except Exception as e:

            print("Error processing position:", e)

    print("Sleeping 5 minutes...\n")

    time.sleep(300)