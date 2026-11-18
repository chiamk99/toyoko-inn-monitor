#!/usr/bin/env python3
import json
import os
import ssl
import sys
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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


def room_plan_url(config, stay, code):
    return (
        "https://www.toyoko-inn.com/china/search/result/room_plan/"
        f"?hotel={code}&people={config['people']}&room={config['room']}"
        f"&smoking={config['smoking']}&start={stay['checkin']}&end={stay['checkout']}"
    )


def extract_json_object(html, key):
    marker = f'"{key}":'
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    return None


def fetch_available_plans(config, stay, code):
    url = room_plan_url(config, stay, code)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    plan_response = extract_json_object(html, "planResponse")
    if not plan_response:
        return []

    found = []
    for room_type in plan_response.get("roomTypeList", []):
        is_smoking = room_type.get("specs", {}).get("isSmoking", False)
        for plan in room_type.get("plans", []):
            vacant = plan.get("vacant", {})
            general = vacant.get("generalVacantRoom", 0)
            membership = vacant.get("membershipVacantRoom", 0)
            if general > 0 or membership > 0:
                price = plan["price"]["generalPrice"] if general > 0 else plan["price"]["membershipPrice"]
                found.append(
                    {
                        "room_type": room_type.get("roomTypeName", ""),
                        "plan_name": plan.get("planName", ""),
                        "price": price,
                        "member_only": general == 0 and membership > 0,
                        "is_smoking": is_smoking,
                    }
                )
    return found


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


def plan_key(p):
    smoking_label = "吸菸" if p["is_smoking"] else "禁菸"
    return f"{p['room_type']}|{smoking_label}|{p['plan_name']}"


def safe_fetch(config, stay, code):
    try:
        return fetch_available_plans(config, stay, code)
    except Exception as e:
        print(f"fetch failed for {code}: {e}", file=sys.stderr)
        return None


def send_chunked(webhook_url, header, lines):
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            notify_discord(webhook_url, chunk)
            chunk = header
        chunk += "\n" + line
    notify_discord(webhook_url, chunk)


def main():
    config = json.loads(CONFIG_PATH.read_text())
    webhook_url = get_webhook_url()
    state = load_state()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_notified = 0
    failures = 0

    for stay in config["stays"]:
        stay_key = f"{stay['checkin']}_{stay['checkout']}"
        stay_state = state.setdefault(stay_key, {})

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda h: safe_fetch(config, stay, h["code"]), config["hotels"])
            )

        newly_available = []
        for hotel, plans in zip(config["hotels"], results):
            code = hotel["code"]
            if plans is None:
                failures += 1
                continue
            previously_available = set(stay_state.get(code, {}))
            new_plans = [p for p in plans if plan_key(p) not in previously_available]
            if new_plans:
                newly_available.append((hotel, new_plans))
            stay_state[code] = {plan_key(p): p["price"] for p in plans}

        if newly_available:
            lines = []
            for hotel, plans in newly_available:
                details = "、".join(
                    f"{p['room_type']}{'🚬' if p['is_smoking'] else '🚭'}¥{p['price']}"
                    f"{'🔒會員' if p['member_only'] else ''}"
                    for p in plans
                )
                lines.append(f"- {hotel['name']}：{details}\n  {room_plan_url(config, stay, hotel['code'])}")
            send_chunked(
                webhook_url,
                f"🏨 **有房通知**（{stay['checkin']} ~ {stay['checkout']}）",
                lines,
            )
            total_notified += sum(len(plans) for _, plans in newly_available)
            print(f"[{timestamp}] Sent notification for {len(newly_available)} hotel(s) ({stay_key}).")

    save_state(state)

    if total_notified == 0:
        print(f"[{timestamp}] No new vacancies." + (f" ({failures} fetch failures)" if failures else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
