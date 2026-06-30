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

  Topic de-dup ledger (FASE 6 selection):
    load_topics_ledger()                       -> (ledger, sha)
    recent_topic_tags(ledger, days=60)         -> set[str]
    domain_counts(ledger, days=7)              -> {domain: count}
    append_topic(ledger, sha, date_iso, tag, domain, level, source="auto")

  Paper ingestion (FASE 0, route A Google Drive):
    PAPERS_DRIVE_FOLDER                         -> str (Drive folder name)
    load_papers_state()                        -> (pstate, sha)
    paper_is_processed(pstate, file_id)        -> bool
    mark_paper_processed(pstate, sha, file_id, pill_id, title="")  # writes

  FASE-9 delivery via git + GitHub MCP (Claude Code on the web — writes are
  proxy-blocked; reads still work). Build everything in memory, then dump:
    append_topic_entry(ledger, date_iso, tag, domain, level, source="auto") -> ledger  # pure
    mark_paper_local(pstate, file_id, pill_id, title="")                     -> pstate  # pure
    build_delivery(out_dir, upserts, deletes=None)                           -> manifest_path
  then run .github/scripts/deliver_pr.sh and merge the PR via GitHub MCP.
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
    _raise_if_proxy_blocked(r)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def gh_delete(path: str, sha: str, msg: str) -> None:
    r = requests.delete(
        f"{_API}/{path}", headers=_HDR,
        json={"message": msg, "sha": sha, "branch": "main"},
        timeout=30,
    )
    _raise_if_proxy_blocked(r)
    r.raise_for_status()


def _raise_if_proxy_blocked(r) -> None:
    """Turn the Claude Code on the web egress-proxy write block into a clear,
    actionable error instead of an opaque 403.

    On the web sandbox, direct Contents-API writes (PUT/DELETE) are rejected by
    the proxy. The routine must NOT write from the sandbox: dump FASE-9 artifacts
    with build_delivery() and land them on main via deliver_pr.sh + a GitHub-MCP
    PR merge. Reads (gh_get/gh_list) are unaffected. Outside the web sandbox
    (e.g. GitHub Actions) this guard never fires."""
    if r.status_code == 403 and "not permitted through this proxy" in r.text:
        raise RuntimeError(
            "Direct GitHub Contents-API writes are blocked by the Claude Code "
            "on the web egress proxy. Do not write from the routine — dump "
            "FASE-9 artifacts with build_delivery() and deliver via "
            "deliver_pr.sh + a GitHub-MCP PR merge (see CLAUDE.md, "
            "'Write path on Claude Code on the web')."
        )


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
# Topic de-duplication ledger (FASE 6 — topic selection)
# topics_log.json: [{"date","tag","domain","level"}, ...]
# ────────────────────────────────────────────────────────────────────

import json as _json

# Drive folder (route A) scanned each morning for paper-derived pills.
PAPERS_DRIVE_FOLDER = "RadioLearn-Papers"


def load_topics_ledger():
    """Return (ledger_list, sha). ([], None) if the file is absent."""
    raw, sha = gh_get("topics_log.json")
    if raw is None:
        return [], None
    return _json.loads(raw), sha


def recent_topic_tags(ledger: list, days: int = 60,
                      as_of: Optional[str] = None) -> set:
    """Set of topic tags used within the last `days` (inclusive)."""
    cutoff = date.fromisoformat(as_of) if as_of else date.today()
    out = set()
    for e in ledger:
        try:
            d = date.fromisoformat(e.get("date", ""))
        except ValueError:
            continue
        if 0 <= (cutoff - d).days <= days:
            t = e.get("tag")
            if t:
                out.add(t)
    return out


def domain_counts(ledger: list, days: int = 7,
                  as_of: Optional[str] = None) -> dict:
    """{domain: count} over the last `days` — for the rotation quota."""
    cutoff = date.fromisoformat(as_of) if as_of else date.today()
    counts: dict = {}
    for e in ledger:
        try:
            d = date.fromisoformat(e.get("date", ""))
        except ValueError:
            continue
        if 0 <= (cutoff - d).days <= days:
            counts[e.get("domain", "?")] = counts.get(e.get("domain", "?"), 0) + 1
    return counts


def append_topic(ledger: list, sha, date_iso: str, tag: str,
                 domain: str, level, source: str = "auto"):
    """Idempotently record today's topic and persist. Returns (ledger, sha).

    `source`: "auto" (generated topic) or "paper" (paper-derived pill).
    Re-running the same day overwrites that day's entry (no duplicates).
    """
    ledger = [e for e in ledger if e.get("date") != date_iso]
    ledger.append({"date": date_iso, "tag": tag, "domain": domain,
                   "level": level, "source": source})
    ledger.sort(key=lambda e: e.get("date", ""))
    new_sha = gh_put_retry("topics_log.json", _json.dumps(ledger, indent=2),
                           f"Topic ledger {date_iso}: {tag}", sha)
    return ledger, new_sha


