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
INSTRUMENT_URL = BASE_URL + "/exchange/v1/derivatives/futures/data/instrument"

secret_bytes = bytes(API_SECRET, encoding="utf-8")

# Cache of pair -> price_increment (tick size). Instruments rarely change,
# so we fetch once per pair and reuse.
TICK_CACHE = {}


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


# ================= GET PRICE INCREMENT (tick size) =================
def get_price_increment(pair):
    """
    Returns the instrument's price_increment (e.g. 0.01, 0.000001).
    Cached per pair.
    """

    if pair in TICK_CACHE:
        return TICK_CACHE[pair]

    try:
        params = {"pair": pair, "margin_currency_short_name": "USDT"}
        r = requests.get(INSTRUMENT_URL, params=params, timeout=10)

        if r.status_code != 200:
            print(f"Instrument fetch error {pair}: HTTP {r.status_code}")
            return None

        data = r.json()
        tick = float(data["instrument"]["price_increment"])

        TICK_CACHE[pair] = tick
        return tick

    except Exception as e:
        print(f"Instrument fetch error {pair}: {e}")
        return None


# ================= TICK-ALIGNMENT HELPERS =================
def tick_decimals(tick):
    """Number of decimal places implied by the tick size."""
    s = f"{tick:.12f}".rstrip("0").rstrip(".")
    if "." in s:
        return len(s.split(".")[1])
    return 0


def align_to_tick(price, tick):
    """
    Snap price to an exact multiple of tick and return it as a fixed-decimal
    STRING with exactly tick_decimals(tick) digits after the point.
    Returning a string (not float) prevents re-introducing float artifacts
    when the caller does str(price).
    """
    if tick is None or tick <= 0:
        return None

    steps = round(price / tick)
    decimals = tick_decimals(tick)
    snapped = steps * tick

    return f"{snapped:.{decimals}f}"


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
def update_sl(position_id, sl_price_str, existing_tp_str):
    """
    sl_price_str and existing_tp_str must already be tick-aligned strings.
    """

    timestamp = int(round(time.time() * 1000))

    body = {
        "timestamp": timestamp,
        "id": position_id,
        "stop_loss": {
            "stop_price": sl_price_str,
            "order_type": "stop_market"
        }
    }

    # Preserve existing TP — never create a new one, never remove one.
    if existing_tp_str is not None:
        body["take_profit"] = {
            "stop_price": existing_tp_str,
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
def calculate_trailing_sl(side, entry_price, profit_percent):
    """
    Returns target SL price (float) based on floored profit level, or None
    if profit hasn't reached the first trigger (+1%).

    LONG (mirror for short):
      profit >= 1%  ->  SL = entry - 0.3%
      profit >= 2%  ->  SL = entry          (break-even)
      profit >= 3%  ->  SL = entry + 0.3%
      profit >= 4%  ->  SL = entry + 0.6%
      profit >= N%  ->  SL = entry + 0.3%*(N-2)
    """

    if profit_percent < 1:
        return None

    level = int(profit_percent)
    offset_percent = 0.3 * (level - 2)

    if side == "long":
        # Favorable direction is UP -> add offset
        new_sl = entry_price * (1 + offset_percent / 100)
    else:
        # Favorable direction is DOWN -> subtract offset
        new_sl = entry_price * (1 - offset_percent / 100)

    return new_sl


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

            # Get the instrument's real tick size — NEVER infer it from entry_price.
            tick = get_price_increment(pair)

            if tick is None:
                print(f"{pair}: could not fetch tick size, skipping")
                continue

            print(pair, "  [", side.upper(), "]")
            print("Entry:", entry_price)
            print("Price:", current_price)
            print("Tick :", tick)
            print("Profit:", round(profit_percent, 3), "%")
            print("Existing SL:", existing_sl)
            print("Existing TP:", existing_tp)

            # ===== INITIAL TP/SL =====
            # If the position has NEITHER a TP nor an SL, seed them:
            #   TP = +5% profit,  SL = -3% loss (direction-aware).
            # Then skip trailing this cycle — next run will see the TP we
            # just set, leave it untouched, and trail the SL as profit grows.
            if existing_tp is None and existing_sl is None:

                if side == "long":
                    raw_initial_tp = entry_price * 1.05  # +5% profit -> price up
                    raw_initial_sl = entry_price * 0.97  # -3% loss  -> price down
                else:  # short
                    raw_initial_tp = entry_price * 0.95  # +5% profit -> price down
                    raw_initial_sl = entry_price * 1.03  # -3% loss  -> price up

                initial_tp_str = align_to_tick(raw_initial_tp, tick)
                initial_sl_str = align_to_tick(raw_initial_sl, tick)

                print("No TP/SL set — initializing  TP:", initial_tp_str, " SL:", initial_sl_str)
                result = update_sl(position_id, initial_sl_str, initial_tp_str)
                print("Init result:", result)
                print("---------------------")
                time.sleep(1)
                continue

            raw_sl = calculate_trailing_sl(side, entry_price, profit_percent)

            if raw_sl is None:
                print("Profit below 1% trigger, no SL update")
                print("---------------------")
                continue

            # Snap SL to a valid tick multiple and keep it as a string for sending.
            sl_price_str = align_to_tick(raw_sl, tick)
            candidate_sl = float(sl_price_str)

            print("Candidate SL:", sl_price_str)

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
                # Re-snap existing_tp to the same tick grid to avoid float-drift
                # corrupting it when we echo it back to the API.
                existing_tp_str = None
                if existing_tp is not None:
                    existing_tp_str = align_to_tick(existing_tp, tick)

                print("Moving SL ->", sl_price_str)
                result = update_sl(position_id, sl_price_str, existing_tp_str)
                print("Update result:", result)
            else:
                print("Existing SL already equal/better, skipping")

            print("---------------------")

            time.sleep(1)

        except Exception as e:
            print("Error processing position:", e)

    print("Sleeping 5 minutes...\n")

    time.sleep(300)