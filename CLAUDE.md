# RadioLearn — Operational guidance for Claude sessions

This repo is driven by a daily Claude session that executes the RadioLearn
routine described in the chat prompt (the **spec**). To prevent the routine
from being left half-executed (no outbox = no Telegram delivery), follow
these rules **strictly**.

## Write path on Claude Code on the web (CRITICAL — since 2026-06-30)

**Direct GitHub Contents-API writes no longer work from the web session.**
Anthropic's Claude Code on the web egress proxy now blocks every `PUT`/`DELETE`
to `api.github.com`:

```
403 {"message":"Write access to this GitHub API path is not permitted through this proxy."}
```

This is environment-level and **token-independent** (reads still return `200`;
a new PAT does not help). It breaks the old "routine writes to `main` via the
Contents API" model. Two channels are **not** blocked and are now the write
path:

- **git push to the *working branch*** (the proxy allows pushes to the current
  branch only, not `main`), and
- **GitHub-MCP pull-request operations** (`create_pull_request`,
  `merge_pull_request`) — routed through Anthropic's MCP channel, not the
  egress proxy.

So the routine **reads + computes in the sandbox, then delivers via git + an
MCP-merged PR**. The sandboxed `routine.py` cannot call MCP tools — only the
session can — so persistence is split: the script *dumps* artifacts; the
session *lands* them. Concretely:

1. `routine.py` does FASE 1–8 (reads via `gh_get`/`gh_list` still work) and
   builds **every** FASE-9 output **in memory** — including the topic ledger
   (`append_topic_entry`) and, on paper days, the papers state
   (`mark_paper_local`). Do **not** call any writing helper (`gh_put`,
   `gh_delete`, `append_topic`, `mark_paper_processed`); they now raise a clear
   `RuntimeError` pointing here.
2. The script's last step is
   `build_delivery(out_dir, upserts, deletes)` (in `radiolearn_lib.py`), which
   writes the artifacts + a `manifest.txt`. `upserts` is a list of
   `(repo_path, content_str)` with **`outbox/<pill_id>.json` LAST**; `deletes`
   lists processed inbox files.
3. The session runs
   `bash .github/scripts/deliver_pr.sh <manifest> <work_branch> <msg_file>`,
   which rebuilds `<work_branch>` from a clean `origin/main`, applies the
   manifest, and force-pushes. It prints `PUSHED <sha>`.
4. The session opens and **squash-merges** the PR to `main` via GitHub MCP
   (`create_pull_request` → `merge_pull_request`, `merge_method="squash"`).
   The merge lands all FASE-9 files atomically; the outbox file appearing on
   `main` triggers `send-to-telegram`.
5. Verify the drain (see the atomic-execution rule).

**FASE-9 tail of `routine.py`** (build everything in memory, then dump — no
GitHub writes):

```python
ledger = append_topic_entry(ledger, today_iso, tag, domain, level, "auto")
upserts = [
    ("sm2_state.json",            json.dumps(state,  indent=2)),
    (f"pills_log/{today_iso}.md", pill_md),
    ("topics_log.json",           json.dumps(ledger, indent=2)),
]
if is_monday:
    upserts.append(("backups/sm2_state.backup.monday.json", json.dumps(state, indent=2)))
if paper_day:                      # pstate = mark_paper_local(pstate, file_id, pill_id, title)
    upserts.append(("papers_state.json", json.dumps(pstate, indent=2)))
upserts.append((f"outbox/{pill_id}.json", json.dumps(envelope, indent=2)))   # LAST
deletes = [f"inbox/{d}.json" for d in processed_inbox_dates]
manifest = build_delivery("/tmp/radiolearn/out", upserts, deletes)
print("MANIFEST", manifest)
```

Then, in the same turn, the session runs (work branch = the session's
designated branch):

```bash
printf 'RadioLearn pill %s: %s\n' "$pill_id" "$tag" > /tmp/radiolearn/msg.txt
bash .github/scripts/deliver_pr.sh /tmp/radiolearn/out/manifest.txt <work_branch> /tmp/radiolearn/msg.txt
```

and merges the resulting PR to `main` via GitHub MCP
(`create_pull_request` head=`<work_branch>` base=`main`, then
`merge_pull_request` `merge_method="squash"`).

Because all FASE-9 files land in **one** squash commit and the session makes
**no** further `main` writes until the Action drains the outbox, the old
"state-before-outbox / outbox-last" race is gone by construction.

