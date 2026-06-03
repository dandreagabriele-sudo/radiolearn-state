"""
RadioLearn library — GitHub Contents API helpers + SM-2 engine (Wozniak 1987).

Fetched and exec()'d by the daily routine at bootstrap. The routine prompt
relies on the public API documented below. Do NOT remove or rename functions
without updating the prompt.

Public API:
  Bootstrap:
    init_github(token, repo="dandreagabriele-sudo/radiolearn-state")

  GitHub Contents API:
    gh_get(path)                              -> (content_str, sha) | (None, None)
    gh_list(folder)                           -> list of {name, sha, path, type}
    gh_put(path, content_str, msg, sha=None)  -> new_sha
    gh_delete(path, sha, msg)                 -> None

  SM-2 (in-memory; persistence is caller's responsibility):
    new_card()                                          -> dict (fresh card)
    update_card(state, card_id, quality, source="user") -> mutates, returns card
    cards_due(state, as_of=None)                        -> [card_ids]
    cards_overdue_without_answer(state, ans)            -> [card_ids]
    select_review_candidates(state, k=3)                -> [card_ids] (≤ k)
    select_review_candidate(state)                      -> card_id | None (legacy)

  Telegram MarkdownV2 helpers:
    esc(text)                                 -> str (escaped)
    link(text, url)                           -> str (MDV2 link)
    quiz_keyboard(pill_id, qidx)              -> dict (inline kb)

  Robust PUT:
    gh_put_retry(path, content, msg, sha=None) -> new_sha

  Pill Q&A sidecar (anti-placeholder safety net, see CLAUDE.md FASE 6a/8):
    pill_qa_path(pill_date)                   -> "pills_log/<date>.qa.json"
    gh_get_pill_qa(pill_date)                 -> (dict|None, sha|None)
    gh_get_pill_question(pill_date, qidx)     -> dict | None
    gh_put_pill_qa(pill_date, qa_obj, msg)    -> new_sha
    assert_no_placeholders(envelope, sentinels=DEFAULT_PLACEHOLDER_SENTINELS)
"""

import base64
import json
import re
import requests
from datetime import date, timedelta
from typing import Optional

# ────────────────────────────────────────────────────────────────────
# GitHub Contents API
# ────────────────────────────────────────────────────────────────────

_API: Optional[str] = None
_HDR: Optional[dict] = None


