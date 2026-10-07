"""The approved ZH two-payment continuation fix.

Root cause (user-reported, real money moved): after queuing a two-payment ZH turn
(clause 1: S$1,000 savings -> joint; clause 2: S$50 to Amei), the customer said
"1000是对的，然后从我的经常账户转给阿美" — confirming clause 1 and naming clause 2's source
as current. The system instead amended clause 2 to S$1,000 from Savings, sending the wrong
amount AND the wrong source after FaceID.

Two leaks, both fixed in parsing.py:
  1. SOURCE_ACCOUNT_ALIASES gained "经常账户" -> "current" (Mandarin synonym for current
     account, the customer's natural phrasing).
  2. split_instructions() no longer re-injects a dropped no-target fragment's content back
     into the single returned clause. Previously, "1000是对的" had no payment target so the
     glue logic kept it as a leading no-target fragment, then the target filter dropped it,
     leaving len(resolved) < 2 — and the function returned [original_whole_text], so "1000"
     leaked into parse_amount on the merged text.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/mm_audit.db AUNTIEJOY_REPO_DB=/tmp/mm.db \
      AUNTIEJOY_NICKNAMES=/tmp/mm.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/multi_merge_probe.py
(Use a FRESH repo db: the seeded nickname 阿美 only exists after a reseed.)
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.orchestrator import TurnRequest, run_turn   # noqa: E402
from app.session import get_session                  # noqa: E402
from app.parsing import split_instructions, instruction_text  # noqa: E402

CUST = "cust_auntie_001"

# The two-payment breath that queues clause 1 (joint) and clause 2 (Amei).
TURN1 = "从我的储蓄账户转一千新元到我的联名账户，然后转五十新元给阿美。"
# The continuation: confirm clause 1 ("1000 is correct") and name clause 2's source.
TURN2 = "1000是对的，然后从我的经常账户转给阿美"


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def drafts(sess_id):
    s = get_session(sess_id)
    return [(d.payee_display, d.amount_minor, d.source_account_id,
             d.account_masked, d.transaction_type) for d in s.pending_drafts]


async def main():
    ok = True

    # ---- The reported bug ----
    print("--- the ZH continuation amends clause 2 to S$50 from Current ---")
    sess = "sess_mm_main"
    r1 = await run_turn(TurnRequest(session_id=sess, text=TURN1,
                                    via_voice=True, asr_confidence=0.96))
    ok &= check("turn 1 reaches CONFIRMATION_READY",
                r1.session.get("state") == "CONFIRMATION_READY", str(r1.session.get("state")))
    d1 = drafts(sess)
    ok &= check("turn 1 queued 2 drafts", len(d1) == 2, str(d1))
    ok &= check("clause 1 = Joint S$1,000 from Savings",
                len(d1) >= 1 and d1[0] == ("DBS Joint Account", 100000, "acct_savings_001",
                                            "ending 1234", "OWN_ACCOUNT_TRANSFER"),
                str(d1[0] if d1 else None))
    ok &= check("clause 2 = Mei Tan S$50 (source defaulted)",
                len(d1) >= 2 and d1[1][0] == "Mei Tan" and d1[1][1] == 5000
                and d1[1][4] == "SAVED_PAYEE_TRANSFER",
                str(d1[1] if len(d1) >= 2 else None))

    r2 = await run_turn(TurnRequest(session_id=sess, text=TURN2,
                                    via_voice=True, asr_confidence=0.96))
    ok &= check("turn 2 still CONFIRMATION_READY",
                r2.session.get("state") == "CONFIRMATION_READY", str(r2.session.get("state")))
    d2 = drafts(sess)
    ok &= check("queue still has 2 drafts (no duplicate)", len(d2) == 2, str(d2))
    if len(d2) >= 2:
        ok &= check("clause 1 unchanged (Joint S$1,000 from Savings)",
                    d2[0] == d1[0], f"was={d1[0]} now={d2[0]}")
        c2 = d2[1]
        ok &= check("clause 2 amount stays S$50 (not S$1,000)",
                    c2[1] == 5000, f"amount_minor={c2[1]}")
        ok &= check("clause 2 source is Current Account (ending 7741)",
                    c2[2] == "acct_current_003" and c2[3] == "ending 7741",
                    f"source={c2[2]} masked={c2[3]}")
        ok &= check("clause 2 source is NOT Savings",
                    c2[2] != "acct_savings_001", str(c2[2]))
        ok &= check("clause 2 payee is still Mei Tan",
                    c2[0] == "Mei Tan", str(c2[0]))

    # ---- instruction_text + split_instructions (the parser fix) ----
    print("--- instruction_text strips the leading '1000是对的' ack before parsing ---")
    remainder = instruction_text(TURN2)
    ok &= check("the leading ack '1000是对的' is dropped",
                "1000是对的" not in remainder, remainder)
    ok &= check("the instruction tail survives (经常账户转给阿美)",
                "经常账户转给阿美" in remainder, remainder)
    parts = split_instructions(remainder, CUST)
    ok &= check("the clean remainder is one instruction with no leaked '1000'",
                len(parts) == 1 and "1000" not in parts[0], str(parts))

    # ---- Regression pins ----
    print("--- regressions: genuine single instructions and full two-instruction turns ---")
    single = split_instructions("从我的储蓄账户转100新元给阿美", CUST)
    ok &= check("a genuine single instruction is unchanged",
                len(single) == 1 and single[0] == "从我的储蓄账户转100新元给阿美",
                str(single))
    two = split_instructions("从我的储蓄账户转1000新元到我的联名账户，然后转50新元给阿美", CUST)
    ok &= check("a genuine two-instruction breath still splits into 2",
                len(two) == 2, str(two))
    none_target = split_instructions("hello", CUST)
    ok &= check("a no-target turn returns [text], not []",
                none_target == ["hello"], str(none_target))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
