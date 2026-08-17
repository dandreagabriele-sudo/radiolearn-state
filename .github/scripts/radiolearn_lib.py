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
    recent_topic_tags(ledger, days=None)       -> set[str]  (None = every tag ever)
    domain_counts(ledger, days=7)              -> {domain: count}
    append_topic(ledger, sha, date_iso, tag, domain, level, source="auto")

  Paper ingestion (FASE 0, route A Google Drive):
    PAPERS_DRIVE_FOLDER                         -> str (Drive folder name)
    load_papers_state()                        -> (pstate, sha)
    paper_is_processed(pstate, file_id)        -> bool
    mark_paper_processed(pstate, sha, file_id, pill_id, title="")  # writes

  Google-Form answer channel (FASE 2-bis — reliable path since 2026-07-05):
    load_forms_state()                         -> (fstate, sha)
    parse_form_csv(csv_text)                   -> [{"timestamp","card_id","quality"}]
    form_row_key(row)                          -> str (idempotency key)
    mark_form_rows_local(fstate, keys)         -> fstate  # pure

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
_BLOB: Optional[str] = None
_HDR: Optional[dict] = None


def init_github(token: str,
                repo: str = "dandreagabriele-sudo/radiolearn-state") -> None:
    """Configure module-level GitHub client. Must be called once at bootstrap."""
    global _API, _BLOB, _HDR
    _API = f"https://api.github.com/repos/{repo}/contents"
    _BLOB = f"https://api.github.com/repos/{repo}/git/blobs"
    _HDR = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(path: str):
    """Returns (content_str, sha) or (None, None) if 404.

    Large-file safety (CRITICAL): the Contents API only inlines files up to
    1 MB — above that it returns ``encoding="none"`` with an EMPTY ``content``.
    Decoding that empty string yields "" and a caller like
    ``state = json.loads(raw) if raw else {default}`` would silently load a
    FRESH state and WIPE every card. ``sm2_state.json`` crossed 1 MB in
    Aug 2026, so this path is live. When the inline content is missing we
    transparently refetch the raw blob via the git blobs API (supports up to
    100 MB, always base64), so gh_get never returns a truncated file.
    """
    r = requests.get(f"{_API}/{path}?ref=main", headers=_HDR, timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json()
    if j.get("encoding") == "base64" and j.get("content"):
        return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]
    # File >1 MB: no inline content. Fetch the full blob by its sha.
    br = requests.get(f"{_BLOB}/{j['sha']}", headers=_HDR, timeout=60)
    br.raise_for_status()
    bj = br.json()
    return base64.b64decode(bj["content"]).decode("utf-8"), j["sha"]


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
    """DEPRECATED — do NOT use for FASE 3. Use cards_unanswered_after_presentation().

    Returns every due card not answered in this session, including the ~280
    cards the routine never showed the user. Feeding this to FASE 3 punished
    unasked cards with quality=0 and destroyed the whole deck (bug found
    2026-08-16 — see cards_unanswered_after_presentation for the analysis).
    Kept only so old tooling keeps importing.
    """
    today = date.today().isoformat()
    return [cid for cid, c in state["cards"].items()
            if c["next_review"] < today and cid not in answered]


def _card_pill_date(card_id: str) -> Optional[str]:
    """ISO date encoded in a card_id ("YYYYMMDD:N" -> "YYYY-MM-DD"), else None."""
    prefix = card_id.split(":")[0]
    if len(prefix) == 8 and prefix.isdigit():
        return f"{prefix[:4]}-{prefix[4:6]}-{prefix[6:8]}"
    return None


def card_last_presented(state: dict, card_id: str) -> Optional[str]:
    """ISO date the card was last shown to the user, or None if never.

    Falls back to the pill date encoded in the card_id, so the cards that
    predate `last_presented` tracking still answer the question correctly.
    """
    c = state["cards"].get(card_id, {})
    return c.get("last_presented") or _card_pill_date(card_id)


def mark_presented(state: dict, card_ids, day: Optional[str] = None) -> dict:
    """Record that these cards were delivered in today's pill. Mutates + returns state.

    FASE 6 MUST call this for EVERY card in the pill — the new questions AND
    the ripasso cards (which reuse the original card_id, so their
    re-presentation is otherwise invisible). Without it FASE 3 cannot tell
    "asked and ignored" from "never asked", which is exactly the bug that
    flattened the deck.
    """
    day = day or date.today().isoformat()
    for cid in card_ids:
        if cid not in state["cards"]:
            state["cards"][cid] = new_card()
        state["cards"][cid]["last_presented"] = day
    return state


