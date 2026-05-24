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

Prefer these helpers over reinventing them in the daily script. Smaller
scripts are less likely to be left half-executed.

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
