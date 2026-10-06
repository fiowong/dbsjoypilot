"""DEMO 6 Voice Mode: an unverified speaker is told NOTHING about the payment.

The invariant behind the fix, in one sentence: while Stage 1 has HELD the request because it
could not verify the speaker, the turn speaks exactly ONE line - the identity step-up - and no
other. Not the read-back, not a clarification ("who should I pay?"), not a suggestion
("did you mean Mei Tan?"). The pipeline still builds the draft, but silently.

Before the fix the hold was enforced at ONE output site (the Stage-10 read-back), so every path
that ended earlier spoke straight past it. Proof of the old behaviour, with the LLM blanked:

    "Send fifty dollars." + SPEAKER_MISMATCH
      STAGE 1  [VOICE_STEP_UP]      I could not confirm you are the registered customer...
      STAGE 5  [RECIPIENT_MISSING]  Mei Ling, who should I pay?     <-- the bug

What this probe asserts:
  1. A held turn speaks ONE line, and it is the step-up line.
  2. The held turn still BUILDS the draft (the front end presents the payment only after
     FaceID, so the draft has to exist while the speaker is unverified).
  3. A line that was withheld is still visible in the audit trail, as its own event.
  4. The gate is scoped to the HOLD only: an ordinary turn still asks its question and still
     reads the payment back.
  5. The gate does NOT leak into /confirm: while the hold is still set, the FaceID-FAILED line
     must still be spoken, or the failed-biometrics demo would end in silence.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/vm.db AUNTIEJOY_NICKNAMES=/tmp/vm_nick.json \
      PYTHONPATH="$PWD" .venv/bin/python -B tools/voice_mode_probe.py
(Blank the key so every agent uses its deterministic mock instead of retrying a dead LLM.)
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.orchestrator import (  # noqa: E402
    TurnRequest, ConfirmRequest, run_turn, run_confirmation,
)

# The scripted DEMO6 command: a payment that RESOLVES, so the hold still has a draft to protect.
DEMO6 = "Transfer two thousand dollars to Mei."
# A payment with NO recipient: the deterministic path reaches "who should I pay?", which is the
# line that used to escape the hold.
NO_RECIPIENT = "Send fifty dollars."


def lines(resp):
    """[(stage, message_key, text)] for every customer-facing line this turn actually spoke."""
    return [(ev["stage"], ev["payload"]["message_key"], ev["payload"]["text"])
            for ev in resp.events if ev["event_type"] == "INTERFACE_LINE"]


def withheld(resp):
    """[message_key] for every line the hold refused to speak."""
    return [ev["payload"]["message_key"] for ev in resp.events
            if ev["event_type"] == "LINE_WITHHELD_IDENTITY_HOLD"]


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


async def turn(text, voice_risk, sid):
    return await run_turn(TurnRequest(session_id=sid, text=text, via_voice=True,
                                      asr_confidence=0.94, voice_risk=voice_risk))


async def main():
    ok = True

    print("--- 1. HELD turn that resolves: one line, and it is the step-up ---")
    r = await turn(DEMO6, "SPEAKER_MISMATCH", "sess_vm_1")
    spoken = lines(r)
    ok &= check("exactly ONE customer-facing line", len(spoken) == 1, str(len(spoken)))
    ok &= check("that line is VOICE_STEP_UP",
                bool(spoken) and spoken[0][1] == "VOICE_STEP_UP", str(spoken))
    ok &= check("the payment is NOT read back",
                not any(k == "INITIATION_READBACK" for _, k, _ in spoken))

    print("\n--- 2. ...but the draft is still built, so FaceID can release it ---")
    s = r.session
    ok &= check("state is CONFIRMATION_READY", s.get("state") == "CONFIRMATION_READY",
                str(s.get("state")))
    ok &= check("a draft exists", bool(s.get("draft")))
    ok &= check("the hold is still exposed to the front end",
                s.get("step_up_required") is True
                and s.get("step_up_reason") == "SPEAKER_MISMATCH")

    print("\n--- 3. HELD turn with no recipient: the question is withheld, not spoken ---")
    r2 = await turn(NO_RECIPIENT, "SPEAKER_MISMATCH", "sess_vm_2")
    spoken2 = lines(r2)
    ok &= check("RECIPIENT_MISSING was NOT spoken",
                not any(k == "RECIPIENT_MISSING" for _, k, _ in spoken2), str(spoken2))
    ok &= check("still exactly ONE line, the step-up",
                len(spoken2) == 1 and spoken2[0][1] == "VOICE_STEP_UP", str(spoken2))
    ok &= check("the withheld question IS in the audit trail",
                "RECIPIENT_MISSING" in withheld(r2), str(withheld(r2)))

    print("\n--- 4. NO hold: the gate must stay shut, ordinary turns still speak ---")
    r3 = await turn(NO_RECIPIENT, "OK", "sess_vm_3")
    ok &= check("RECIPIENT_MISSING is still spoken when identity is fine",
                any(k == "RECIPIENT_MISSING" for _, k, _ in lines(r3)), str(lines(r3)))
    r4 = await turn(DEMO6, "OK", "sess_vm_4")
    ok &= check("the read-back is still spoken when identity is fine",
                any(k == "INITIATION_READBACK" for _, k, _ in lines(r4)), str(lines(r4)))
    ok &= check("nothing was withheld", withheld(r4) == [], str(withheld(r4)))

    print("\n--- 5. the gate must NOT reach /confirm: failed FaceID still speaks ---")
    rc = await run_confirmation(ConfirmRequest(
        session_id="sess_vm_1", fields=["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
        language="EN", biometric=True, faceid_result="FAILED"))
    clines = [(ev["payload"].get("message_key"), ev["payload"].get("text"))
              for ev in rc.events if ev["event_type"] == "INTERFACE_LINE"]
    ok &= check("BIOMETRIC_FAILED is spoken, not withheld",
                any(k == "BIOMETRIC_FAILED" for k, _ in clines), str(clines))
    ok &= check("the failed biometrics path still REJECTS",
                rc.session.get("state") == "FROZEN" and rc.session.get("outcome") == "REJECTED",
                f"{rc.session.get('state')}/{rc.session.get('outcome')}")

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
