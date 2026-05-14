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
    new_card()                                -> dict (fresh card)
    update_card(state, card_id, quality)      -> mutates state, returns card
    cards_due(state, as_of=None)              -> [card_ids]
    cards_overdue_without_answer(state, ans)  -> [card_ids]
    select_review_candidate(state)            -> card_id | None
"""

import base64
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


def update_card(state: dict, card_id: str, quality: int) -> dict:
    """Apply SM-2 update. Quality clamped to [0,5]. Mutates state in place."""
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


def select_review_candidate(state: dict) -> Optional[str]:
    """Pick stalest due card: oldest next_review, lowest EF as tiebreak."""
    due = cards_due(state)
    if not due:
        return None
    due.sort(key=lambda cid: (state["cards"][cid]["next_review"],
                              state["cards"][cid]["ef"]))
    return due[0]
