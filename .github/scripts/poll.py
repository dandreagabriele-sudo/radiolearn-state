"""
Receiver script — polling Telegram.

Chiama getUpdates con offset = last_update_id + 1, processa ogni callback_query,
risponde con answerCallbackQuery (toglie lo spinner), e accoda le risposte in
inbox/<YYYY-MM-DD>.json. Aggiorna telegram_state.json con il nuovo last_update_id.

Formato di inbox/<YYYY-MM-DD>.json (append-only):
[
  {
    "update_id": 123456789,
    "received_at": "2026-05-13T15:23:01+00:00",
    "card_id": "20260512:0",
    "pill_id": "20260512",
    "question_idx": 0,
    "quality": 4
  }
]

Il telegram_state.json è OWNED da questo script: la routine cloud non lo modifica mai.
"""
import os
import re
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "telegram_state.json"

# Formato callback_data atteso: q|YYYYMMDD|<question_idx>|<quality>
CB_PATTERN = re.compile(r"^q\|(\d{8})\|(\d+)\|([0-5])$")


def call(method: str, params: dict | None = None, post: bool = False) -> dict:
    url = f"{API_BASE}/{method}"
    if post:
        req = urllib.request.Request(
            url,
            data=json.dumps(params or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {method}: {body}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_update_id": 0}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_to_inbox(date_iso: str, entry: dict) -> None:
    os.makedirs("inbox", exist_ok=True)
    path = f"inbox/{date_iso}.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []
    data.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    state = load_state()
    print(f"Starting from last_update_id = {state['last_update_id']}")

    result = call("getUpdates", {
        "offset": state["last_update_id"] + 1,
        "timeout": 25,
        "allowed_updates": json.dumps(["callback_query"]),
    })

    if not result.get("ok"):
        print(f"getUpdates failed: {result}")
        return 1

    updates = result.get("result", [])
    print(f"Received {len(updates)} update(s).")

    max_id = state["last_update_id"]
    accepted = 0
    skipped = 0

    for upd in updates:
        max_id = max(max_id, upd["update_id"])
        cb = upd.get("callback_query")
        if not cb:
            skipped += 1
            continue

        data = cb.get("data", "")
        m = CB_PATTERN.match(data)
        if not m:
            print(f"  ⚠ Malformed callback_data: {data!r}")
            try:
                call("answerCallbackQuery",
                     {"callback_query_id": cb["id"], "text": "⚠ Formato non valido"},
                     post=True)
            except RuntimeError as e:
                print(f"    answerCallbackQuery failed: {e}")
            skipped += 1
            continue

        pill_id, qidx_str, quality_str = m.group(1), m.group(2), m.group(3)
        qidx, quality = int(qidx_str), int(quality_str)
        card_id = f"{pill_id}:{qidx}"

        # Ack al volo all'utente (toglie lo spinner sul bottone)
        try:
            call("answerCallbackQuery", {
                "callback_query_id": cb["id"],
                "text": f"✓ Registrato (q={quality}) — vedrai il nuovo intervallo domani",
                "show_alert": False,
            }, post=True)
        except RuntimeError as e:
            # Il callback potrebbe essere troppo vecchio (>24h); non bloccare
            print(f"  ⚠ answerCallbackQuery for {card_id}: {e}")

        # Append all'inbox
        date_iso = datetime.fromtimestamp(
            cb.get("message", {}).get("date", upd.get("update_id", 0)),
            tz=timezone.utc
        ).date().isoformat()

        append_to_inbox(date_iso, {
            "update_id": upd["update_id"],
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "card_id": card_id,
            "pill_id": pill_id,
            "question_idx": qidx,
            "quality": quality,
        })
        accepted += 1
        print(f"  ✓ {card_id} q={quality} → inbox/{date_iso}.json")

    state["last_update_id"] = max_id
    state["last_poll_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(state)

    print(f"Summary: {accepted} accepted, {skipped} skipped, new last_update_id={max_id}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
