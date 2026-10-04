"""Time TWO payments made back-to-back in the SAME chat session.

WHY THIS EXISTS
---------------
A demo observation said a second payment in the same chat behaves oddly "after
about a minute". Reading the code found no cooldown, no throttle and no
input-disabling gate, so this probe measures the real thing instead of guessing:
it completes one payment, then immediately starts a second one on the SAME
session id and times every step.

It answers three questions at once:
  1. Does a second payment even WORK on a session that already finished one?
  2. How long does each step take -- is "about a minute" a wait, or just how
     long a payment takes?
  3. Is the second payment slower than the first? If so the delay is likely the
     LLM provider's live-model budget / rate limiting, not a rule in our code.

Payment 2 deliberately uses "Mei Tan" (the seeded display name) rather than a
nickname, so it cannot stall on an ambiguity clarification and skew the timing.

USAGE (from backend/)
    AUNTIEJOY_DB=/tmp/second_payment.db PYTHONPATH="$PWD" .venv/bin/python -B tools/second_payment_probe.py

READING THE OUTPUT
    Each line is one step with its wall-clock seconds, then the session state.
    A step at ~30s has hit TURN_DEADLINE_S; ~20s has hit an agent ceiling.
    If payment 2 does not reach EXECUTED, that is the real "odd thing" and a
    bug, not a wait -- fix the code rather than telling the customer to wait.
"""
import asyncio
import sys
import time
import uuid

from app.config import Settings
from app.orchestrator import (ConfirmRequest, TurnRequest, run_confirmation,
                              run_turn)
from app.session import get_session

CONFIRM_FIELDS = ["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"]


def snap(session):
    """The three fields that say whether money actually moved.

    Read through to_public(), not off the Session object: `ledger` is DERIVED at
    publication time ("RECORDED" if an execution row exists) and simply does not
    exist as an attribute, so touching session.ledger raises.
    """
    pub = session.to_public() if hasattr(session, "to_public") else session
    return (f"state={pub['state']:<24} outcome={str(pub['outcome']):<20} "
            f"ledger={pub['ledger']}")


async def step(label, coro, session_id):
    started = time.monotonic()
    resp = await coro
    took = time.monotonic() - started
    session = get_session(session_id)
    print(f"  {label:<34} {took:6.2f}s   {snap(session)}")
    return took, resp


async def main():
    session_id = f"sess_secondpay_{uuid.uuid4().hex[:6]}"
    get_session(session_id)
    print(f"engine={'live' if Settings.LIVE_LLM else 'MOCK (no key)'}  "
          f"model={Settings.LLM_MODEL}")
    print(f"session={session_id}\n")

    print("PAYMENT 1 (fresh session)")
    t1, _ = await step(
        "turn: transfer S$50 to Mei",
        run_turn(TurnRequest(session_id=session_id,
                             text="帮我 transfer 五十块给 Mei.",
                             via_voice=True, asr_confidence=0.97)),
        session_id)
    t2, _ = await step(
        "confirm (RECIPIENT/AMOUNT/SOURCE)",
        run_confirmation(ConfirmRequest(session_id=session_id,
                                        fields=CONFIRM_FIELDS, language="ZH",
                                        biometric=True, faceid_result="SUCCESS")),
        session_id)
    s = get_session(session_id).to_public()
    print(f"  -> payment 1 total {t1 + t2:.2f}s\n")

    if s["state"] != "EXECUTED":
        print("  !! payment 1 did not execute; the baseline itself is broken, "
              "so the second-payment question cannot be measured.")
        return 1

    print("PAYMENT 2 (SAME session, started immediately)")
    t3, _ = await step(
        "turn: transfer S$20 to Mei Tan",
        run_turn(TurnRequest(session_id=session_id,
                             text="Transfer twenty dollars to Mei Tan.",
                             via_voice=True, asr_confidence=0.97)),
        session_id)
    t4, _ = await step(
        "confirm (RECIPIENT/AMOUNT/SOURCE)",
        run_confirmation(ConfirmRequest(session_id=session_id,
                                        fields=CONFIRM_FIELDS, language="EN",
                                        biometric=True, faceid_result="SUCCESS")),
        session_id)
    s2 = get_session(session_id).to_public()
    print(f"  -> payment 2 (reused session) total {t3 + t4:.2f}s\n")

    # Phase 3 isolates the cause. It is the SAME payment as phase 2, made at the same
    # point in the run, on a brand-new session instead of the finished one. If this one
    # executes and phase 2 did not, the culprit is session REUSE -- not "the second
    # payment", not time, and not the model. That is what tells us the fix is to give
    # each payment a fresh session rather than to tell the customer to wait.
    print("PAYMENT 3 (FRESH session - the proposed fix)")
    fresh = f"sess_freshpay_{uuid.uuid4().hex[:6]}"
    get_session(fresh)
    t5, _ = await step(
        "turn: transfer S$20 to Mei Tan",
        run_turn(TurnRequest(session_id=fresh,
                             text="Transfer twenty dollars to Mei Tan.",
                             via_voice=True, asr_confidence=0.97)),
        fresh)
    t6, _ = await step(
        "confirm (RECIPIENT/AMOUNT/SOURCE)",
        run_confirmation(ConfirmRequest(session_id=fresh,
                                        fields=CONFIRM_FIELDS, language="EN",
                                        biometric=True, faceid_result="SUCCESS")),
        fresh)
    s3 = get_session(fresh).to_public()
    print(f"  -> payment 3 total {t5 + t6:.2f}s\n")

    print("VERDICT")
    print(f"  payment 1, fresh session     {t1 + t2:6.2f}s  -> {s['state']}")
    print(f"  payment 2, REUSED session    {t3 + t4:6.2f}s  -> "
          f"{s2['state']}/{s2['outcome']}")
    print(f"  payment 3, fresh session     {t5 + t6:6.2f}s  -> {s3['state']}")
    print()

    reused_broken = s2["state"] != "EXECUTED"
    fresh_ok = s3["state"] == "EXECUTED"
    if reused_broken and fresh_ok:
        print("  CONFIRMED: reusing a finished session BREAKS the next payment")
        print(f"             (it stalls at {s2['state']}/{s2['outcome']}), while the")
        print("             identical payment on a fresh session executes fine.")
        print("  => The fix is to start each payment on a FRESH session. It is NOT")
        print("     a 60-second wait, and a 'please wait' card would only hide it.")
        return 0
    if reused_broken and not fresh_ok:
        print("  !! Neither the reused nor a fresh session executes the second")
        print("     payment -- something else is wrong. Investigate before building UI.")
        return 1
    print("  Second payment works on the reused session; there is no bug to fix here.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
