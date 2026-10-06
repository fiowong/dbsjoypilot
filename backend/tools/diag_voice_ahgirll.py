"""Reproduce the voice-input freeze on the mis-spelled nickname "ah girll".

User said (voice): "Move a grand from my savings account to my joint account, then send 150 to
ah girll." -> Joy froze with FROZEN_RISK ("I've paused this payment for safety. No money was sent.")
The correctly-spelled "ah girl" / "John Smith" versions work. Diagnose WHY the mis-spelling freezes.

Run with a FRESH repo db (nicknames seeded).
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.parsing import split_instructions, parse_payee_phrase  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"
MIS = "Move a grand from my savings account to my joint account, then send 150 to ah girll."
OK = "Move a grand from my savings account to my joint account, then send 150 to ah girl."


async def dump(label, text):
    print(f"\n========== {label} ==========")
    parts = split_instructions(text, CUST)
    print("split_instructions ->", parts)
    for i, p in enumerate(parts):
        print(f"  clause {i+1}: payee={parse_payee_phrase(p, CUST)!r}")
    r = await run_turn(TurnRequest(session_id=f"sess_{label}", text=text,
                                   via_voice=True, asr_confidence=0.41, voice_risk="OK"))
    print("state:", r.session.get("state"), "| outcome:", r.session.get("outcome"),
          "| frozen:", r.session.get("frozen"))
    draft = r.session.get("draft") or {}
    print("draft beneficiary_id:", draft.get("beneficiary_id"),
          "| payee_display:", draft.get("payee_display"),
          "| amount_minor:", draft.get("amount_minor"))
    print("--- events (type : key) ---")
    for ev in r.events:
        t = ev["event_type"]
        if t in ("GUARDRAILS_RESULT", "RISK_POLICY_RESULT", "TRANSACTION_FROZEN",
                 "RISK_REJECTED", "COOLING_OFF_APPLIED", "PAYEE_DID_YOU_MEAN",
                 "MULTI_INSTRUCTION_UNRESOLVED", "INTERFACE_LINE"):
            payload = ev.get("payload", {})
            key = payload.get("message_key") if isinstance(payload, dict) else ""
            reasons = payload.get("reasons") if isinstance(payload, dict) else ""
            print(f"  {t} : {key} {reasons if reasons else ''}")


async def main():
    await dump("MISPELLED_ah_girll", MIS)
    await dump("CORRECT_ah_girl", OK)


asyncio.run(main())