> Other routines with the same "PAT + Contents API" architecture are hit by the
> same proxy change and must migrate the same way (git push to the working
> branch + MCP PR merge), or run outside Claude Code on the web.

## Atomic-execution rule (CRITICAL)

The routine has 9 phases. Phases 1–6 are bookkeeping + content composition;
phases 7–9 (envelope, log, persistence) are the deliverable. The deliverable
is what actually reaches the user via Telegram. If you stop after phase 6,
the user gets nothing.

**Therefore:**

1. **Compose the full Python script first**, in `/tmp/radiolearn/routine.py`.
   Write it with `Write` (one tool call), then optionally do a single
   `Edit` pass to fix bugs you spotted on the second read. The script ends by
   calling `build_delivery(...)` (it **dumps**; it does not write to GitHub —
   see "Write path on Claude Code on the web").
2. **Execute the script, then deliver in the same turn:** run `routine.py`
   (it prints the manifest path), then `deliver_pr.sh`, then the GitHub-MCP
   PR create + squash-merge. Do not split compose / execute / deliver across
   turns — a dumped-but-unmerged manifest means no Telegram delivery.
3. After the PR is merged, fetch `git log origin/main --oneline -3` and verify
   that an `🤖 Outbox drained` commit appeared and `outbox/<pill_id>.json` is
   gone from `main` (the Action consumed the envelope). If it did not within
   ~60 s, inspect the workflow logs.
4. If you are forced to stop mid-routine for any reason, persist what you
   have to a sentinel file (e.g. `/tmp/radiolearn/state.partial.json`) and
   warn the user explicitly. Do **not** silently return.

## Persistence order (non-negotiable)

FASE 9 set: `sm2_state.json`, `pills_log/<YYYY-MM-DD>.md`, `topics_log.json`,
**(Monday) `backups/sm2_state.backup.monday.json`**, **(paper days)
`papers_state.json`**, and `outbox/<pill_id>.json` — plus the `deletes` for
processed inbox files. Pass them to `build_delivery(out_dir, upserts, deletes)`
in that order, with **`outbox/<pill_id>.json` LAST** in `upserts`.

Under the PR-merge write path the whole set lands on `main` in **one atomic
squash commit**, so the receiver Action always sees consistent SM-2 data
alongside the outbox — the ordering in `upserts` is now cosmetic, not a
correctness requirement. Keep the outbox last anyway for readability and so the
list still reads correctly if the mechanism ever reverts to sequential writes.

The old per-write race (a write landing on `main` *after* the outbox while
`send-to-telegram` is mid-flight — what broke on 2026-06-15, the Monday backup)
**cannot happen** here: everything is in the single merge commit and the session
performs **no** further `main` write until the Action has drained the outbox.
The one rule that survives: after the merge, do not push anything else to `main`
until you have seen the `🤖 Outbox drained` commit.

(The send-to-telegram / poll-telegram Actions run server-side, are not behind
the egress proxy, and continue to use git push with `git pull --rebase`
retries — unchanged.)

## Library helpers

`.github/scripts/radiolearn_lib.py` exports these helpers in addition to the
GitHub + SM-2 API documented at the top of the file:

- `esc(text)` — MarkdownV2 escape (Telegram).
- `link(text, url)` — MarkdownV2 inline link.
- `quiz_keyboard(pill_id, qidx)` — 0–5 inline self-assessment keyboard.
- `gh_put_retry(path, content, msg, sha)` — `gh_put` with one retry on
  `409`/`422` (refetch sha) and `5xx` (sleep 3 s). **Writes — proxy-blocked on
  the web; do not call it from the routine** (it raises a `RuntimeError`
  pointing at the delivery path). Kept for Actions-side tooling.
