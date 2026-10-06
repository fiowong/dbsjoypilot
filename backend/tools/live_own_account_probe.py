"""Own-account moves under the LIVE Payment Agent, and the safeguard that fixes the shape.

The bug: "From my savings account to my joint account, move one grand." worked offline and failed
in the recording. PAYMENT_SYSTEM_PROMPT only ever described "one SGD transfer to a saved payee",
so the live model reads an own-account move as a saved-payee transfer with no recipient and the
turn dies asking "who should I pay?" - about the customer's OWN account. PaymentAgent.mock has an
own-account branch, which is exactly why the offline path gets it right.

The safeguard (orchestrator.py, TRANSACTION_TYPE_SETTLED): the transaction TYPE is deterministic,
because the customer either named a biller, their card, one of their OWN accounts, or a saved
payee, and all four are recognised by parsers reading the trusted repository. It mirrors the
detection order PaymentAgent.mock and _instruction_proposal already use.

There is no API key here, so this probe stands in for the live agent by returning the proposals a
model actually produces; everything downstream is the real orchestrator, resolver and catalogue.
Two live shapes are simulated, because we cannot know which one the model returns:
  (a) it reports a missing recipient and still carries the amount
  (b) it comes back PROPOSAL_READY but mistypes the move as a saved-payee transfer

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/lo_audit.db AUNTIEJOY_REPO_DB=/tmp/lo.db \
      AUNTIEJOY_NICKNAMES=/tmp/lo.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/live_own_account_probe.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app import orchestrator  # noqa: E402
from app.agents import PaymentAgent  # noqa: E402
from app.models import PaymentProposal  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

REAL_RUN = PaymentAgent.run

MOVE = "From my savings account to my joint account, move one grand."
MOVE_NO_AMOUNT = "From my savings account to my joint account, move."
PAYEE_TURN = "Send fifty dollars to Mei."
DEMO3 = "Send one fifty to John from my everyday account."
BILL_TURN = "Pay my StarHub bill."


def fake(status, transaction_type, recipient, amount, field):
    async def run(turn, ctx):
        return (PaymentProposal(
            status=status, detected_language="EN", recipient_phrase=recipient,
            amount_minor=amount, currency="SGD", transaction_type=transaction_type,
            source_account_phrase=None, clarification_field=field,
            clarification_question=None, risk_flags=[]), "live-simulated")
    return run


def keys(resp):
    return [ev["payload"]["message_key"] for ev in resp.events
            if ev["event_type"] == "INTERFACE_LINE"]


def text(resp, key=None):
    for ev in resp.events:
        if ev["event_type"] == "INTERFACE_LINE" and (key is None
                                                     or ev["payload"]["message_key"] == key):
            return ev["payload"]["text"]
    return ""


def settled(resp):
    return [ev["payload"] for ev in resp.events
            if ev["event_type"] == "TRANSACTION_TYPE_SETTLED"]


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


async def turn(t, sid, voice_risk="OK"):
    return await run_turn(TurnRequest(session_id=sid, text=t, via_voice=True,
                                      asr_confidence=0.94, voice_risk=voice_risk))


async def main():
    ok = True

    print("--- 1. live shape (a): missing recipient, amount carried ---")
    orchestrator.PaymentAgent.run = fake("NEEDS_CLARIFICATION", None, None, 100000, "RECIPIENT")
    r = await turn(MOVE, "sess_lo_1")
    ok &= check("'who should I pay?' is NOT asked", "RECIPIENT_MISSING" not in keys(r), str(keys(r)))
    s = settled(r)
    ok &= check("TRANSACTION_TYPE_SETTLED is in the audit", len(s) == 1, str(s))
    ok &= check("the type is OWN_ACCOUNT_TRANSFER",
                s and s[0]["transaction_type"] == "OWN_ACCOUNT_TRANSFER", str(s))
    ok &= check("a draft was built", bool(r.session.get("draft")))
    ok &= check("the read-back moves money to the JOINT account",
                "Joint Account" in text(r, "INITIATION_READBACK"), text(r)[:110])

    print("\n--- 2. live shape (b): PROPOSAL_READY but mistyped as a saved-payee transfer ---")
    r2 = await turn(MOVE, "sess_lo_2")
    ok &= check("still corrected to OWN_ACCOUNT_TRANSFER",
                settled(r2) and settled(r2)[0]["transaction_type"] == "OWN_ACCOUNT_TRANSFER",
                str(settled(r2)))
    ok &= check("and still reads the move back", "Joint Account" in text(r2, "INITIATION_READBACK"))

    print("\n--- 3. no amount: the AMOUNT question is the one asked ---")
    orchestrator.PaymentAgent.run = fake("NEEDS_CLARIFICATION", None, None, None, "RECIPIENT")
    r3 = await turn(MOVE_NO_AMOUNT, "sess_lo_3")
    ok &= check("AMOUNT is asked, not RECIPIENT",
                "AMOUNT_MISSING" in keys(r3) and "RECIPIENT_MISSING" not in keys(r3),
                str(keys(r3)))

    print("\n--- 4. Voice Mode: the draft survives for FaceID to release ---")
    r4 = await turn(MOVE, "sess_lo_4", "SPEAKER_MISMATCH")
    ok &= check("only the step-up line is spoken", keys(r4) == ["VOICE_STEP_UP"], str(keys(r4)))
    ok &= check("a draft WAS built", bool(r4.session.get("draft")))

    print("\n--- 5. regressions: the safeguard stays shut when the turn is NOT an own-account move ---")
    # A live shape that is already correct for these turns: a saved payee with its amount.
    orchestrator.PaymentAgent.run = fake("PROPOSAL_READY", None, "Mei Tan", 5000, None)
    r5 = await turn(PAYEE_TURN, "sess_lo_5")
    ok &= check("a saved-payee turn is untouched", settled(r5) == [], str(settled(r5)))
    ok &= check("and still reads back Mei Tan", "Mei Tan" in text(r5, "INITIATION_READBACK"),
                text(r5)[:90])
    r6 = await turn(DEMO3, "sess_lo_6")
    ok &= check("DEMO 3 ('from my everyday account') is untouched", settled(r6) == [])
    orchestrator.PaymentAgent.run = fake("PROPOSAL_READY", "BILL_PAYMENT", "StarHub", 8640, None)
    r7 = await turn(BILL_TURN, "sess_lo_7")
    ok &= check("a bill payment is untouched", settled(r7) == [], str(settled(r7)))

    print("\n--- 6. with the real (mock) agent nothing is overridden ---")
    orchestrator.PaymentAgent.run = REAL_RUN
    r8 = await turn(MOVE, "sess_lo_8")
    ok &= check("nothing was overridden", settled(r8) == [], str(settled(r8)))
    ok &= check("the move still reads back", "Joint Account" in text(r8, "INITIATION_READBACK"))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
