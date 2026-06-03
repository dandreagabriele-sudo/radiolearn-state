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
`outbox/<pill_id>.json`. The state must land before the outbox so that when
the receiver Action processes callbacks, it sees consistent SM-2 data.

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
- `pill_qa_path(pill_date)` → `"pills_log/<date>.qa.json"`.
- `gh_get_pill_qa(pill_date)` → `(qa_dict | None, sha | None)`.
- `gh_get_pill_question(pill_date, qidx)` → `dict | None` (single Q&A).
- `gh_put_pill_qa(pill_date, qa_obj, msg)` → new sha.
- `assert_no_placeholders(envelope, sentinels=...)` → raises `ValueError`
  if the envelope contains a known placeholder sentinel
  (`"domanda ripasso non disponibile"`, `"placeholder"`, `"todo"`, …) or
  ≥ 3 empty option lines (`A) — / B) — / …`).

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
- **Composizione contenuti**: vedi la sezione successiva.

## Ripassi composti dinamicamente (post-bug 2026-06-03)

Il 2026-06-03 i 3 ripassi sono arrivati su Telegram come "Domanda ripasso
non disponibile" perché il daily script aveva un `if/elif` con i `card_id`
hard-coded basati su una predizione fatta prima di eseguire FASE 3. La
FASE 3 ha resettato quei `card_id` (li ha spostati a `next_review` di
domani) e a FASE 6a `select_review_candidates` ha restituito tre `card_id`
diversi — il ramo `else` del template ha emesso il placeholder.

**Ordine reale che il composer deve onorare**:

1. FASE 2 letta inbox → popola `answered_today`.
2. FASE 3 resetta le carte due-and-unanswered (muta `next_review`).
3. **Solo dopo** `select_review_candidates(state, k=3)` riflette lo stato
   reale post-reset.

**Regole obbligatorie per FASE 6a in tutti i futuri daily script**:

1. **Mai** hard-codare i contenuti di ripasso per `card_id` specifici. I
   `card_id` selezionati non sono predicibili prima di eseguire FASE 2-3.
2. Per ciascun `cid` restituito da `select_review_candidates`:
   - `orig_pill_id, qidx_str = cid.split(":")`;
   - `orig_date = f"{orig_pill_id[:4]}-{orig_pill_id[4:6]}-{orig_pill_id[6:8]}"`;
   - `orig_q = gh_get_pill_question(orig_date, int(qidx_str))`;
   - se `orig_q is None` (sidecar `.qa.json` assente per pillole legacy):
     scarica `pills_log/<date>.md` raw via `gh_get` ed estrai la Q al
     `qidx` indicato. Se non riesci: **scarta** quel `cid` dal bundle,
     aggiungi un warning, e prosegui con i candidati rimanenti — **non**
     emettere placeholder;
   - sintetizza in linea una NUOVA Q&A che indaga lo stesso concetto da
     angolazione diversa, riusando `orig_pill_id` e `qidx` nel
     `callback_data` (lo SM-2 update atterra sulla carta originale).
3. **Prima di scrivere l'outbox in FASE 9**, chiama
   `assert_no_placeholders(envelope)`. Se solleva `ValueError`:
   - **non** scrivere l'outbox;
   - **non** aggiornare `last_review_inclusion_date`;
   - re-componi i ripassi mancanti o omettili interamente, e riprova.

## Sidecar `pills_log/<date>.qa.json` — obbligatorio in FASE 8 (post-2026-06-03)

Per rendere ai futuri daily script banale ricomporre i ripassi, ogni
FASE 8 deve scrivere — oltre al `pills_log/<date>.md` archivistico — un
sidecar JSON strutturato con le **nuove** Q&A del giorno (i ripassi NON
vanno nel sidecar: il sidecar è la "fonte di verità" per quando *queste*
carte saranno ripassate in futuro). Schema:

```json
{
  "pill_id":  "20260603",
  "date":     "2026-06-03",
  "topic":    "Late Gadolinium Enhancement (LGE) ...",
  "domain":   "Torace / Cuore — RM cardiaca — Livello 1 (esperto)",
  "questions": [
    {
      "card_id":   "20260603:0",
      "qidx":      0,
      "title":     "Pattern LGE dell'amiloidosi cardiaca",
      "body":      "Donna 72 a, IC a frazione di eiezione preservata, ...",
      "opts":      {"A": "...", "B": "...", "C": "...", "D": "..."},
      "answer":    "A",
      "rationale": "L'amiloidosi cardiaca infiltra ..."
    }
  ]
}
```

Usa `gh_put_pill_qa(date, qa_obj, msg)`. L'ordine completo di FASE 9
diventa: `sm2_state.json` → `pills_log/<date>.md` →
`pills_log/<date>.qa.json` → `outbox/<pill_id>.json`. Il sidecar viene
PRIMA dell'outbox per consistenza con la regola "state-before-outbox" —
anche se nessuna routine attuale legge il sidecar appena scritto, mantenere
l'invariante "tutto persistito prima dell'envelope" semplifica la
riconciliazione manuale post-mortem.

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