- `load_topics_ledger()` / `recent_topic_tags(ledger, days=60)` /
  `domain_counts(ledger, days=7)` — topic de-dup ledger (see "Topic
  de-duplication" below). To record today's topic use the **pure**
  `append_topic_entry(ledger, date_iso, tag, domain, level, source)` (no I/O)
  and dump the result as the `topics_log.json` upsert — **not** `append_topic`
  (which writes).
- `load_papers_state()` / `paper_is_processed(pstate, file_id)` and
  `PAPERS_DRIVE_FOLDER` — paper ingestion (see "Paper-derived pills" below). To
  mark a paper consumed use the **pure** `mark_paper_local(pstate, file_id,
  pill_id, title)` (no I/O) and dump `papers_state.json` — **not**
  `mark_paper_processed` (which writes).
- `build_delivery(out_dir, upserts, deletes=None)` — write the FASE-9 artifacts
  + `manifest.txt` for `.github/scripts/deliver_pr.sh`. This is the routine's
  final step (see "Write path on Claude Code on the web").

Prefer these helpers over reinventing them in the daily script. Smaller
scripts are less likely to be left half-executed.

## Answer source tagging — FASE 3 and FASE 5

`update_card(state, card_id, quality, source="user")` tags every history
entry with its origin. This matters for the weekly summary:

- **FASE 2** (real callback from inbox) → `update_card(state, cid, q)`
  (default `source="user"`).
- **FASE 3** (forgotten-card auto-reset) → MUST pass `source="reset"`,
  i.e. `update_card(state, cid, 0, source="reset")`.

**FASE 5** (weekly summary, Mondays) MUST count only entries with
`h.get("source", "user") == "user"` for "Quiz risposti" and
"Qualità media". Without this filter, FASE 3 resets (always
`quality=0`) pollute the completion rate (→ 100 %) and crush the
average quality (→ 0.0) — this was the bug surfaced on 2026-05-25.
"Carte dimenticate" in the summary should count entries with
`source == "reset"` in the last 7 days.

Old history entries (pre-tag) have no `source` field and default to
`"user"` for backward compatibility; this slightly inflates the
"Quiz risposti" count during the first 7-day window after the upgrade
but self-corrects after one week.

### Weekly-summary counting — cohort metric (FASE 5, OVERRIDE)

The original spec computed `Quiz risposti: Y/Z` as *(user history rows in
the last 7 days) / (all quiz cards delivered in the last 7 days)*. That
ratio is **incoherent** and was surfaced as misleading on 2026-06-29:

- **Cohort mismatch** — the numerator counts answer *rows* (a ripasso of an
  old card adds a `user` row whose card is **not** in the denominator),
  while the denominator counts *cards*. Numerator and denominator are
  different populations, so the percentage is meaningless and can even
  exceed 100 %.
- **Raw-row counting** — ripasso reuses the original `card_id`, so one card
  answered three times over three weeks contributes three rows.

Compute the completion rate over **one cohort: the quiz cards delivered in
the trailing 7 days**, counting **distinct cards**, not rows:

```
cutoff = (today - 6 days)                       # YYYYMMDD prefix compare
cohort = [cid for cid in state["cards"]         # new cards delivered this week
          if cid[:8].isdigit() and cid[:8] >= cutoff_compact]
def answered(cid):                              # ≥1 genuine user answer, ever
    return any(h.get("source","user") == "user"
               for h in state["cards"][cid]["history"])
Z = len(cohort)                                 # quiz inviati (cards)
Y = sum(1 for cid in cohort if answered(cid))   # quiz risposti (distinct cards)
rate = round(100*Y/Z) if Z else 0               # bounded 0–100 %
quals = [h["quality"] for cid in cohort for h in state["cards"][cid]["history"]
         if h.get("source","user") == "user"]
avg_q = round(sum(quals)/len(quals), 1) if quals else 0.0
```

Notes:
- Count a cohort card as answered if it has **any** `user` entry (do not
  filter the entry by date): answers are credited the *morning after* they
  are given (poller → inbox → next routine's FASE 2), so an entry-date
  filter would drop yesterday's answers. The cohort (pill date) is the
  window; the answer can land a little later.
- Today's pill is **not** in the cohort — its cards are created in FASE 6,
  after FASE 5 runs — so it never drags the rate down.
- `Carte dimenticate` keeps its meaning: `source == "reset"` rows in the
  last 7 days (by entry date). `Carte mature` = cards with `interval > 21`.

A persistently low rate with healthy answering usually means answers are
being **lost upstream** (see "Telegram answer retrieval" below), not a
counting bug — check there before assuming the user simply skipped quizzes.

### Telegram answer retrieval — single-consumer invariant

Quiz answers reach the state only via `getUpdates` long-polling in
`poll.py`. Telegram tracks the confirmed-update offset **per bot token,
shared across every consumer of that token**: if a second process polls the
same bot, whoever calls `getUpdates` first confirms (deletes) the update
for everyone, so our poller silently sees nothing. This is the documented
cause of the intermittent "answers stop being credited" symptom
(2026-06-16 → 2026-06-29): a stale poller in another repo/deployment using
the same token was racing and usually winning. Resolved on 2026-06-29 by
switching to a brand-new bot (`@RadTeachingPills_bot`, id 8793723167) the
stale poller has no token for; a 4-tap test then captured 4/4 cleanly.
When swapping bots, **reset `telegram_state.json` to
`{"last_update_id": 0}`** — a new bot's update_id range can be lower than
the old stored offset, which would otherwise make the poller skip every
new answer. **Invariant: exactly one process may poll the bot
(`@RadTeachingPills_bot`).** If crediting goes intermittent again,
run the manual **Telegram Diagnose** workflow right after tapping a button
(`getMe` / `getWebhookInfo` / a **non-destructive `offset=last_update_id+1`
peek**); a pending update that the peek shows but the poller never captures,
or taps that vanish before a healthy poll sees them, points to a second
consumer — fix by stopping it or revoking + reissuing the bot token (then
update the `TELEGRAM_BOT_TOKEN` secret).

> **Never diagnose with `getUpdates(offset=-1)`.** Telegram documents a
> negative offset as "all previous updates will be forgotten": it forgets the
> pending answer server-side, so the diagnostic itself *destroys* the tap you
> are inspecting. This was the 2026-07-01 finding — the poller and delivery
> were healthy; the `offset=-1` peek in `diagnose.py` was silently dropping
> every tapped answer used to test it, which read as "crediting is broken."
> `diagnose.py` now peeks with the positive `last_update_id + 1` offset, which
> shows exactly what the next poll will fetch and consumes nothing.

## Quiz answers via Google Form (FASE 2-bis — OVERRIDE, since 2026-07-05)

Telegram inline-button answers stopped being credited again on 2026-07-02
(getUpdates returns 0 updates, offset frozen, no webhook set, correct bot —
the single-consumer race recurred even on the new bot). Decision 2026-07-05:
**the reliable answer channel is a Google Form**, read back through the
response Sheet via the Drive MCP. The Telegram plumbing (keyboards, poll.py,
workflows) stays untouched as a best-effort secondary path.

**Setup state:** `form_config.json` at the repo root holds
`{"prefill_url": "...&entry.NNNN={card_id}", "sheet_file_id": "..."}`.
**Until that file exists, the channel is not yet configured**: compose pills
as before (keyboard only) and skip FASE 2-bis. The user creates the Form once
(question 1: "Card", short text, required; question 2: "Qualità", multiple
choice 0–5, required — in THIS order, the CSV parser relies on it), links it
to a response Sheet, and provides the prefilled link; the session then
commits `form_config.json` (find `sheet_file_id` via Drive `search_files`).

**Each morning, right after FASE 2 (inbox), when `form_config.json` exists:**

1. The *session* (not the sandboxed routine) downloads the response Sheet:
   `download_file_content(fileId=sheet_file_id, exportMimeType="text/csv")`
   → base64-decode → save to `/tmp/radiolearn/form.csv`. If the download
   fails, skip FASE 2-bis and flag it in the FASE-10 report — do not abort.
2. `routine.py` ingests it:

   ```python
   fstate, _ = load_forms_state()
   rows = parse_form_csv(open("/tmp/radiolearn/form.csv").read())
   fresh = [r for r in rows if form_row_key(r) not in fstate["processed_row_keys"]]
   for r in fresh:
       update_card(state, r["card_id"], r["quality"])   # source="user"
       answered_today.add(r["card_id"])
   fstate = mark_form_rows_local(fstate, [form_row_key(r) for r in fresh])
   ```

3. Ingest form rows **before** FASE 3, so form-answered cards land in
   `answered_today` and are not reset as no-shows.
4. Add `("forms_state.json", json.dumps(fstate, indent=2))` to the FASE-9
   `upserts` (before the outbox) whenever `fresh` is non-empty.

**Pill composition:** each quiz message keeps the 0–5 inline keyboard AND
gains one line under the spoiler block:
`link("📝 Registra 0–5", prefill_url.replace("{card_id}", card_id.replace(":", "%3A")))`
(MDV2 link URLs only escape `)` and `\`). Ripasso questions put the
*original* card_id in the link, same as in callback_data. Double-crediting
(tap + form for the same card) is accepted as rare and SM-2-tolerable.

Form answers are `source="user"`, so FASE 5 weekly stats count them with no
changes. The answered-morning-after semantics are identical to the inbox
path. Unlike Telegram updates, Sheet rows never expire and are never
consumed by a rogue second reader — idempotency lives in `forms_state.json`
(`processed_row_keys`, FIFO cap 5000), like `processed_update_ids`.

## Ripasso (FASE 6a) — cadenza e bundle (OVERRIDE spec)

Questa sezione **sostituisce** la regola della chat spec ("cadenza minima
7 giorni, 1 ripasso per pillola").

- **Cadenza minima**: `days_since_last_review >= 3` (≈ 2–3 pillole di
  ripasso a settimana).
- **Bundle**: usa `select_review_candidates(state, k=3)` per pescare
  fino a 3 carte stantie (ordinate per `next_review` asc, poi `ef` asc).
- **Numero ripassi per pillola**: 2 o 3 (default 3; scendi a 2 solo se
  la pillola del giorno sarebbe troppo lunga o se sono dovute meno di 3
  carte).
- **Struttura di ogni ripasso**: identica a una Q nuova — domanda +
  A/B/C/D + `||spoiler con risposta + razionale||` + bottoni 0–5.
- **callback_data**: ogni ripasso riusa l'`original_card_id` della
  carta selezionata (`q|<orig_pill_id>|<orig_qidx>|<quality>`), NON un
  id nuovo. Lo SM-2 update atterra sulla carta originale.
- **last_review_inclusion_date**: aggiorna a `today.isoformat()` se hai
  incluso ≥ 1 ripasso, **anche** se per qualche carta selezionata la
  pillola originale è assente (404). Evita di ri-tentare ogni giorno.
- **Pillola totale**: 6–8 Q (3 ripassi + 3–4 nuove) nei giorni di
  ripasso; 3–4 Q nuove nei giorni senza ripasso.
- **Composizione contenuti**: per ogni carta di ripasso pesca il
  `pills_log/<orig_date>.md` originale e genera una Q&A nuova che
  indaghi lo stesso concetto da angolazione diversa (scenario clinico,
  fisiopatologia, DD, eccetera).

## Paper-derived pills (FASE 0 — route A, Google Drive)

The user can turn a scientific paper into the next day's pill by dropping
the PDF into the Google Drive folder **`RadioLearn-Papers`** (constant
`PAPERS_DRIVE_FOLDER`) on the connected account (dandrea.gabriele@gmail.com).
This is **FASE 0**: a pre-check that runs *before* topic generation. A paper
dropped on day D surfaces as day D+1's pill (the routine runs once each
morning) — this is the "replace the day-after pill" behaviour.

**Each morning, before FASE 6:**

1. Via the Google Drive MCP tools (the daily *session* has them; the
   sandboxed `routine.py` does **not**), resolve the folder once with
   `search_files` (`name = 'RadioLearn-Papers' and mimeType =
   'application/vnd.google-apps.folder'`), then list PDFs in it
   (`mimeType = 'application/pdf' and '<folder_id>' in parents`).
2. Load `papers_state.json` (`load_papers_state()`); drop any file whose
   id is in `processed_file_ids`. If none remain → no paper today, proceed
   to the normal generated topic.
3. If ≥ 1 unprocessed PDF: pick the **oldest by `createdTime` (FIFO)** —
   exactly **one paper per day** (one-shot). Download it
   (`download_file_content` → base64 → save to the scratchpad → `Read` the
   PDF; `read_file_content` is an acceptable text-only fallback).
4. The paper **replaces the day's generated topic** (FASE 6b). Everything
   else is unchanged: FASE 2/3 (inbox + resets), FASE 6a ripasso, SM-2
   cards for the new questions, FASE 7–9 persistence.

**Pill composition from a paper (one-shot summary):**

- Extract only the **essential** points and, above all, **what is new**
  (novel findings, changed thresholds, a new sign/criterion). Summarize —
  do not transcribe.
- Profile to the user's 3 levels as usual; if the paper is **off-domain**
  (outside the three competency areas), keep it **basic** (Livello-3
  style): plain explanation, no deep specialist detail.
- Full pill structure: concetto + connessioni + (optional images) + 3–4
  new quiz Q&A with the mandatory structure (A/B/C/D + `||spoiler||` +
  buttons 0–5), anchored to the paper.
- **Citation is mandatory and must be accurate**: authors / journal /
  year / DOI taken **from the paper itself**. Never invent a DOI/URL; if
  you add a link use the paper's own DOI or a `web_search`-verified
  landing page (never `requests.get`).

**Mark the paper consumed in the FASE-9 set** with the pure
`pstate = mark_paper_local(pstate, file_id, pill_id, title)` and include
`papers_state.json` as an upsert in `build_delivery` (before the outbox);
record the topic with `source="paper"`. The Drive toolset has no move/delete
op, so idempotency lives entirely in `papers_state.json`; the folder may keep
accumulating PDFs harmlessly.

## Topic de-duplication & rotation ledger (FASE 6)

Audit on 2026-06-27 (40 pills): `mosaic-attenuation` ×4, `cmr-lge` ×4,
`reversed-halo-atoll` ×3, plus `uip-ipf` / `pe-ctpa` / `lirads-hcc` ×2;
~51 % torace and only **one** Livello-3 pill. Cause: no memory across
sessions. `topics_log.json` (backfilled from all prior pills) fixes this.

**Before choosing a generated topic (FASE 6b — skip on paper days):**

1. `ledger, tsha = load_topics_ledger()`.
2. `blocked = recent_topic_tags(ledger, days=60)` — **do not reuse any tag
   from the last 60 days.** Pick a genuinely different concept (a new tag),
   not a re-angled duplicate of a recent one.
3. Balance with `domain_counts(ledger, days=7)` and steer the rotation:
   - cap `torace` (Livello 1) at **≤ 3 per rolling 7 days**;
   - guarantee **≥ 1 Livello-2 (RM addome)** and **≥ 1 Livello-3
     (RM altri distretti: neuro, MSK, testa-collo, pelvi, mammella)** per
     rolling 7 days. Livello 3 is the starved bucket — prioritise it.

**Always record the chosen topic in the FASE-9 set** (as the `topics_log.json`
upsert, before the outbox) with the pure helper:
`ledger = append_topic_entry(ledger, today_iso, tag, domain, level, source)`
— `source` is `"auto"` or `"paper"`. Use a short, stable, normalized
kebab-case `tag` (e.g. `crazy-paving`, `cmr-lge`) so future de-dup matches.

**Standardized `pills_log` front-matter** (keeps the ledger derivable): keep
the `**Topic:**` line and add directly under it
`<!-- meta: tag=<kebab>; domain=<torace|cardio|addome|aorta-vascolare|neuro|msk|altro>; level=<1|2|3>; source=<auto|paper> -->`.

## Secrets

The PAT and chat ID live only in the chat prompt. Do **not** commit them
anywhere in this repo. The receiver Action holds the Telegram bot token in
the repository secrets (`secrets.TELEGRAM_BOT_TOKEN`).

## Channels

- **Outbound to Telegram:** `outbox/<pill_id>.json` (envelope of Telegram
  Bot API method calls). The `send-to-telegram` workflow consumes and
  deletes the file.
- **Inbound from Telegram (best-effort):** `inbox/<YYYY-MM-DD>.json`
  (callbacks for quiz self-assessment). The `poll-telegram` workflow writes
  these. The daily routine reads, applies SM-2 updates, then deletes the
  file. Unreliable since 2026-07-02 (single-consumer race) — the primary
  answer channel is now the Google Form (see FASE 2-bis).
- **Inbound from Google Form (primary):** the Form's linked response Sheet
  (id in `form_config.json`), exported to CSV via the Drive MCP by the
  session each morning. **Form state:** `forms_state.json` (processed row
  keys — idempotency; Sheet rows are never deleted).
- **State:** `sm2_state.json` (cards + history + idempotency set).
- **Telegram state:** `telegram_state.json` (owned by the poller Action;
  the routine never writes it).
- **Topic ledger:** `topics_log.json` (one entry per pill; drives FASE 6
  de-duplication and domain/level rotation).
- **Paper queue (inbound):** Google Drive folder `RadioLearn-Papers` on
  the connected account (read via the Drive MCP by the session, not by
  the sandboxed `routine.py`). **Paper state:** `papers_state.json`
  (processed file ids — idempotency, since Drive has no move/delete op).

Never call `api.telegram.org` directly from the routine — sandbox blocks
it and the workflow already handles delivery. Never `git push` — the
integration is read-only; write via the Contents API only.
