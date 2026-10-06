"""Reproduce the two reported issues under the deterministic (mock) reference path.

Issue 1 (ZH): "从我的储蓄账户转一千新元到我的联名账户，然后转五十新元给阿美。" -> only first payment.
Issue 2 (BM): turn2 "1000 dollar betul. hantarlah duitnya ke ah girl dari akun semasa." -> scam freeze.

Run with a FRESH repo db so taught nicknames (ah girl / 阿美) exist.
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.parsing import (  # noqa: E402
    split_instructions, parse_source_account_phrase, parse_payee_phrase, parse_amount,
)
from app.agents import GuardrailsAgent  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"

ZH = "从我的储蓄账户转一千新元到我的联名账户，然后转五十新元给阿美。"
BM_T1 = "Pindahkan seribu dollar dari akun simpanan saya ke akun bersama saya, kemudian hantarlah lima puluh dolar kepada ah girl."
BM_T2 = "1000 dollar betul. hantarlah duitnya ke ah girl dari akun semasa."


def spoken(resp):
    return [ev["payload"]["text"] for ev in resp.events
            if ev["event_type"] == "INTERFACE_LINE"]


async def main():
    print("=== ZH: split + resolution ===")
    parts = split_instructions(ZH, CUST)
    print("split_instructions ->", parts)
    for i, p in enumerate(parts):
        print(f"  clause {i+1}: source={parse_source_account_phrase(p)!r} "
              f"payee={parse_payee_phrase(p, CUST)!r} amount={parse_amount(p)!r}")

    print("\n=== ZH: full turn ===")
    r = await run_turn(TurnRequest(session_id="sess_zh", text=ZH,
                                   via_voice=False, asr_confidence=0.96))
    print("state:", r.session.get("state"), "| outcome:", r.session.get("outcome"))
    draft = r.session.get("draft") or {}
    print("draft keys:", list(draft.keys()))
    print("spoken:")
    for line in spoken(r):
        print("   ", line)

    print("\n=== BM: guardrail on turn 2 (mock) ===")
    g = GuardrailsAgent.mock(BM_T2, {"session": {}})
    print("decision:", g.decision, "| category:", g.risk_category,
          "| key:", g.message_key, "| reason:", g.developer_reason)

    print("\n=== BM: two-turn conversation (mock) ===")
    r1 = await run_turn(TurnRequest(session_id="sess_bm", text=BM_T1,
                                    via_voice=False, asr_confidence=0.96))
    print("T1 state:", r1.session.get("state"))
    for line in spoken(r1):
        print("   T1:", line)
    r2 = await run_turn(TurnRequest(session_id="sess_bm", text=BM_T2,
                                    via_voice=False, asr_confidence=0.96))
    print("T2 state:", r2.session.get("state"), "| outcome:", r2.session.get("outcome"))
    for line in spoken(r2):
        print("   T2:", line)


asyncio.run(main())
