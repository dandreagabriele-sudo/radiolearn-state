#!/usr/bin/env python3
"""One-off repair of sm2_state.json after the FASE-3 mass-reset bug (2026-08-16).

WHAT WENT WRONG
    FASE 3 called cards_overdue_without_answer(), which returns every overdue
    card — including the ~280/day the routine never presented. Each was reset
    with quality=0, which pins repetitions=0, interval=1 and re-arms
    next_review=tomorrow, so the card comes back overdue two days later and is
    reset again, forever. Result at HEAD: 338/338 cards at repetitions=0,
    325/338 at the ef=1.3 floor, 0 mature cards, and 7382 reset rows against
    142 genuine answers. Every passing Form answer was annulled within ~48 h.

WHAT THIS SCRIPT DOES
    1. Rebuilds ef / interval / repetitions / next_review for every card by
       REPLAYING only the genuine answers, so the user's real scores finally
       count.
    2. Drops every spurious reset row (97 % of the file), keeping only genuine
       answers. All 7382 of them are artifacts of the bug, so none is worth
       preserving — and keeping any would corrupt FASE-5's "Carte dimenticate",
       which counts source="reset" rows in the trailing 7 days.
    3. Sets last_presented (for the new FASE 3 and the rotation tie-break) and
       stamps penalised_for on the never-answered cards, so the repaired deck
       is not immediately re-reset for presentations it already paid for.

IDENTIFYING GENUINE ANSWERS
    source="reset"        -> spurious, drop.
    source="user"         -> genuine.
    no source field       -> pre-tag (76 rows). These default to "user" for
                             backward compatibility, but the q=0 blocks on
                             2026-05-17/19/23/25 are whole-cohort zeros with no
                             positive answer anywhere that day: they are FASE-3
                             resets from before tagging existed. Detected
                             heuristically (>=5 zeros and 0 positives in a day)
                             and dropped.

    NOTE on scale: answers before 2026-08-04 were recorded on the un-shifted
    Form scale (see parse_form_csv). Per CLAUDE.md that is a deliberate,
    documented decision, so the replay honours the stored quality as-is and
    does not retro-correct it.

USAGE
    python3 .github/scripts/repair_sm2.py sm2_state.json out.json
"""
import json
import sys
from collections import Counter
from datetime import date, timedelta


def card_pill_date(card_id):
    prefix = card_id.split(":")[0]
    if len(prefix) == 8 and prefix.isdigit():
        return f"{prefix[:4]}-{prefix[4:6]}-{prefix[6:8]}"
    return None


def find_masked_reset_days(cards):
    """Pre-tag days that are really FASE-3 resets: bulk zeros, no positives."""
    zeros, positives = Counter(), Counter()
    for c in cards.values():
        for e in c.get("history", []):
            if "source" not in e:
                (zeros if e.get("quality") == 0 else positives)[e.get("date")] += 1
    return {d for d, n in zeros.items() if n >= 5 and positives.get(d, 0) == 0}


def is_genuine(entry, masked_days):
    if entry.get("source") == "reset":
        return False
    if "source" not in entry and entry.get("date") in masked_days:
        return False
    return True


def replay(history):
    """Re-run SM-2 over the genuine answers only. Returns (ef, interval, reps, last_date)."""
    ef, interval, reps, last = 2.5, 0, 0, None
    for e in sorted(history, key=lambda x: x.get("date", "")):
        q = max(0, min(5, int(e.get("quality", 0))))
        if q < 3:
            reps, interval = 0, 1
        else:
            if reps == 0:
                interval = 1
            elif reps == 1:
                interval = 6
            else:
                interval = max(1, round(interval * ef))
            reps += 1
        ef = max(1.3, round(ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)), 4))
        last = e.get("date")
    return ef, interval, reps, last


def repair(state):
    cards = state["cards"]
    masked = find_masked_reset_days(cards)
    stats = {"cards": len(cards), "answered": 0, "never_answered": 0,
             "rows_before": 0, "rows_after": 0, "mature": 0,
             "masked_days": sorted(masked)}

    for cid, c in cards.items():
        hist = c.get("history", [])
        stats["rows_before"] += len(hist)

        genuine = [e for e in hist if is_genuine(e, masked)]
        genuine.sort(key=lambda e: e.get("date", ""))

        ef, interval, reps, last = replay(genuine)

        if last:
            stats["answered"] += 1
            next_review = (date.fromisoformat(last) + timedelta(days=interval)).isoformat()
            last_presented = last
            penalised_for = None
        else:
            # Delivered but never genuinely answered: neutral EF, still owed a
            # review, ordered by the pill it came from. Stamp penalised_for so
            # the new FASE 3 does not charge it again for that presentation.
            stats["never_answered"] += 1
            pd = card_pill_date(cid) or "2026-05-14"
            next_review, last_presented = pd, pd
            penalised_for = pd

        stats["rows_after"] += len(genuine)
        if interval > 21:
            stats["mature"] += 1

        c["ef"] = ef
        c["interval"] = interval
        c["repetitions"] = reps
        c["next_review"] = next_review
        c["last_presented"] = last_presented
        c["last_quality"] = genuine[-1].get("quality") if genuine else None
        c["history"] = genuine
        if penalised_for:
            c["penalised_for"] = penalised_for
        else:
            c.pop("penalised_for", None)

    return state, stats


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "sm2_state.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "sm2_state.repaired.json"
    state = json.load(open(src))
    state, stats = repair(state)
    with open(dst, "w") as f:
        json.dump(state, f, indent=2)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
