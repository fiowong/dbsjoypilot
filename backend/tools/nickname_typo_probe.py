"""The approved "ah girll" fix: make nickname matching tolerate a one-character mis-spelling.

Root cause of the freeze: voice ASR returned "ah girll" (one stray 'l'). match_in_text() only did
exact matching, so "ah girll" resolved to NOBODY. With no payee, split_instructions collapsed the
turn to a single clause, "150" got glued to the scale word "grand" from the first clause, the
draft amount became S$150,000, and controls.evaluate_risk REJECTED it as above the synthetic limit
-> FROZEN_RISK ("I've paused this payment for safety. No money was sent.").

Fix: match_in_text() now falls back to a tight edit-distance <= 1 match against the customer's
KNOWN nicknames. "ah girll" -> "ah girl" (Mei Tan) resolves the sentence into two clauses and
S$150 is read correctly. "Ah Boy" is more than one edit from "ah girl" and must stay unresolved.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/ty_audit.db AUNTIEJOY_REPO_DB=/tmp/ty.db \
      AUNTIEJOY_NICKNAMES=/tmp/ty.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/nickname_typo_probe.py
(Use a FRESH repo db: the seeded nickname "ah girl" -> Mei Tan only exists after a reseed.)
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.nicknames import match_in_text                          # noqa: E402
from app.parsing import split_instructions, parse_payee_phrase    # noqa: E402
from app.orchestrator import TurnRequest, run_turn               # noqa: E402

CUST = "cust_auntie_001"
MIS = "Move a grand from my savings account to my joint account, then send 150 to ah girll."
OK = "Move a grand from my savings account to my joint account, then send 150 to ah girl."
BOY = "Move a grand from my savings account to my joint account, then send 150 to Ah Boy."


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


async def main():
    ok = True

    print("--- unit: match_in_text tolerates a one-char mis-spelling ---")
    hit = match_in_text(CUST, MIS)
    ok &= check("MIS 'ah girll' resolves to a nickname", hit is not None, str(hit))
    ok &= check("... to 'ah girl' -> Mei Tan",
                hit is not None and hit == ("ah girl", hit[1])
                and hit[1].display_name == "Mei Tan",
                str(hit and (hit[0], hit[1].display_name)))

    print("--- unit: an unrelated name must NOT resolve (distance > 1) ---")
    boy = match_in_text(CUST, BOY)
    ok &= check("'Ah Boy' stays unresolved", boy is None, str(boy))

    print("--- split: the mis-spelled turn separates into two clauses ---")
    parts = split_instructions(MIS, CUST)
    ok &= check("MIS splits into 2 instructions", len(parts) == 2, f"parts={len(parts)}")
    if len(parts) == 2:
        ok &= check("MIS clause 2 payee resolves to Mei Tan",
                    parse_payee_phrase(parts[1], CUST) == "Mei Tan",
                    f"clause2={parts[1]!r} -> {parse_payee_phrase(parts[1], CUST)!r}")
        ok &= check("MIS clause 2 has NO 'grand' (no amount contamination)",
                    "grand" not in parts[1].lower(), parts[1])

    print("--- end-to-end: the mis-spelled voice turn no longer freezes ---")
    r = await run_turn(TurnRequest(session_id="sess_typo_mis", text=MIS,
                                   via_voice=True, asr_confidence=0.41,
                                   voice_risk="SPEAKER_MISMATCH"))
    state = r.session.get("state")
    frozen = r.session.get("frozen")
    ok &= check("MIS turn is NOT frozen", not frozen, f"state={state}")
    ok &= check("MIS turn reaches CONFIRMATION_READY (not FROZEN)",
                state == "CONFIRMATION_READY", str(state))

    print("--- regression: the correctly-spelled turn is still fine ---")
    r2 = await run_turn(TurnRequest(session_id="sess_typo_ok", text=OK,
                                    via_voice=True, asr_confidence=0.41,
                                    voice_risk="SPEAKER_MISMATCH"))
    ok &= check("OK turn reaches CONFIRMATION_READY",
                r2.session.get("state") == "CONFIRMATION_READY", str(r2.session.get("state")))
    ok &= check("OK splits into 2 instructions",
                len(split_instructions(OK, CUST)) == 2, str(split_instructions(OK, CUST)))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
