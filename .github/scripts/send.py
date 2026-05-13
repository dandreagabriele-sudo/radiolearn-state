"""
Sender script — drena outbox/ verso Telegram.

Per ogni file in outbox/, esegue in ordine le chiamate API Telegram contenute
nel campo `messages`. Se TUTTE riescono, il file viene cancellato e il commit
finale (nel workflow) registra la consegna. Se anche una fallisce, il file
resta in outbox/ per essere ritentato al prossimo trigger.

Formato atteso di ogni outbox/<id>.json:
{
  "pill_id": "20260513",
  "created_at": "2026-05-13T08:40:00+02:00",
  "messages": [
    {
      "method": "sendMessage",
      "params": {"chat_id": "8538175163", "text": "...", "parse_mode": "MarkdownV2"}
    },
    {
      "method": "sendPhoto",
      "params": {"chat_id": "8538175163", "photo": "https://...", "caption": "..."}
    }
  ]
}
"""
import os
import sys
import json
import time
import glob
import urllib.request
import urllib.error

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def call(method: str, params: dict) -> dict:
    """Invia una chiamata POST a Telegram. Solleva RuntimeError su errore."""
    req = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {method}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error on {method}: {e}")

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false on {method}: {data}")
    return data


def process_file(path: str) -> bool:
    """Processa un singolo file di outbox. Ritorna True se tutto consegnato."""
    print(f"\n📤 Processing {path}")
    try:
        with open(path, encoding="utf-8") as f:
            envelope = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ✗ Cannot parse: {e}")
        return False

    messages = envelope.get("messages", [])
    if not messages:
        print(f"  ⚠ No messages in envelope, treating as done")
        return True

    for i, msg in enumerate(messages, 1):
        method = msg.get("method")
        params = msg.get("params", {})
        if not method:
            print(f"  ✗ Message {i}: missing 'method' field")
            return False
        try:
            result = call(method, params)
            mid = result.get("result", {}).get("message_id", "?")
            print(f"  ✓ [{i}/{len(messages)}] {method} → message_id={mid}")
        except RuntimeError as e:
            print(f"  ✗ [{i}/{len(messages)}] {method}: {e}")
            return False

        time.sleep(0.4)  # rispetto del rate limit Telegram (~30 msg/sec max)

    return True


def main() -> int:
    paths = sorted(glob.glob("outbox/*.json"))
    if not paths:
        print("Outbox is empty.")
        return 0

    print(f"Found {len(paths)} file(s) in outbox.")
    failures = []

    for path in paths:
        ok = process_file(path)
        if ok:
            os.remove(path)
            print(f"  🗑  Deleted {path}")
        else:
            failures.append(path)
            print(f"  ⏸  Kept {path} for retry")

    print(f"\nSummary: {len(paths) - len(failures)} delivered, {len(failures)} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
