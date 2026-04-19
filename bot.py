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

print("TRAILING SL BOT STARTED")

if not API_KEY:
    raise Exception("COINDCX_KEY not set")

if not API_SECRET:
    raise Exception("COINDCX_SECRET not set")

BASE_URL = "https://api.coindcx.com"
PRICES_URL = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"

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


# ================= GET CURRENT PRICE =================
def get_current_price(pair):

    try:

        r = requests.get(PRICES_URL, timeout=10)

        if r.status_code != 200:
            return None

        data = r.json()

        pair_data = data.get("prices", {}).get(pair)

        if not pair_data:
            return None

        return float(pair_data.get("ls"))

    except Exception as e:

        print("Price fetch error:", e)
        return None


# ================= UPDATE SL (keeps TP untouched) =================
def update_sl(position_id, sl_price, existing_tp):

    timestamp = int(round(time.time() * 1000))

    body = {
        "timestamp": timestamp,
        "id": position_id,
        "stop_loss": {
            "stop_price": str(sl_price),
            "order_type": "stop_market"
        }
    }

    # Preserve existing TP — never create a new one, never remove one.
    if existing_tp is not None:
        body["take_profit"] = {
            "stop_price": str(existing_tp),
            "order_type": "take_profit_market"
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


# ================= TRAILING SL CALCULATION =================
def calculate_trailing_sl(side, entry_price, profit_percent, precision):
    """
    Returns target SL price based on floored profit level.
    Returns None if profit hasn't reached the first trigger (+1%).

    LONG (mirror for short):
      profit >= 1%   ->  SL = entry - 0.3%
      profit >= 2%   ->  SL = entry         (break-even)
      profit >= 3%   ->  SL = entry + 0.3%
      profit >= 4%   ->  SL = entry + 0.6%
      profit >= N%   ->  SL = entry + 0.3%*(N-2)
    """

    if profit_percent < 1:
        return None

    # Floor profit to integer level. 1.9% -> level 1, 2.0% -> level 2.
    level = int(profit_percent)

    # Percent offset from entry along the *favorable* direction.
    # level 1 -> -0.3 (losing side), level 2 -> 0, level 3 -> +0.3, level 4 -> +0.6 ...
    offset_percent = 0.3 * (level - 2)

    if side == "long":
        # Favorable direction is UP -> add offset
        new_sl = entry_price * (1 + offset_percent / 100)
    else:  # short
        # Favorable direction is DOWN -> subtract offset
        new_sl = entry_price * (1 - offset_percent / 100)

    return round(new_sl, precision)


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

            # Positive active_pos = LONG, negative = SHORT
            side = "long" if active_pos > 0 else "short"

            existing_sl = pos.get("stop_loss_trigger")
            existing_tp = pos.get("take_profit_trigger")

            if existing_sl is not None:
                existing_sl = float(existing_sl)

            if existing_tp is not None:
                existing_tp = float(existing_tp)

            current_price = get_current_price(pair)

            if current_price is None:
                print(f"{pair}: could not fetch price, skipping")
                continue

            # Profit % is direction-aware
            if side == "long":
                profit_percent = ((current_price - entry_price) / entry_price) * 100
            else:
                profit_percent = ((entry_price - current_price) / entry_price) * 100

            precision = len(str(entry_price).split(".")[1]) if "." in str(entry_price) else 0

            print(pair, "  [", side.upper(), "]")
            print("Entry:", entry_price)
            print("Price:", current_price)
            print("Profit:", round(profit_percent, 3), "%")
            print("Existing SL:", existing_sl)
            print("Existing TP:", existing_tp)

            candidate_sl = calculate_trailing_sl(side, entry_price, profit_percent, precision)

            if candidate_sl is None:
                print("Profit below 1% trigger, no SL update")
                print("---------------------")
                continue

            print("Candidate SL:", candidate_sl)

            # Only tighten SL, never loosen it.
            # LONG: higher SL is safer.  SHORT: lower SL is safer.
            should_update = False

            if existing_sl is None:
                should_update = True
            elif side == "long" and candidate_sl > existing_sl:
                should_update = True
            elif side == "short" and candidate_sl < existing_sl:
                should_update = True

            if should_update:
                print("Moving SL ->", candidate_sl)
                result = update_sl(position_id, candidate_sl, existing_tp)
                print("Update result:", result)
            else:
                print("Existing SL already equal/better, skipping")

            print("---------------------")

            time.sleep(1)

        except Exception as e:

            print("Error processing position:", e)

    print("Sleeping 5 minutes...\n")

    time.sleep(300)