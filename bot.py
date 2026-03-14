import sys
sys.stdout.reconfigure(line_buffering=True)

import hmac
import hashlib
import json
import time
import requests

from config import COINDCX_KEY, COINDCX_SECRET

# ================= API KEYS =================

API_KEY = COINDCX_KEY
API_SECRET = COINDCX_SECRET

print("BOT STARTED")

if not API_KEY:
    raise Exception("COINDCX_KEY environment variable not set")

if not API_SECRET:
    raise Exception("COINDCX_SECRET environment variable not set")

print("API KEY PRESENT:", True)
print("API SECRET PRESENT:", True)

BASE_URL = "https://api.coindcx.com"
PUBLIC_URL = "https://public.coindcx.com/market_data/candlesticks"

secret_bytes = bytes(API_SECRET, encoding="utf-8")


# ================= GET ACTIVE POSITIONS =================
def get_active_positions():

    print("Fetching positions...")

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

    response = requests.post(url, data=json_body, headers=headers)

    print("Positions API response received")

    return response.json()


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


# ================= GET CURRENT PRICE =================
def get_current_price(pair):

    params = {
        "pair": pair,
        "resolution": "1",
        "pcode": "f"
    }

    r = requests.get(PUBLIC_URL, params=params)

    if r.status_code != 200:
        return None

    data = r.json()

    if data["s"] != "ok":
        return None

    candles = data["data"]

    if not candles:
        return None

    return float(candles[-1]["close"])


# ================= MAIN LOOP =================
while True:

    print("\nChecking active positions...\n")

    try:
        positions = get_active_positions()
    except Exception as e:
        print("Error fetching positions:", str(e))
        time.sleep(300)
        continue

    if isinstance(positions, dict) and positions.get("status") == "error":
        print("API Error:", positions)
        time.sleep(300)
        continue

    if not isinstance(positions, list):
        print("Unexpected API response:", positions)
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

            side = "LONG" if active_pos > 0 else "SHORT"

            current_price = get_current_price(pair)

            if current_price is None:
                continue

            print("PAIR:", pair)
            print("SIDE:", side)
            print("ENTRY PRICE:", entry_price)
            print("CURRENT PRICE:", current_price)

            existing_sl = pos.get("stop_loss_trigger")

            if existing_sl is not None:
                existing_sl = float(existing_sl)

            print("Existing SL:", existing_sl)

            # ===== BREAK EVEN CONDITION =====
            if current_price < entry_price:

                new_sl = entry_price

                precision = len(str(entry_price).split(".")[1]) if "." in str(entry_price) else 0
                take_profit = round(entry_price * 0.90, precision)

                update_needed = False

                if existing_sl is None or existing_sl > entry_price:
                    update_needed = True

                if update_needed:

                    print("Moving SL to Break Even")
                    print("New SL:", new_sl)

                    result = update_tpsl(position_id, new_sl, take_profit)

                    print("API Response:", result)

                else:
                    print("SL already moved to break even")

            else:
                print("Price not below entry — skipping")

            print("---------------------------")

            time.sleep(1)

        except Exception as e:
            print("Error processing position:", str(e))

    print("Sleeping 5 minutes...\n")

    time.sleep(300)