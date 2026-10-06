"""IN-10 clause splitting: a comma inside ONE instruction is not a boundary between two.

The bug: "from my savings account to my joint account, move a grand, then send one fifty to Ah
Girl" is two payments, but the first clause leads with the ACCOUNTS and puts the verb and the
amount after the comma. Cutting on that comma left:

    clause 1  "from my savings account to my joint account"   destination, NO amount
    clause 2  "send one fifty to Ah Girl."                    amount, different destination

neither resolved, so the breath collapsed to MULTI_INSTRUCTION_UNRESOLVED and Joy said
"There's no payment waiting for your confirmation right now" - even though nothing about the
REQUEST was unclear, only its word order.

The rule now: a fragment that names no target of its own is not an instruction, it is the TAIL of
the clause before it, and it is glued back on. Merging only, never splitting.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/mc_audit.db AUNTIEJOY_REPO_DB=/tmp/mc.db \
      AUNTIEJOY_NICKNAMES=/tmp/mc.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/multi_clause_probe.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.parsing import split_instructions  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"

# The reported command, accounts-first then the verb and the amount.
ACCOUNTS_FIRST = ("from my savings account to my joint account, move a grand, "
                  "then send one fifty to Ah Girl.")
# The same two payments in the original word order. Must be untouched.
VERB_FIRST = "Move a grand from savings to my joint account, then send fifty to Ah Girl."
DEMO1 = "Pay my StarHub bill and send S$50 to Ah Girl, explain satu-satu."
SINGLE = "Send fifty dollars to Mei."


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


async def turn(text, sid):
    return await run_turn(TurnRequest(session_id=sid, text=text, via_voice=True,
                                      asr_confidence=0.96))


def spoken(resp):
    return [(ev["payload"]["message_key"], ev["payload"]["text"])
            for ev in resp.events if ev["event_type"] == "INTERFACE_LINE"]


def unresolved(resp):
    return [ev["payload"] for ev in resp.events
            if ev["event_type"] == "MULTI_INSTRUCTION_UNRESOLVED"]


async def main():
    ok = True

    print("--- 1. the reported word order now splits into two COMPLETE clauses ---")
    parts = split_instructions(ACCOUNTS_FIRST, CUST)
    ok &= check("exactly 2 clauses", len(parts) == 2, str(parts))
    ok &= check("clause 1 keeps its verb and amount ('move a grand')",
                len(parts) == 2 and "grand" in parts[0].lower(), parts[0] if parts else "")
    ok &= check("clause 2 is the payment to Ah Girl",
                len(parts) == 2 and "ah girl" in parts[1].lower(), parts[1] if len(parts) > 1 else "")

    print("\n--- 2. ...and the turn resolves both payments ---")
    r = await turn(ACCOUNTS_FIRST, "sess_mc_1")
    ok &= check("no MULTI_INSTRUCTION_UNRESOLVED", unresolved(r) == [], str(unresolved(r)))
    ok &= check("state is CONFIRMATION_READY", r.session.get("state") == "CONFIRMATION_READY",
                str(r.session.get("state")))
    intro = [t for k, t in spoken(r) if k == "MULTI_REVIEW_INTRO"]
    ok &= check("Joy reads back TWO instructions", intro and "two instructions" in intro[0].lower(),
                intro[0][:90] if intro else "none")
    ok &= check("the S$1,000.00 own-account move is in the read-back",
                any("1,000.00" in t for _, t in spoken(r)))

    print("\n--- 3. the original word order is unchanged ---")
    old = split_instructions(VERB_FIRST, CUST)
    ok &= check("still 2 clauses", len(old) == 2, str(old))
    ok &= check("clause 1 is the own-account move",
                old and "move a grand" in old[0].lower(), old[0] if old else "")
    r2 = await turn(VERB_FIRST, "sess_mc_2")
    ok &= check("still resolves", r2.session.get("state") == "CONFIRMATION_READY"
                and unresolved(r2) == [])

    print("\n--- 4. DEMO 1 is unchanged (a trailing fragment does not become a 3rd clause) ---")
    d1 = split_instructions(DEMO1, CUST)
    ok &= check("exactly 2 clauses, not 3", len(d1) == 2, str(d1))
    r3 = await turn(DEMO1, "sess_mc_3")
    ok &= check("still resolves", r3.session.get("state") == "CONFIRMATION_READY"
                and unresolved(r3) == [])
    ok &= check("the bill is still paid", any("StarHub" in t for _, t in spoken(r3)))

    print("\n--- 5. a single instruction is untouched ---")
    ok &= check("one clause, the whole turn",
                split_instructions(SINGLE, CUST) == [SINGLE], str(split_instructions(SINGLE, CUST)))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
