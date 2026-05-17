"""
Sender script — drena outbox/ verso Telegram.

Per ogni file in outbox/, esegue in ordine le chiamate API Telegram contenute
nel campo `messages`. Se TUTTE riescono, il file viene cancellato e il commit
finale (nel workflow) registra la consegna. Se anche una fallisce, il file
resta in outbox/ per essere ritentato al prossimo trigger.

I messaggi `sendMessage` con `text` oltre il limite Telegram (4096 caratteri)
vengono spezzati automaticamente in più chunk consecutivi, tagliando su
confini naturali (paragrafo > riga > spazio) e preservando la formattazione
MarkdownV2. La `reply_markup` (tastiera inline) resta solo sull'ULTIMO chunk.

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
import re
import sys
import json
import time
import glob
import urllib.request
import urllib.error

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Limite duro Telegram per sendMessage.text. Sopra questo valore, HTTP 400.
TELEGRAM_TEXT_LIMIT = 4096
# Soglia di sicurezza per ogni chunk: lascia margine per emoji multi-byte
# e per piccole differenze tra len(Python) e conteggio UTF-16 di Telegram.
SAFE_CHUNK_SIZE = 3900


# ---------------------------------------------------------------------------
# Splitter per messaggi troppo lunghi
# ---------------------------------------------------------------------------

def _strip_md_escapes(s: str) -> str:
    """Rimuove le sequenze di escape MarkdownV2 (\\X) per il conteggio marker."""
    return re.sub(r"\\.", "", s)


def _markdownv2_balanced(s: str) -> bool:
    """True se i marker di formattazione MarkdownV2 sono accoppiati nel chunk.

    Telegram fa il parsing di ogni messaggio in modo INDIPENDENTE: un *bold*
    aperto qui e chiuso nel chunk successivo viene rifiutato con HTTP 400.
    Conta i marker dopo aver rimosso le sequenze di escape.
    """
    cleaned = _strip_md_escapes(s)
    for marker in ("*", "_", "~", "`", "|"):
        if cleaned.count(marker) % 2 != 0:
            return False
    return True


def _split_text(text: str, max_len: int = SAFE_CHUNK_SIZE) -> list:
    """Spezza `text` in chunk <= max_len, preferendo confini naturali.

    Strategia in ordine di preferenza:
      1. Doppio newline (fine paragrafo)  →  taglio più pulito
      2. Singolo newline (fine riga)
      3. Spazio (fine parola)
      4. Taglio duro (solo se la riga è priva di spazi e > max_len)

    Dopo ogni split, verifica che il chunk abbia marker MarkdownV2 bilanciati;
    se non lo è, retrocede al precedente `\\n\\n` (più sicuro).
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    # Soglia minima per evitare chunk degeneri di pochi caratteri all'inizio.
    min_acceptable = max_len // 2

    while len(remaining) > max_len:
        cut = -1
        consume = 0  # caratteri del separatore da "mangiare" (non duplicare)

        # Tentativo 1: doppio newline
        idx = remaining.rfind("\n\n", min_acceptable, max_len)
        if idx != -1:
            cut, consume = idx, 2
        else:
            # Tentativo 2: newline singolo
            idx = remaining.rfind("\n", min_acceptable, max_len)
            if idx != -1:
                cut, consume = idx, 1
            else:
                # Tentativo 3: spazio
                idx = remaining.rfind(" ", min_acceptable, max_len)
                if idx != -1:
                    cut, consume = idx, 1
                else:
                    # Taglio duro: caso degenere (riga unica enorme)
                    cut, consume = max_len, 0

        chunk = remaining[:cut]

        # Difesa MarkdownV2: se i marker sono sbilanciati, retrocede.
        if not _markdownv2_balanced(chunk):
            fallback = chunk.rfind("\n\n")
            if fallback > min_acceptable // 2:
                cut, consume = fallback, 2
                chunk = remaining[:cut]
            # Se nemmeno il fallback è bilanciato, lasciamo correre:
            # con i contenuti reali (sezioni separate da \n\n) non capita.

        chunks.append(chunk)
        remaining = remaining[cut + consume:]

    if remaining:
        chunks.append(remaining)

    return chunks


def expand_long_messages(messages: list) -> list:
    """Espande la lista sostituendo ogni sendMessage troppo lungo con più
    sendMessage consecutivi. Altri metodi (sendPhoto, sendDocument...) passano
    inalterati: `caption` ha un limite suo (1024) ma è raramente sforato e
    Telegram in quel caso restituisce un errore più gestibile.

    Politica `reply_markup`: la tastiera inline resta solo sull'ULTIMO chunk
    del testo spezzato. Così, per esempio, i bottoni di auto-valutazione delle
    Q&A compaiono dopo l'intera domanda anche se molto lunga.
    """
    expanded = []

    for msg in messages:
        method = msg.get("method")
        params = msg.get("params", {})

        if method != "sendMessage":
            expanded.append(msg)
            continue

        text = params.get("text", "")
        if len(text) <= TELEGRAM_TEXT_LIMIT:
            expanded.append(msg)
            continue

        # Split necessario.
        chunks = _split_text(text)
        reply_markup = params.get("reply_markup")

        for j, chunk in enumerate(chunks):
            new_params = {
                k: v for k, v in params.items()
                if k not in ("text", "reply_markup")
            }
            new_params["text"] = chunk
            # reply_markup solo sull'ultimo chunk
            if j == len(chunks) - 1 and reply_markup is not None:
                new_params["reply_markup"] = reply_markup
            expanded.append({"method": "sendMessage", "params": new_params})

    return expanded


# ---------------------------------------------------------------------------
# Chiamate Telegram
# ---------------------------------------------------------------------------

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

    # Espande eventuali sendMessage troppo lunghi in più chunk.
    original_count = len(messages)
    messages = expand_long_messages(messages)
    if len(messages) != original_count:
        print(f"  ℹ  Split {original_count} → {len(messages)} message(s) per limite Telegram (4096 char)")

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
