"""Reproduce the reported BM continuation freeze (T1 -> T2) at session level.

The customer's own audit trail (session sess_free_fvuo0vc9) is unambiguous about what happened:

  seq 82736  T1  GUARDRAILS_RESULT  decision=ALLOW   -> two drafts, CONFIRMATION_READY
  seq 82747  T2  GUARDRAILS_RESULT  engine=live, decision=BLOCK,
                 risk_category=SCAM_OR_COERCION_SIGNAL, message_key=PAUSE_SCAM
  seq 82748  T2  CONTROL_STOPPED    blocked_by=GUARDRAILS_AGENT
  seq 82749  T2  INTERFACE_LINE     PAUSE_SCAM (BM)

The live model read the customer confirming her OWN payment as a scam. The
SCAM_FALSE_POSITIVE relaxer accepted only SAFETY_INTERVENTION, so a BLOCK verdict walked
straight to the gate and froze the session.

scam_relax_probe already covers both verdict labels, but on a FRESH session. This probe covers
the thing that actually broke: the SECOND turn of a session that is already holding a draft.
A relaxer that sits above the gate should not care about draft state - this proves it, because
"the fix works on a fresh session" is not the same claim as "the fix works on the turn the
customer was actually on".

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/bmc_audit.db AUNTIEJOY_REPO_DB=/tmp/bmc.db \
      AUNTIEJOY_NICKNAMES=/tmp/bmc_nick.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/bm_continuation_probe.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, ".")

from app.models import GuardrailDecision, RiskCategory, GuardrailResult  # noqa: E402
from app.agents import GuardrailsAgent  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"
# The customer's verbatim turns, copied from the audit trail.
T1 = ("Pindahkan seribu dollar dari akun simpanan saya ke akun bersama saya, kemudian "
      "hantarlah lima puluh dolar kepada ah girl.")
T2 = "1000 dollar betul. hantarlah duitnya ke ah girl dari akun semasa."
# A genuine scam on the continuation turn: the relaxer must NOT fire on this one.
REAL_SCAM = "the bank officer says move it to a safe account now"


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def relaxer_fired(events):
    """True when the audit shows the SCAM relaxer overriding the model's verdict."""
    for e in events:
        if e.get("event_type") == "GUARDRAILS_RESULT":
            note = (e.get("payload") or {}).get("note", "") or ""
            if "SCAM_OR_COERCION relaxed to ALLOW" in note:
                return True
    return False


def guardrail_verdict(events):
    for e in events:
        if e.get("event_type") == "GUARDRAILS_RESULT":
            p = e.get("payload") or {}
            return f"{p.get('decision')}/{p.get('risk_category')}"
    return "<none>"


def seed_nickname():
    """T1 says "ah girl", so the taught nickname has to exist. Seeded into the probe's own
    temp store, never the customer's file, so the probe stays self-contained."""
    path = os.environ.get("AUNTIEJOY_NICKNAMES", "nicknames.json")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({CUST: {"ah girl": "payee_mei_001"}}, fh)


async def main():
    ok = True
    seed_nickname()
    orig_run = GuardrailsAgent.run

    # --- T1: the ordinary first turn, exactly as the live run had it (ALLOW) ---
    print("--- T1: the multi-instruction BM opening (live verdict ALLOW) ---")
    r1 = await run_turn(TurnRequest(session_id="sess_bmc_1", text=T1,
                                    via_voice=False, asr_confidence=0.96))
    ok &= check("T1 is not frozen", not r1.session.get("frozen"), str(r1.session.get("state")))
    ok &= check("T1 reaches CONFIRMATION_READY",
                r1.session.get("state") == "CONFIRMATION_READY", str(r1.session.get("state")))

    # --- T2: the continuation, with the live model's false positive replayed verbatim ---
    print("--- T2: continuation where the live model returned BLOCK/SCAM on the customer's own "
          "confirmation ---")

    async def fake_run(turn, ctx):
        if turn.strip() == T2:
            return GuardrailResult(
                decision=GuardrailDecision.BLOCK,
                risk_category=RiskCategory.SCAM_OR_COERCION_SIGNAL,
                risk_level="HIGH", detected_language="EN", message_key="PAUSE_SCAM",
                developer_reason="SIMULATED live over-trigger, copied from seq 82747"), "live"
        return GuardrailsAgent.mock(turn, ctx), "mock"

    GuardrailsAgent.run = staticmethod(fake_run)
    r2 = await run_turn(TurnRequest(session_id="sess_bmc_1", text=T2,
                                    via_voice=False, asr_confidence=0.96))
    ok &= check("T2 is NOT frozen", not r2.session.get("frozen"), str(r2.session.get("state")))
    ok &= check("T2 does not stop on the scam path",
                r2.session.get("outcome") not in ("BLOCK", "SAFETY_INTERVENTION"),
                str(r2.session.get("outcome")))
    ok &= check("T2 reaches CONFIRMATION_READY",
                r2.session.get("state") == "CONFIRMATION_READY", str(r2.session.get("state")))
    ok &= check("the relaxer is what saved it (audit shows the override)", relaxer_fired(r2.events),
                guardrail_verdict(r2.events))
    # Check the CUSTOMER-FACING line, not any event. Two GUARDRAILS_RESULT events are expected:
    # the model's original verdict, then the relaxer's override - so the model's own
    # message_key=PAUSE_SCAM is still on the record (that is the point of an audit trail). What
    # must not exist is an INTERFACE_LINE saying it to the customer.
    said = [e.get("payload", {}).get("message_key") for e in r2.events
            if e.get("event_type") == "INTERFACE_LINE"]
    ok &= check("no PAUSE_SCAM line was said to the customer", "PAUSE_SCAM" not in said,
                f"lines said: {said}")

    # --- Control: a genuine scam on the continuation turn must STILL freeze ---
    print("--- control: a genuine scam on the continuation turn must still freeze ---")

    async def scam_run(turn, ctx):
        if turn.strip() == REAL_SCAM:
            return GuardrailResult(
                decision=GuardrailDecision.BLOCK,
                risk_category=RiskCategory.SCAM_OR_COERCION_SIGNAL,
                risk_level="CRITICAL", detected_language="EN", message_key="PAUSE_SCAM",
                developer_reason="real coercion story"), "live"
        return GuardrailsAgent.mock(turn, ctx), "mock"

    GuardrailsAgent.run = staticmethod(scam_run)
    r3 = await run_turn(TurnRequest(session_id="sess_bmc_2", text=REAL_SCAM,
                                    via_voice=False, asr_confidence=0.96))
    ok &= check("the coercion story IS protected",
                r3.session.get("state") in ("FROZEN", "COOLING_OFF"), str(r3.session.get("state")))
    ok &= check("and the relaxer did NOT fire on it", not relaxer_fired(r3.events),
                guardrail_verdict(r3.events))

    GuardrailsAgent.run = staticmethod(orig_run)

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