def cards_unanswered_after_presentation(state: dict, grace_days: int = 3,
                                        as_of: Optional[str] = None) -> list:
    """Cards genuinely forgotten: shown to the user, then left unanswered.

    This is the FASE-3 selector. It replaces cards_overdue_without_answer(),
    which returned every overdue card — including the ~280 per day the routine
    never presented. Resetting those with quality=0 drove EF to the 1.3 floor,
    pinned repetitions at 0 and interval at 1 on all 338 cards, and left the
    stalest-first selector re-proposing the same six May cards forever, so a
    perfect Google-Form score could not advance anything (2026-08-16).

    A card is returned only when ALL of these hold:
      * it was actually presented at least `grace_days` ago;
      * no genuine (source="user") answer arrived on or after that presentation;
      * it has not already been penalised for that same presentation.

    The third clause bounds the penalty to ONE reset per unanswered pill,
    instead of one every two days for the rest of the card's life. It is
    tracked with the explicit `penalised_for` field (set by
    apply_forgotten_reset) rather than by scanning for reset rows in history,
    so that pruning or re-tagging history never silently re-arms the penalty —
    and so FASE-5's "Carte dimenticate" count stays independent of it. The
    history scan is kept only as a fallback for cards last touched by the old
    code path.
    """
    today = date.fromisoformat(as_of) if as_of else date.today()
    out = []
    for cid, c in state["cards"].items():
        lp = c.get("last_presented") or _card_pill_date(cid)
        if not lp:
            continue
        try:
            presented = date.fromisoformat(lp)
        except ValueError:
            continue
        if (today - presented).days < grace_days:
            continue
        if c.get("penalised_for") == lp:
            continue
        hist = c.get("history", [])
        if any(h.get("source", "user") == "user" and h.get("date", "") >= lp
               for h in hist):
            continue
        if "penalised_for" not in c and any(
                h.get("source") == "reset" and h.get("date", "") > lp
                for h in hist):
            continue
        out.append(cid)
    return out


def apply_forgotten_reset(state: dict, card_ids) -> dict:
    """FASE 3: reset each forgotten card ONCE and record the penalty.

    Use this instead of calling update_card(..., 0, source="reset") directly —
    it stamps `penalised_for`, which is what stops the card being reset again
    every two days for the rest of its life.
    """
    for cid in card_ids:
        c = state["cards"].get(cid)
        if c is None:
            continue
        update_card(state, cid, 0, source="reset")
        state["cards"][cid]["penalised_for"] = (
            c.get("last_presented") or _card_pill_date(cid))
    return state


def select_review_candidates(state: dict, k: int = 3,
                             as_of: Optional[str] = None) -> list:
    """Pick up to k due cards to re-ask: active review queue first, then backlog.

    Two queues, served in order — the same split Anki and friends use:

      * ACTIVE  (repetitions >= 1): cards already in a repetition cycle. Served
        first, because consolidating a card the user has started learning is
        what actually grows an interval. Starving this queue means every card
        is reviewed once and never again, which is not spaced repetition.
      * BACKLOG (repetitions == 0): never successfully answered. Fills whatever
        capacity is left, introducing new material at the rate the schedule
        can absorb.

    Within each queue: (next_review asc, last_presented asc, ef asc).
    `last_presented` is the rotation tie-break — without it, cards tied on
    (next_review, ef) fall back to dict insertion order, i.e. card-creation
    order, so the same few oldest cards come back every day.

    Why the split is required, not cosmetic: the 220 legacy cards that were
    never answered carry a next_review from their original pill date (May-Aug
    2026), so on pure urgency they outrank every card in an active cycle
    forever. Measured over 180 simulated days at the current cadence, urgency-
    only ordering produced 0 cards with a second review and 3 mature; queue
    ordering produced 79 with a second review and 28 mature.
    """
    due = cards_due(state, as_of)
    if not due:
        return []

    def rank(cid):
        return (state["cards"][cid]["next_review"],
                card_last_presented(state, cid) or "",
                state["cards"][cid]["ef"])

    active = sorted((c for c in due if state["cards"][c].get("repetitions", 0) > 0),
                    key=rank)
    backlog = sorted((c for c in due if state["cards"][c].get("repetitions", 0) == 0),
                     key=rank)
    return (active + backlog)[:max(0, int(k))]


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


