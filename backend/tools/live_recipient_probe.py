"""The live Payment Agent's RECIPIENT over-trigger, and the safeguard that undoes it.

The bug: "Send John Smith 50 dollars from my savings account." came back from the LIVE Payment
Agent as NEEDS_CLARIFICATION / clarification_field="RECIPIENT". The turn then ended at stage 5
and Joy asked "who should I pay?" about a person the customer had just named. In Voice Mode it
was worse: no draft was built, so nothing was left for FaceID to release.

The Payment Agent is never told who the customer's saved payees are, so the recipient is the one
field it can only ever COPY out of the turn. The safeguard: if it fails to copy it but
deterministic code can already read a saved payee out of the turn, deterministic code owns the
field (orchestrator.py, the RECIPIENT_RECOVERED block).

This probe drives that path by standing in for the live agent - there is no API key here, so
`PaymentAgent.run` is replaced with one that returns the exact over-trigger the model produced.
Everything downstream is the real orchestrator, the real resolver and the real catalogue.

It also pins down what the safeguard must NOT do: never invent a binding, never swallow the
unknown-name learning moment, never answer the amount question with a recipient question.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/lr_audit.db AUNTIEJOY_REPO_DB=/tmp/lr.db \
      AUNTIEJOY_NICKNAMES=/tmp/lr.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/live_recipient_probe.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app import orchestrator  # noqa: E402
from app.agents import PaymentAgent  # noqa: E402
from app.models import PaymentProposal  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

REAL_RUN = PaymentAgent.run

# The sentence from the bug report.
SHOT = "Send John Smith 50 dollars from my savings account."
NO_PAYEE = "Send fifty dollars."
UNKNOWN_NAME = "Send Ah Boy fifty dollars."
NO_AMOUNT = "Send John Smith money."


def over_trigger(recipient_phrase, amount_minor):
    """A PaymentAgent.run that reproduces the live model's over-trigger exactly."""
    async def run(turn, ctx):
        return (PaymentProposal(
            status="NEEDS_CLARIFICATION", detected_language="EN",
            recipient_phrase=recipient_phrase, amount_minor=amount_minor, currency="SGD",
            clarification_field="RECIPIENT", clarification_question="Who should I pay?",
            risk_flags=[]), "live-simulated")
    return run


def lines(resp):
    return [(ev["stage"], ev["payload"]["message_key"], ev["payload"]["text"])
            for ev in resp.events if ev["event_type"] == "INTERFACE_LINE"]


def keys(resp):
    return [k for _, k, _ in lines(resp)]


def event(resp, event_type):
    return [ev["payload"] for ev in resp.events if ev["event_type"] == event_type]


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


async def turn(text, voice_risk, sid):
    return await run_turn(TurnRequest(session_id=sid, text=text, via_voice=True,
                                      asr_confidence=0.94, voice_risk=voice_risk))


async def main():
    ok = True

    print("--- 1. the reported bug, live over-trigger: the recipient is recovered ---")
    orchestrator.PaymentAgent.run = over_trigger(None, 5000)
    r = await turn(SHOT, "OK", "sess_lr_1")
    ok &= check("'who should I pay?' is NOT asked", "RECIPIENT_MISSING" not in keys(r), str(keys(r)))
    rec = event(r, "RECIPIENT_RECOVERED")
    ok &= check("RECIPIENT_RECOVERED is in the audit", len(rec) == 1, str(rec))
    # parse_payee_phrase returns the phrase as spoken, so compare folded; the read-back below is
    # what proves the payee record was bound correctly.
    ok &= check("the recovered recipient is John Smith",
                rec and str(rec[0]["recipient_phrase"]).lower() == "john smith",
                rec[0]["recipient_phrase"] if rec else "None")
    ok &= check("the payment reaches CONFIRMATION_READY with a draft",
                r.session.get("state") == "CONFIRMATION_READY" and bool(r.session.get("draft")),
                str(r.session.get("state")))
    ok &= check("the read-back names John Smith",
                any("John Smith" in t for _, k, t in lines(r) if k == "INITIATION_READBACK"),
                str(lines(r)))

    print("\n--- 2. ...and in Voice Mode, the draft survives for FaceID to release ---")
    r2 = await turn(SHOT, "SPEAKER_MISMATCH", "sess_lr_2")
    ok &= check("only the step-up line is spoken", keys(r2) == ["VOICE_STEP_UP"], str(keys(r2)))
    ok &= check("a draft WAS built, so FaceID has something to release",
                bool(r2.session.get("draft")))
    ok &= check("state is CONFIRMATION_READY",
                r2.session.get("state") == "CONFIRMATION_READY", str(r2.session.get("state")))

    print("\n--- 3. NO payee named: the question must still be asked ---")
    r3 = await turn(NO_PAYEE, "OK", "sess_lr_3")
    ok &= check("RECIPIENT_MISSING is still asked", "RECIPIENT_MISSING" in keys(r3), str(keys(r3)))
    ok &= check("nothing was fabricated", event(r3, "RECIPIENT_RECOVERED") == [])

    print("\n--- 4. an UNKNOWN name: the learning moment is preserved ---")
    r4 = await turn(UNKNOWN_NAME, "OK", "sess_lr_4")
    # Either branch of the unknown-name machinery is correct here (a fuzzy "did you mean X?" when
    # one payee is closest, or the "which of your payees is that?" list otherwise). What must NOT
    # happen is the flat "who should I pay?", which throws away the name the customer gave.
    ok &= check("the unknown name is still asked about, not 'who should I pay?'",
                ("NICKNAME_UNKNOWN" in keys(r4) or "PAYEE_DID_YOU_MEAN" in keys(r4))
                and "RECIPIENT_MISSING" not in keys(r4), str(keys(r4)))
    print("      (asked:", ", ".join(keys(r4)) or "nothing", ")")

    print("\n--- 5. no amount either: the AMOUNT question is the one that gets asked ---")
    orchestrator.PaymentAgent.run = over_trigger(None, None)
    r5 = await turn(NO_AMOUNT, "OK", "sess_lr_5")
    ok &= check("AMOUNT_MISSING is asked", "AMOUNT_MISSING" in keys(r5), str(keys(r5)))
    ok &= check("the recipient is no longer in question",
                "RECIPIENT_MISSING" not in keys(r5), str(keys(r5)))

    print("\n--- 6. with the real (mock) agent the safeguard is a no-op ---")
    orchestrator.PaymentAgent.run = REAL_RUN
    r6 = await turn(SHOT, "OK", "sess_lr_6")
    ok &= check("the ordinary read-back happens", "INITIATION_READBACK" in keys(r6), str(keys(r6)))
    ok &= check("nothing was overridden", event(r6, "RECIPIENT_RECOVERED") == [])

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
