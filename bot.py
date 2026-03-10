from config import COINDCX_KEY, COINDCX_SECRET

API_KEY = COINDCX_KEY
API_SECRET = COINDCX_SECRET


import hmac
import hashlib
import json
import time
import requests

# ================= API KEYS =================
API_KEY = "XXXX"
API_SECRET = "YYYY"

BASE_URL = "https://api.coindcx.com"
PUBLIC_URL = "https://public.coindcx.com/market_data/candlesticks"

secret_bytes = bytes(API_SECRET, encoding="utf-8")


# ================= GET ACTIVE POSITIONS =================
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

    response = requests.post(url, data=json_body, headers=headers)

    return response.json()


# ================= EMA CALCULATION =================
def get_ema_200(pair):

    now = int(time.time())

    params = {
        "pair": pair,
        "from": now - (360000),
        "to": now,
        "resolution": "15",
        "pcode": "f"
    }

    r = requests.get(PUBLIC_URL, params=params)

    if r.status_code != 200:
        return None, None, None

    data = r.json()

    if data["s"] != "ok":
        return None, None, None

    candles = sorted(data["data"], key=lambda x: x["time"])

    closes = [float(c["close"]) for c in candles]

    if len(closes) < 200:
        return None, None, None

    period = 200
    multiplier = 2 / (period + 1)

    ema = sum(closes[:period]) / period

    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema

    current_price = closes[-1]

    price_str = str(current_price)

    if "." in price_str:
        precision = len(price_str.split(".")[1])
    else:
        precision = 0

    ema = round(ema, precision)

    stop_loss = round(ema * 1.01, precision)

    return current_price, ema, stop_loss


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

    for pos in positions:

        active_pos = float(pos.get("active_pos", 0))

        if active_pos == 0:
            continue

        pair = pos["pair"]
        entry_price = float(pos["avg_price"])
        position_id = pos["id"]

        side = "LONG" if active_pos > 0 else "SHORT"

        current_price, ema, new_sl = get_ema_200(pair)

        if current_price is None:
            continue

        print("PAIR:", pair)
        print("SIDE:", side)
        print("CURRENT PRICE:", current_price)
        print("EMA200:", ema)

        # ===== EXISTING SL =====
        existing_sl = pos.get("stop_loss_trigger")

        if existing_sl is not None:
            existing_sl = float(existing_sl)

        print("Existing SL:", existing_sl)
        print("New SL:", new_sl)

        # ===== CONDITION =====
        if current_price < ema:

            precision = len(str(entry_price).split(".")[1]) if "." in str(entry_price) else 0
            take_profit = round(entry_price * 0.90, precision)

            update_needed = False

            if existing_sl is None or existing_sl == 0:
                update_needed = True

            elif new_sl < existing_sl:
                update_needed = True

            if update_needed:

                print("Updating TPSL")
                print("TP:", take_profit)

                result = update_tpsl(position_id, new_sl, take_profit)

                print("API Response:", result)

            else:
                print("New SL is higher than existing SL — skipping update")

        else:
            print("Price above EMA — skipping")

        print("---------------------------")

    print("Sleeping 5 minutes...\n")

    time.sleep(300)