def recent_topic_tags(ledger: list, days: Optional[int] = None,
                      as_of: Optional[str] = None) -> set:
    """Set of topic tags already used — all of them by default.

    `days=None` (the default) blocks EVERY tag ever recorded. The old default
    was 60 days, which quietly turned into a recycling schedule: on 2026-08-17
    the routine re-served `hot-cross-bun-msa`, the second topic ever produced
    (2026-05-15), because 94 days had elapsed. By then 24 tags had aged out of
    the window and the pool of "reusable" topics was growing daily.

    Radiology has far more teachable signs than this ledger has entries (75
    distinct tags after 87 pills), so there is no reason to re-serve one. Pass
    an explicit `days` only if the ledger ever grows large enough that genuinely
    novel topics run out.
    """
    cutoff = date.fromisoformat(as_of) if as_of else date.today()
    out = set()
    for e in ledger:
        t = e.get("tag")
        if not t:
            continue
        if days is None:
            out.add(t)
            continue
        try:
            d = date.fromisoformat(e.get("date", ""))
        except ValueError:
            continue
        if 0 <= (cutoff - d).days <= days:
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


# ────────────────────────────────────────────────────────────────────
# Google-Form answer channel (FASE 2-bis) — adopted 2026-07-05 because
# Telegram getUpdates starves (single-consumer race; see CLAUDE.md,
# "Quiz answers via Google Form"). The SESSION downloads the linked
# response Sheet as CSV via the Drive MCP and saves it locally; the
# routine parses it with parse_form_csv() and de-dups with
# forms_state.json: {"processed_row_keys": [...]}.
# ────────────────────────────────────────────────────────────────────

def load_forms_state():
    """Return (fstate, sha). Default skeleton if absent."""
    raw, sha = gh_get("forms_state.json")
    if raw is None:
        return {"processed_row_keys": []}, None
    st = _json.loads(raw)
    st.setdefault("processed_row_keys", [])
    return st, sha


def parse_form_csv(csv_text: str) -> list:
    """Parse the Form-responses CSV export into answer rows.

    Expected column order (fixed by the Form's question order):
    timestamp, card_id, quality. Extra columns are ignored, the header
    row is skipped, malformed rows are dropped. Quality labels like
    "3 - così così" are accepted (leading digit wins).

    SCALE MAPPING (decided 2026-08-04). The Google Form's "Qualità"
    question offers six options 1–6, but SM-2 (and the Telegram 0–5
    buttons) use 0–5. The user chose to keep the 1–6 form as-is, so we
    map form → SM-2 by subtracting 1, clamped to [0,5]:

        form 1→0, 2→1, 3→2, 4→3, 5→4, 6→5

    Consequence: the SM-2 pass/fail boundary (quality < 3 = forgotten)
    now falls between form-3 (→ q2, fail) and form-4 (→ q3, pass).
    Returns [{"timestamp", "card_id", "quality"}].
    """
    import csv as _csv
    import io as _io
    import re as _re
    pat = _re.compile(r"^(\d{8}):(\d+)$")
    rows = []
    rdr = _csv.reader(_io.StringIO(csv_text))
    next(rdr, None)  # header
    for r in rdr:
        if len(r) < 3:
            continue
        ts, cid, q = r[0].strip(), r[1].strip(), r[2].strip()
        if not pat.match(cid) or not q[:1].isdigit():
            continue
        # Form options are 1–6; subtract 1 to land on the SM-2 0–5 scale.
        rows.append({"timestamp": ts, "card_id": cid,
                     "quality": max(0, min(5, int(q[0]) - 1))})
    return rows


def form_row_key(row: dict) -> str:
    """Stable idempotency key for one form-response row.

    Keyed on timestamp + card_id ONLY. One Form submission is a unique
    (timestamp, card) pair, so quality is not needed for uniqueness — and
    it must NOT be in the key: the 2026-08-04 scale remap (form 1–6 → SM-2
    0–5, see parse_form_csv) changed every parsed quality, and a
    quality-bearing key would have made all historic rows look new and be
    re-ingested. A genuine re-answer of the same card happens at a
    different timestamp, so it still receives its own key.

    NOTE: forms_state.json was migrated to this scheme on 2026-08-04.
    """
    return f"{row['timestamp']}|{row['card_id']}"


def mark_form_rows_local(fstate: dict, keys) -> dict:
    """Pure: record processed row keys (FIFO cap 5000), NO I/O.

    Dump `json.dumps(fstate, indent=2)` as the forms_state.json upsert.
    """
    seen = fstate.setdefault("processed_row_keys", [])
    for k in keys:
        if k not in seen:
            seen.append(k)
    fstate["processed_row_keys"] = seen[-5000:]
    return fstate


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
