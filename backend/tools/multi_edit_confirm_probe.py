"""The 500 that made BM FaceID look broken (customer report, 6 Oct).

Reported as "faceid for melayu doesn't work" with the BM `unreachable` line. It is NOT a
language bug and NOT a network bug. The customer's own audit trail shows three BM sessions
(sess_free_8yi6likm / _gd3busmf / _wf2awsyn) all stopping at the SAME event:

    ... CONFIRMATION_COMPLETE 11, MOCK_FACEID 12, PAYLOAD_SIGNED 13, GATEWAY_REQUEST_RECEIVED 14
    <nothing>

No GATEWAY_DECISION. The ZH session that worked (sess_free_bynia1au) ran the same shape and
completed. The difference is one word of LLM classification on turn 2:

    ZH turn 2 -> intent PAYMENT_CLARIFICATION -> REVIEW_RESTARTED -> validation left intact
    BM turn 2 -> intent EDIT_PAYMENT          -> EDIT_INVALIDATION -> session.validation = None

EDIT_INVALIDATION nulls session.validation (orchestrator.py ~1061) because an edit supersedes the
payment on screen. But in a MULTI turn the amendment can target a QUEUED draft (dr_002) while the
draft actually under review is dr_001 - and the merge only restores validation when the amended
draft IS the open one (`if match is session.draft`). So the open draft keeps validation=None.

/confirm then evaluates `compare_validation_result(session.validation, ...)` as an ARGUMENT to
gateway_verify, which dereferences `validation.field_checks` -> AttributeError on None -> HTTP 500.
A 500 is not a 409, so the frontend's `else` branch reports "could not reach the payment system".

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/mec_audit.db AUNTIEJOY_REPO_DB=/tmp/mec.db \
      AUNTIEJOY_NICKNAMES=/tmp/mec_nick.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/multi_edit_confirm_probe.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, ".")

from app.models import IntentResult  # noqa: E402
from app.agents import IntentClassifierAgent  # noqa: E402
from app.orchestrator import (TurnRequest, ConfirmRequest, run_turn,  # noqa: E402
                              run_confirmation)

CUST = "cust_auntie_001"
T1 = ("Pindahkan seribu dollar dari akun simpanan saya ke akun bersama saya, kemudian "
      "hantarlah lima puluh dolar kepada ah girl.")
# The BM continuation that the live model labelled EDIT_PAYMENT (audit seq 82201).
T2 = "1000 dollar betul. hantarlah duitnya ke ah girl dari akun semasa."


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def seed_nickname():
    path = os.environ.get("AUNTIEJOY_NICKNAMES", "nicknames.json")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({CUST: {"ah girl": "payee_mei_001"}}, fh)


async def main():
    ok = True
    seed_nickname()
    orig = IntentClassifierAgent.run
    sid = "sess_mec_1"

    print("--- T1: the BM multi-instruction opening ---")
    r1 = await run_turn(TurnRequest(session_id=sid, text=T1, via_voice=False, asr_confidence=0.96))
    ok &= check("T1 queues two drafts", r1.session.get("state") == "CONFIRMATION_READY",
                str(r1.session.get("state")))

    print("--- T2: continuation the live model called EDIT_PAYMENT ---")

    async def fake_intent(turn, ctx):
        if turn.strip() == T2:
            return IntentResult(intent="EDIT_PAYMENT", confidence=0.9, detected_language="BM",
                                reason="SIMULATED live classification, copied from seq 82201"), "live"
        return IntentClassifierAgent.mock(turn, ctx), "mock"

    IntentClassifierAgent.run = staticmethod(fake_intent)
    r2 = await run_turn(TurnRequest(session_id=sid, text=T2, via_voice=False, asr_confidence=0.96))
    IntentClassifierAgent.run = staticmethod(orig)

    events2 = [e.get("event_type") for e in r2.events]
    ok &= check("T2 invalidated the downstream results (EDIT_INVALIDATION fired)",
                "EDIT_INVALIDATION" in events2)
    ok &= check("T2 amended the QUEUED draft, not the open one (MULTI_ITEM_UPDATED)",
                "MULTI_ITEM_UPDATED" in events2)
    ok &= check("T2 still leaves a draft under review", bool(r2.session.get("draft")),
                str(r2.session.get("state")))

    print("--- FaceID /confirm: this is where it used to die with a 500 ---")
    try:
        r3 = await run_confirmation(ConfirmRequest(
            session_id=sid, fields=["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
            language="BM", biometric=True, faceid_result="SUCCESS"))
    except Exception as exc:                                  # noqa: BLE001
        ok &= check("run_confirmation does not blow up", False, f"{type(exc).__name__}: {exc}")
        print("\nRESULT: FAILURES PRESENT")
        sys.exit(1)

    # In a MULTI session the correct end state is CONFIRMATION_READY, not EXECUTED: the first
    # instruction is executed and the review ADVANCES to the next queued draft (this is exactly how
    # the customer's working ZH session ended - LEDGER_RECORDED then NEXT_INSTRUCTION_QUEUED). So
    # the assertions are on the events: money actually moved, and the queue advanced.
    ev3 = [e.get("event_type") for e in r3.events]
    ok &= check("no exception escaped /confirm (would be HTTP 500)", True, "returned a response")
    ok &= check("the first instruction really executed (LEDGER_RECORDED)", "LEDGER_RECORDED" in ev3)
    ok &= check("the review advanced to the queued instruction (NEXT_INSTRUCTION_QUEUED)",
                "NEXT_INSTRUCTION_QUEUED" in ev3)
    ok &= check("the session is left on the next draft, not stuck",
                r3.session.get("state") == "CONFIRMATION_READY"
                and bool(r3.session.get("draft")), str(r3.session.get("state")))

    # --- The backstop: if any future path leaves validation missing again, /confirm must ANSWER
    #     (409) rather than crash (500). A 500 reaches the customer as "could not reach the payment
    #     system", which is untrue and unactionable, so this must never regress to a 500. ---
    print("--- backstop: a draft with no validation must be refused, not 500 ---")
    from fastapi import HTTPException  # noqa: E402
    from app.session import get_session  # noqa: E402
    s = get_session(sid)
    saved, s.validation = s.validation, None
    try:
        await run_confirmation(ConfirmRequest(
            session_id=sid, fields=["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
            language="BM", biometric=True, faceid_result="SUCCESS"))
        ok &= check("a missing validation is refused, not silently executed", False,
                    "it returned a response instead of refusing")
    except HTTPException as exc:
        ok &= check("a missing validation is refused with 409 (not a 500)",
                    exc.status_code == 409, f"HTTP {exc.status_code}: {exc.detail}")
    except Exception as exc:                                  # noqa: BLE001
        ok &= check("a missing validation is refused with 409 (not a 500)", False,
                    f"{type(exc).__name__}: {exc}")
    finally:
        s.validation = saved

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