def init_github(token: str,
                repo: str = "dandreagabriele-sudo/radiolearn-state") -> None:
    """Configure module-level GitHub client. Must be called once at bootstrap."""
    global _API, _HDR
    _API = f"https://api.github.com/repos/{repo}/contents"
    _HDR = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(path: str):
    """Returns (content_str, sha) or (None, None) if 404."""
    r = requests.get(f"{_API}/{path}?ref=main", headers=_HDR, timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json()
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def gh_list(folder: str) -> list:
    """List items in folder; [] if folder is absent."""
    r = requests.get(f"{_API}/{folder}?ref=main", headers=_HDR, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def gh_put(path: str, content_str: str, msg: str,
           sha: Optional[str] = None) -> str:
    """Create or update a file. `sha` required for updates, omitted for new."""
    payload = {
        "message": msg,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha is not None:
        payload["sha"] = sha
    r = requests.put(f"{_API}/{path}", headers=_HDR, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def gh_delete(path: str, sha: str, msg: str) -> None:
    r = requests.delete(
        f"{_API}/{path}", headers=_HDR,
        json={"message": msg, "sha": sha, "branch": "main"},
        timeout=30,
    )
    r.raise_for_status()


# ────────────────────────────────────────────────────────────────────
# SM-2 engine (Wozniak 1987)
# In-memory mutations only. Persistence is the caller's job.
# ────────────────────────────────────────────────────────────────────

def new_card() -> dict:
    return {
        "ef": 2.5,
        "interval": 0,
        "repetitions": 0,
        "next_review": date.today().isoformat(),
        "last_quality": None,
        "history": [],
    }


def update_card(state: dict, card_id: str, quality: int,
                source: str = "user") -> dict:
    """Apply SM-2 update. Quality clamped to [0,5]. Mutates state in place.

    `source` tags the resulting history entry:
      - "user"  → genuine user-supplied callback (default; backward compat).
      - "reset" → FASE 3 forgotten-card reset (no real user answer).
    The weekly summary (FASE 5) MUST count only `source == "user"` entries
    for "Quiz risposti" and "Qualità media"; otherwise resets pollute the
    completion rate (100 %) and crush the average quality (→ 0.0).
    """
    if card_id not in state["cards"]:
        state["cards"][card_id] = new_card()
    card = state["cards"][card_id]
    quality = max(0, min(5, int(quality)))

    if quality < 3:
        # Forgotten: reset repetitions and interval
        card["repetitions"] = 0
        card["interval"] = 1
    else:
        if card["repetitions"] == 0:
            card["interval"] = 1
        elif card["repetitions"] == 1:
            card["interval"] = 6
        else:
            card["interval"] = max(1, round(card["interval"] * card["ef"]))
        card["repetitions"] += 1

    # Easiness Factor update (Wozniak 1987 formula)
    new_ef = card["ef"] + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    card["ef"] = max(1.3, round(new_ef, 4))

    card["next_review"] = (date.today() + timedelta(days=card["interval"])).isoformat()
    card["last_quality"] = quality
    card["history"].append({
        "date": date.today().isoformat(),
        "quality": quality,
        "ef_after": card["ef"],
        "interval_after": card["interval"],
        "source": source,
    })
    return card


def cards_due(state: dict, as_of: Optional[str] = None) -> list:
    cutoff = as_of or date.today().isoformat()
    return [cid for cid, c in state["cards"].items() if c["next_review"] <= cutoff]


def cards_overdue_without_answer(state: dict, answered: set) -> list:
    """Due cards not answered in this session."""
    today = date.today().isoformat()
    return [cid for cid, c in state["cards"].items()
            if c["next_review"] < today and cid not in answered]


def select_review_candidates(state: dict, k: int = 3) -> list:
    """Pick up to k stalest due cards.

    Ranking key per card: (next_review asc, ef asc). The most stale +
    spiniest card is at index 0. Returns [] if no card is due. Used by
    FASE 6a (bundle ripasso) to include 2–3 review questions per pill.
    """
    due = cards_due(state)
    if not due:
        return []
    due.sort(key=lambda cid: (state["cards"][cid]["next_review"],
                              state["cards"][cid]["ef"]))
    return due[:max(0, int(k))]


def select_review_candidate(state: dict) -> Optional[str]:
    """Pick stalest due card. Kept for backward compatibility.

    Prefer `select_review_candidates(state, k)` in new code.
    """
    picks = select_review_candidates(state, k=1)
    return picks[0] if picks else None


# ────────────────────────────────────────────────────────────────────
# Telegram MarkdownV2 helpers
# ────────────────────────────────────────────────────────────────────

_MDV2_SPECIAL = set(r'_*[]()~`>#+-=|{}.!\\')


def esc(text: str) -> str:
    """Escape MarkdownV2 special characters for Telegram message text.

    Escape the full Telegram MarkdownV2 special set:
        _ * [ ] ( ) ~ ` > # + - = | { } . ! \\
    Inside `[label](url)` links, escape only the label (use `link()`).
    """
    return "".join("\\" + c if c in _MDV2_SPECIAL else c for c in text)


def link(text: str, url: str) -> str:
    """Build a MarkdownV2 inline link.

    Escapes the label with `esc()` and the closing paren / backslash in
    the URL (the only chars Telegram requires escaping inside link URLs).
    """
    safe_url = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{esc(text)}]({safe_url})"


def quiz_keyboard(pill_id: str, qidx: int) -> dict:
    """0-5 self-assessment inline keyboard for a quiz question.

    callback_data format: `q|<pill_id>|<qidx>|<quality>` (matches poll.py).
    """
    return {"inline_keyboard": [
        [
            {"text": "5 ✅", "callback_data": f"q|{pill_id}|{qidx}|5"},
            {"text": "4",        "callback_data": f"q|{pill_id}|{qidx}|4"},
            {"text": "3",        "callback_data": f"q|{pill_id}|{qidx}|3"},
        ],
        [
            {"text": "2",        "callback_data": f"q|{pill_id}|{qidx}|2"},
            {"text": "1",        "callback_data": f"q|{pill_id}|{qidx}|1"},
            {"text": "0 ❌", "callback_data": f"q|{pill_id}|{qidx}|0"},
        ],
    ]}


# ────────────────────────────────────────────────────────────────────
# GitHub PUT with one retry on 409/422 (refetch sha) and 5xx (sleep 3s)
# ────────────────────────────────────────────────────────────────────

import time as _time


def gh_put_retry(path: str, content_str: str, msg: str,
                 sha: Optional[str] = None) -> str:
    """`gh_put` with one retry: refresh sha on 409/422, sleep 3s on 5xx."""
    for attempt in range(2):
        try:
            return gh_put(path, content_str, msg, sha)
        except requests.HTTPError as e:
            sc = e.response.status_code
            if sc in (409, 422) and attempt == 0:
                _, sha = gh_get(path)
                continue
            if 500 <= sc < 600 and attempt == 0:
                _time.sleep(3)
                continue
            raise


# ────────────────────────────────────────────────────────────────────
# Pill Q&A sidecar — structured Q&A persisted alongside pills_log/*.md
#
# Background: FASE 6a (ripasso bundle) used to hard-code review question
# templates by card_id inside the daily script, with an `else` branch that
# emitted a "Domanda ripasso non disponibile" placeholder. When FASE 3 reset
# the predicted candidates, the live `select_review_candidates` returned
# different card_ids and the placeholder branch fired (bug surfaced 2026-06-03).
#
# The structural fix is to make every pill's Q&A retrievable as machine-readable
# JSON so future ripassi can compose questions DYNAMICALLY from the original
# pill, plus a validator that refuses to ship an envelope containing known
# placeholder sentinels.
# ────────────────────────────────────────────────────────────────────

# Expected shape of a Q&A object (one entry per question):
#   {
#       "card_id":   "20260603:0",
#       "qidx":      0,
#       "title":     "Pattern LGE dell'amiloidosi cardiaca",
#       "body":      "Donna 72 a, IC a frazione di eiezione preservata, ...",
#       "opts":      {"A": "...", "B": "...", "C": "...", "D": "..."},
#       "answer":    "A",
#       "rationale": "L'amiloidosi cardiaca infiltra ..."
#   }
#
# Expected shape of the sidecar object (one file per pill):
#   {
#       "pill_id":  "20260603",
#       "date":     "2026-06-03",
#       "topic":    "Late Gadolinium Enhancement (LGE) ...",
#       "domain":   "Torace / Cuore — RM cardiaca — Livello 1 (esperto)",
#       "questions": [ <qa_obj>, ... ]  # only the NEW questions; not the ripassi
#   }


def pill_qa_path(pill_date: str) -> str:
    """Path of the structured Q&A sidecar for `pills_log/<pill_date>.md`."""
    return f"pills_log/{pill_date}.qa.json"


def gh_get_pill_qa(pill_date: str):
    """Fetch the structured Q&A sidecar for a pill.

    Returns (qa_dict, sha). (None, None) if the sidecar is absent (legacy pill).
    The caller is responsible for the fallback path (raw .md parsing, or
    aborting the review for that card with a warning — never silently shipping
    a placeholder).
    """
    content, sha = gh_get(pill_qa_path(pill_date))
    if content is None:
        return None, None
    try:
        return json.loads(content), sha
    except json.JSONDecodeError:
        return None, sha


def gh_get_pill_question(pill_date: str, qidx: int):
    """Return the single Q&A dict for `pill_date` and `qidx`, or None.

    None means: sidecar missing, malformed, or qidx out of range. Caller MUST
    NOT substitute a placeholder — instead, log a warning and skip the card
    from the bundle (then select_review_candidates can pick the next stalest).
    """
    qa, _ = gh_get_pill_qa(pill_date)
    if qa is None:
        return None
    questions = qa.get("questions") or []
    for q in questions:
        if int(q.get("qidx", -1)) == int(qidx):
            return q
    return None


def gh_put_pill_qa(pill_date: str, qa_obj: dict, msg: str) -> str:
    """Create or overwrite the sidecar (refreshing sha via gh_put_retry)."""
    path = pill_qa_path(pill_date)
    _, sha = gh_get(path)
    return gh_put_retry(path, json.dumps(qa_obj, indent=2, ensure_ascii=False),
                        msg, sha)


# Default sentinels that indicate a placeholder slipped past the composer.
# Keep them lowercase; the validator does a case-insensitive substring search.
DEFAULT_PLACEHOLDER_SENTINELS = (
    "domanda ripasso non disponibile",
    "domanda non disponibile",
    "placeholder",
    "todo",
    "lorem ipsum",
)


def _walk_strings(obj):
    """Yield every string contained in `obj` (dict/list/str/anything else)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def assert_no_placeholders(envelope: dict,
                           sentinels=DEFAULT_PLACEHOLDER_SENTINELS,
                           empty_option_marker: str = "—") -> None:
    """Raise ValueError if `envelope` contains any placeholder sentinel.

    Also catches the canonical "A) — / B) — / ..." pattern (empty option text
    used by the 2026-06-03 placeholder). Run this BEFORE persisting the
    outbox envelope in FASE 9; it is cheaper to abort and re-compose than to
    ship placeholder Q&As to Telegram and try to recall them after the fact.
    """
    sentinels_lc = tuple(s.lower() for s in sentinels)
    for s in _walk_strings(envelope):
        s_lc = s.lower()
        for sent in sentinels_lc:
            if sent in s_lc:
                raise ValueError(
                    f"assert_no_placeholders: found sentinel '{sent}' in "
                    f"envelope message text: {s[:200]!r}"
                )
        # Detect "A) — / B) — / C) — / D) —" style (≥ 3 empty options)
        pattern = re.compile(
            r"(?m)^\s*[A-D]\\?\)\s*" + re.escape(empty_option_marker) + r"\s*$"
        )
        matches = pattern.findall(s)
        if len(matches) >= 3:
            raise ValueError(
                "assert_no_placeholders: found ≥3 empty option lines "
                f"('A) {empty_option_marker}' style) in message text — "
                "likely a fallback placeholder."
            )
