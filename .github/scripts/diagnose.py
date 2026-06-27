"""One-off Telegram diagnostics — workflow_dispatch only.

READ-ONLY: never advances the poller offset, never writes telegram_state.json,
never prints the bot token. Answers the open question from the 2026-06-27 audit
("answers stopped being credited after 2026-06-16"): is a webhook stealing the
callbacks, is this the right bot, and are taps actually reaching getUpdates?

Best signal: tap a quiz button on Telegram, then run this workflow within a
minute (the live poller runs every 15 min and may otherwise confirm the update
first).
"""
import os
import json
import urllib.parse
import urllib.request
import urllib.error

BOT_TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def call(method: str, params: dict | None = None) -> dict:
    url = f"{API}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error_code": e.code,
                "description": e.read().decode("utf-8", errors="replace")}


def show(title: str, resp: dict) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(resp, ensure_ascii=False, indent=2)[:4000])


# 1. Identity — confirm the token maps to the expected bot.
show("getMe", call("getMe"))

# 2. Webhook — a set webhook silently diverts every update away from
#    getUpdates, so the poller would see nothing while taps still "work".
wi = call("getWebhookInfo")
show("getWebhookInfo", wi)
res = wi.get("result") or {}
hook = res.get("url") if wi.get("ok") else None
pending = res.get("pending_update_count")
if hook:
    print(f"\n*** WEBHOOK IS SET ({hook!r}). This is almost certainly the cause: "
          f"it consumes callbacks before getUpdates can. Fix: call deleteWebhook "
          f"(or setWebhook with an empty url) to restore long-polling. ***")
else:
    print("\nNo webhook set — getUpdates long-polling is the active delivery path.")
print(f"pending_update_count (awaiting getUpdates) = {pending}")

# 3. Stored offset for comparison.
try:
    with open("telegram_state.json", encoding="utf-8") as f:
        stored = json.load(f).get("last_update_id", 0)
except FileNotFoundError:
    stored = 0
print(f"Stored last_update_id = {stored}")

# 4. Read-only peek at the latest update (offset=-1 does NOT confirm/advance).
peek = call("getUpdates", {"offset": -1, "timeout": 0,
                           "allowed_updates": json.dumps(["callback_query"])})
show("getUpdates offset=-1 (latest, read-only peek)", peek)

ups = peek.get("result") or []
if ups:
    u = ups[-1]
    cb = u.get("callback_query")
    gap = u["update_id"] - stored
    print(f"\nLatest update_id={u['update_id']} | stored offset={stored} | gap={gap}")
    print(f"callback_data = {(cb.get('data') if cb else None)!r}")
    if cb and gap > 0:
        print("→ A callback IS reaching the bot but sits ahead of the stored "
              "offset: delivery works; investigate capture/commit in poll.py.")
    elif cb:
        print("→ A callback is present and already within the confirmed range.")
else:
    print("\nNo callback_query visible. If you just tapped a button and this is "
          "empty: either a webhook consumed it (see getWebhookInfo) or the tap "
          "did not reach THIS bot (wrong bot/chat).")
