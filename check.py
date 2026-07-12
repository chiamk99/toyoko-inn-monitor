#!/usr/bin/env python3
import json
import os
import ssl
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SECRETS_PATH = BASE_DIR / "secrets.json"
STATE_PATH = BASE_DIR / "state.json"


def get_webhook_url():
    env_value = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_value:
        return env_value
    if SECRETS_PATH.exists():
        return json.loads(SECRETS_PATH.read_text())["webhook_url"]
    raise RuntimeError("No webhook URL found: set DISCORD_WEBHOOK_URL or create secrets.json")

try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()


def to_china_utc_iso(date_str):
    y, m, d = map(int, date_str.split("-"))
    dt = datetime(y, m, d, tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_prices(config):
    codes = [h["code"] for h in config["hotels"]]
    payload = {
        "0": {
            "json": {
                "hotelCodes": codes,
                "checkinDate": to_china_utc_iso(config["checkin"]),
                "checkoutDate": to_china_utc_iso(config["checkout"]),
                "numberOfPeople": config["people"],
                "numberOfRoom": config["room"],
                "smokingType": config["smoking"],
            },
            "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
        }
    }
    url = (
        "https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices"
        "?batch=1&input=" + urllib.parse.quote(json.dumps(payload, separators=(",", ":")))
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as resp:
        data = json.load(resp)
    return data[0]["result"]["data"]["json"]["prices"]


def hotel_detail_url(config, code):
    return (
        f"https://www.toyoko-inn.com/china/search/detail/{code}/"
        f"?people={config['people']}&room={config['room']}&smoking={config['smoking']}"
        f"&start={config['checkin']}&end={config['checkout']}"
    )


def notify_discord(webhook_url, content):
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT):
        pass


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def main():
    config = json.loads(CONFIG_PATH.read_text())
    webhook_url = get_webhook_url()
    state = load_state()

    prices = fetch_prices(config)

    newly_available = []
    for hotel in config["hotels"]:
        code = hotel["code"]
        info = prices.get(code, {})
        available = bool(info.get("existEnoughVacantRooms")) or info.get("lowestPrice", 0) > 0
        was_available = state.get(code, {}).get("available", False)

        if available and not was_available:
            newly_available.append((hotel, info))

        state[code] = {"available": available, "lowestPrice": info.get("lowestPrice", 0)}

    save_state(state)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if newly_available:
        lines = [f"🏨 **有房通知**（{config['checkin']} ~ {config['checkout']}）"]
        for hotel, info in newly_available:
            price = info.get("lowestPrice", 0)
            price_text = f"¥{price}〜" if price else "有空房"
            lines.append(f"- {hotel['name']}：{price_text}\n  {hotel_detail_url(config, hotel['code'])}")
        message = "\n".join(lines)
        notify_discord(webhook_url, message)
        print(f"[{timestamp}] Sent notification for {len(newly_available)} hotel(s).")
    else:
        print(f"[{timestamp}] No new vacancies.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
