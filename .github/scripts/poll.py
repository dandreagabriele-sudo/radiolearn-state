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
    start_offset = state["last_update_id"] + 1
    print(f"Starting from last_update_id = {state['last_update_id']} "
          f"(offset={start_offset})")

    result = call("getUpdates", {
        "offset": start_offset,
        "timeout": 25,
        "allowed_updates": json.dumps(["callback_query"]),
    })

    if not result.get("ok"):
        print(f"getUpdates failed: {result}")
        return 1

    updates = result.get("result", [])
    print(f"Received {len(updates)} update(s).")

    # Process strictly in ascending update_id order so the offset only ever
    # moves forward *past updates we have fully handled*. `confirmed_through`
    # is the highest update_id that is either (a) durably written to the inbox
    # working tree, or (b) genuinely non-actionable (non-callback / unparseable
    # — retrying can't help). If an inbox write fails we STOP advancing and let
    # the next run re-fetch, so a real answer is never stepped over and lost.
    # (Previously `max_id` advanced for every update unconditionally, which made
    # any skipped/failed update an irreversible loss once Telegram dropped it.)
    updates.sort(key=lambda u: u["update_id"])
    confirmed_through = state["last_update_id"]
    accepted = malformed = noncb = 0

    for upd in updates:
        uid = upd["update_id"]
        cb = upd.get("callback_query")
        # Raw visibility for diagnostics (never logs the bot token):
        print(f"  update_id={uid} keys={sorted(k for k in upd if k != 'update_id')} "
              f"data={(cb.get('data') if cb else None)!r}")

        if not cb:
            # allowed_updates should exclude these, but be defensive: a
            # non-callback can never become an answer, so it is safe to pass.
            noncb += 1
            confirmed_through = uid
            continue

        data = cb.get("data", "")
        m = CB_PATTERN.match(data)
        if not m:
            print(f"  ⚠ Malformed callback_data: {data!r} (update {uid}) — "
                  f"acking and stepping past (unparseable, retry can't help)")
            try:
                call("answerCallbackQuery",
                     {"callback_query_id": cb["id"], "text": "⚠ Formato non valido"},
                     post=True)
            except RuntimeError as e:
                print(f"    answerCallbackQuery failed: {e}")
            malformed += 1
            confirmed_through = uid   # don't let a poison-pill block the queue
            continue

        pill_id, qidx_str, quality_str = m.group(1), m.group(2), m.group(3)
        qidx, quality = int(qidx_str), int(quality_str)
        card_id = f"{pill_id}:{qidx}"

        # Bucket by the pill message's date when available; fall back to *now*
        # (NOT update_id — that was a latent bug bucketing into ~1997).
        msg_ts = cb.get("message", {}).get("date")
        when = (datetime.fromtimestamp(msg_ts, tz=timezone.utc)
                if msg_ts else datetime.now(timezone.utc))
        date_iso = when.date().isoformat()

        # Persist FIRST, then ack, then advance the offset — so a write failure
        # never advances past an answer we did not actually capture.
        try:
            append_to_inbox(date_iso, {
                "update_id": uid,
                "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "card_id": card_id,
                "pill_id": pill_id,
                "question_idx": qidx,
                "quality": quality,
            })
        except OSError as e:
            print(f"  ✗ inbox write failed for {card_id} (update {uid}): {e} "
                  f"— STOP advancing; will retry next run")
            break

        # Ack al volo (toglie lo spinner). Non-fatal if the callback is too old.
        try:
            call("answerCallbackQuery", {
                "callback_query_id": cb["id"],
                "text": f"✓ Registrato (q={quality}) — nuovo intervallo domani",
                "show_alert": False,
            }, post=True)
        except RuntimeError as e:
            print(f"  ⚠ answerCallbackQuery for {card_id}: {e}")

        accepted += 1
        confirmed_through = uid
        print(f"  ✓ {card_id} q={quality} → inbox/{date_iso}.json")

    state["last_update_id"] = confirmed_through
    state["last_poll_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(state)

    print(f"Summary: {accepted} accepted, {malformed} malformed, "
          f"{noncb} non-callback, new last_update_id={confirmed_through}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
