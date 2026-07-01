"""One-off Telegram diagnostics — workflow_dispatch only.

NON-DESTRUCTIVE: never writes telegram_state.json, never prints the bot token,
and — crucially — never consumes pending updates. The peek uses a POSITIVE
offset (stored last_update_id + 1), exactly what the real poller would fetch, so
a tapped-but-uncredited answer stays in Telegram's queue for the next poll.

  ⚠️  Do NOT reintroduce getUpdates(offset=-1) here. Telegram documents a
  negative offset as "all previous updates will be forgotten": it MUTATES the
  server-side queue and DROPS the pending answer you are trying to inspect.
  That was the original bug (2026-07-01): "READ-ONLY" referred only to our
  stored offset, but offset=-1 still forgot updates server-side, so every
  tap-then-diagnose test destroyed the very answer it was checking — which made
  the crediting look permanently broken when the poller was actually fine.

Answers the recurring question: is a webhook stealing the callbacks, is this the
right bot, and are taps actually reaching getUpdates? Tap a quiz button, then
run this workflow — timing no longer matters, the peek won't eat the tap.
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

# 4. Non-destructive peek at what the poller will fetch next.
#    Use a POSITIVE offset (stored + 1) — NEVER offset=-1. offset=stored+1 only
#    confirms ids <= stored (already confirmed → no-op) and returns every pending
#    update the next real poll would return, WITHOUT forgetting anything. Safe to
#    run anytime, even the instant after a tap. (offset=-1 would forget the
#    pending answer — see the module docstring.)
peek = call("getUpdates", {"offset": stored + 1, "timeout": 0,
                           "allowed_updates": json.dumps(["callback_query"])})
show(f"getUpdates offset={stored + 1} (pending, non-destructive peek)", peek)

ups = peek.get("result") or []
if ups:
    print(f"\n{len(ups)} pending update(s) queued for the poller "
          f"(stored offset={stored}):")
    for u in ups:
        cb = u.get("callback_query")
        print(f"  update_id={u['update_id']} "
              f"callback_data={(cb.get('data') if cb else None)!r}")
    print("→ Delivery works and these are queued: the next poll-telegram run "
          "captures them. This peek did NOT consume them (positive offset).")
else:
    print("\nNo pending callback_query. If you just tapped a button and this is "
          "empty, the tap is not reaching THIS bot's getUpdates queue: check "
          "getWebhookInfo above (a webhook diverts every update), confirm you "
          "tapped a message actually sent by THIS bot (right bot/chat), or a "
          "second getUpdates consumer on the same token drained it first.")