# ────────────────────────────────────────────────────────────────────
# Paper ingestion idempotency (FASE 0 — route A, Google Drive)
# papers_state.json: {"processed_file_ids": [...], "log": [...]}
# The session reads the PDF via the Drive MCP (search_files +
# download_file_content); this ledger only records what has been turned
# into a pill so a paper is never processed twice.
# ────────────────────────────────────────────────────────────────────

def load_papers_state():
    """Return (papers_state_dict, sha). Default skeleton if absent."""
    raw, sha = gh_get("papers_state.json")
    if raw is None:
        return {"processed_file_ids": [], "log": []}, None
    st = _json.loads(raw)
    st.setdefault("processed_file_ids", [])
    st.setdefault("log", [])
    return st, sha


def paper_is_processed(pstate: dict, file_id: str) -> bool:
    return file_id in pstate.get("processed_file_ids", [])


def mark_paper_processed(pstate: dict, sha, file_id: str, pill_id: str,
                         title: str = ""):
    """Record a paper as consumed and persist. Returns (pstate, sha)."""
    if file_id not in pstate["processed_file_ids"]:
        pstate["processed_file_ids"].append(file_id)
    pstate["log"].append({"file_id": file_id, "pill_id": pill_id,
                          "title": title, "date": date.today().isoformat()})
    new_sha = gh_put_retry("papers_state.json", _json.dumps(pstate, indent=2),
                           f"Paper consumed -> pill {pill_id}", sha)
    return pstate, new_sha


# ────────────────────────────────────────────────────────────────────
# FASE-9 delivery via git + GitHub MCP  (Claude Code on the web)
#
# The web egress proxy blocks direct Contents-API writes (PUT/DELETE -> 403
# "not permitted through this proxy"); reads still work. So the routine READS +
# COMPUTES with this library, builds ALL FASE-9 outputs in memory, dumps them
# with build_delivery(), and the session lands them on main with deliver_pr.sh
# (git) + a GitHub-MCP PR merge.
#
# Use the pure helpers below — append_topic_entry() / mark_paper_local() —
# instead of the writing append_topic() / mark_paper_processed(), so NOTHING in
# the routine touches the blocked write API. (append_topic/mark_paper_processed
# remain for GitHub-Actions contexts, which are not behind the proxy.)
# ────────────────────────────────────────────────────────────────────

def append_topic_entry(ledger: list, date_iso: str, tag: str, domain: str,
                       level, source: str = "auto") -> list:
    """Pure variant of append_topic — return an updated ledger, NO I/O.

    Re-running the same day overwrites that day's entry (no duplicates). Dump
    `json.dumps(ledger, indent=2)` as the topics_log.json upsert in the manifest.
    """
    ledger = [e for e in ledger if e.get("date") != date_iso]
    ledger.append({"date": date_iso, "tag": tag, "domain": domain,
                   "level": level, "source": source})
    ledger.sort(key=lambda e: e.get("date", ""))
    return ledger


def mark_paper_local(pstate: dict, file_id: str, pill_id: str,
                     title: str = "") -> dict:
    """Pure variant of mark_paper_processed — mutate+return pstate, NO I/O.

    Dump `json.dumps(pstate, indent=2)` as the papers_state.json upsert.
    """
    if file_id not in pstate.setdefault("processed_file_ids", []):
        pstate["processed_file_ids"].append(file_id)
    pstate.setdefault("log", []).append(
        {"file_id": file_id, "pill_id": pill_id, "title": title,
         "date": date.today().isoformat()})
    return pstate


def build_delivery(out_dir: str, upserts, deletes=None) -> str:
    """Write FASE-9 artifacts + a manifest.txt for deliver_pr.sh; return its path.

    upserts: list of (repo_path, content_str). Keep outbox/<pill_id>.json LAST
             for readability — the squash merge is atomic, so order is cosmetic.
    deletes: list of repo_path (e.g. processed inbox files).

    Then the session runs:
        bash .github/scripts/deliver_pr.sh <manifest> <work_branch> <msg_file>
    and merges the resulting PR to main via GitHub MCP (squash).
    """
    import os
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    for repo_path, content in upserts:
        local = os.path.join(out_dir, repo_path.replace("/", "__"))
        with open(local, "w", encoding="utf-8") as f:
            f.write(content)
        lines.append(f"UPSERT {repo_path} {local}")
    for repo_path in (deletes or []):
        lines.append(f"DELETE {repo_path}")
    manifest = os.path.join(out_dir, "manifest.txt")
    with open(manifest, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return manifest
