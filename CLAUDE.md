# RadioLearn — Operational guidance for Claude sessions

This repo is driven by a daily Claude session that executes the RadioLearn
routine described in the chat prompt (the **spec**). To prevent the routine
from being left half-executed (no outbox = no Telegram delivery), follow
these rules **strictly**.

## Atomic-execution rule (CRITICAL)

The routine has 9 phases. Phases 1–6 are bookkeeping + content composition;
phases 7–9 (envelope, log, persistence) are the deliverable. The deliverable
is what actually reaches the user via Telegram. If you stop after phase 6,
the user gets nothing.

**Therefore:**

1. **Compose the full Python script first**, in `/tmp/radiolearn/routine.py`.
   Write it with `Write` (one tool call), then optionally do a single
   `Edit` pass to fix bugs you spotted on the second read.
2. **Execute the script as the final tool call of the same turn.**
   Do not split "compose" and "execute" across turns.
3. After the script prints `[FASE 9] outbox envelope written`, fetch
   `git log origin/main --oneline -3` and verify that an
   `🤖 Outbox drained` commit appeared (the Action consumed the envelope).
   If it did not within ~60 s, inspect the workflow logs.
4. If you are forced to stop mid-routine for any reason, persist what you
   have to a sentinel file (e.g. `/tmp/radiolearn/state.partial.json`) and
   warn the user explicitly. Do **not** silently return.

## Persistence order (non-negotiable)

FASE 9 order: `sm2_state.json` → `pills_log/<YYYY-MM-DD>.md` →
**(Monday) `backups/sm2_state.backup.monday.json`** → `outbox/<pill_id>.json`.
The state must land before the outbox so that when the receiver Action
processes callbacks, it sees consistent SM-2 data.

The `outbox/<pill_id>.json` write MUST be the **last** Contents-API write of
the routine. It triggers the `send-to-telegram` Action, which checks out
`main`, sends, then commits the drain and pushes. Any routine write that
lands on `main` *after* the outbox (e.g. the Monday backup) advances the
branch while the Action is mid-flight and makes the Action's push lose the
race — a red run + an orphaned outbox file that gets re-sent the next day
(this is what broke on 2026-06-15). So on Mondays write the backup **before**
the outbox. The `send-to-telegram` / `poll-telegram` workflows now also
retry their push with `git pull --rebase`, but keeping the outbox last is
the cheap structural guarantee.

On `409`/`422` for `sm2_state.json`: refetch the sha and retry **once**.
Do not write the outbox until the state has landed.

## Library helpers

`.github/scripts/radiolearn_lib.py` exports these helpers in addition to the
GitHub + SM-2 API documented at the top of the file:

- `esc(text)` — MarkdownV2 escape (Telegram).
- `link(text, url)` — MarkdownV2 inline link.
- `quiz_keyboard(pill_id, qidx)` — 0–5 inline self-assessment keyboard.
- `gh_put_retry(path, content, msg, sha)` — `gh_put` with one retry on
  `409`/`422` (refetch sha) and `5xx` (sleep 3 s).

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

## Secrets

The PAT and chat ID live only in the chat prompt. Do **not** commit them
anywhere in this repo. The receiver Action holds the Telegram bot token in
the repository secrets (`secrets.TELEGRAM_BOT_TOKEN`).

## Channels

- **Outbound to Telegram:** `outbox/<pill_id>.json` (envelope of Telegram
  Bot API method calls). The `send-to-telegram` workflow consumes and
  deletes the file.
- **Inbound from Telegram:** `inbox/<YYYY-MM-DD>.json` (callbacks for quiz
  self-assessment). The `poll-telegram` workflow writes these. The daily
  routine reads, applies SM-2 updates, then deletes the file.
- **State:** `sm2_state.json` (cards + history + idempotency set).
- **Telegram state:** `telegram_state.json` (owned by the poller Action;
  the routine never writes it).

Never call `api.telegram.org` directly from the routine — sandbox blocks
it and the workflow already handles delivery. Never `git push` — the
integration is read-only; write via the Contents API only.
