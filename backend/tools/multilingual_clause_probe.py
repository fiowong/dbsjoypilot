"""The same two payments spoken in EN, ZH and BM must behave identically.

Before this fix only the English command worked. Two gaps, both in the same family:

  1. split_instructions knew only English connectors, so 然后 / kemudian never split a breath
     into its two payments - the whole turn stayed one clause and the second payment vanished.
  2. The parsers were English-only: 联名账户 / akun bersama matched no own account, 存储账户 /
     akun simpanan matched no source, "seribu" (a thousand) parsed as no amount at all, and
     the Mandarin nickname 阿美 could not be found in a sentence.

Plus the boundary trap underneath all of them: \\b is only a boundary next to a NON-word
character, and CJK characters ARE word characters in Unicode mode, so "\\b存储账户\\b" could
never match inside "从我的存储账户" and "\\b阿美\\b" could never match inside "给阿美". Both
matched only when a space happened to sit next to them. See _phrase_pattern and
nicknames._nickname_pattern.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/ml_audit.db AUNTIEJOY_REPO_DB=/tmp/ml.db \
      AUNTIEJOY_NICKNAMES=/tmp/ml.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/multilingual_clause_probe.py
(Use a FRESH repo db: the seeded nickname 阿美 only exists after a reseed.)
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.parsing import (  # noqa: E402
    split_instructions, parse_own_account_phrase, parse_source_account_phrase,
    parse_amount, parse_payee_phrase,
)
from app.nicknames import match_in_text  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"

EN = ("from my savings account to my joint account, move a grand, "
      "then send one fifty to Ah Girl.")
ZH = "从我的存储账户转1000新元到我的联名账户，然后转50新元给阿美。"
BM = ("Pindahkan seribu dollar dari akun simpanan saya ke akun bersama saya, "
      "kemudian hantarlah lima puluh dolar kepada ah girl.")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def spoken(resp):
    return [ev["payload"] for ev in resp.events if ev["event_type"] == "INTERFACE_LINE"]


def unresolved(resp):
    return [ev for ev in resp.events if ev["event_type"] == "MULTI_INSTRUCTION_UNRESOLVED"]


async def main():
    ok = True

    for label, turn_text in (("EN", EN), ("ZH", ZH), ("BM", BM)):
        print(f"--- {label}: the same breath splits into two complete payments ---")
        parts = split_instructions(turn_text, CUST)
        ok &= check(f"{label}: exactly 2 clauses", len(parts) == 2, str(parts))
        if len(parts) != 2:
            continue
        first, second = parts
        ok &= check(f"{label}: clause 1 is the own-account move",
                    parse_own_account_phrase(first, CUST) == "DBS Joint Account",
                    repr(parse_own_account_phrase(first, CUST)))
        ok &= check(f"{label}: clause 1 carries S$1,000.00",
                    parse_amount(first)[0] == 100000, str(parse_amount(first)))
        ok &= check(f"{label}: clause 2 is the payment to Mei Tan",
                    parse_payee_phrase(second, CUST) == "Mei Tan",
                    repr(parse_payee_phrase(second, CUST)))
        ok &= check(f"{label}: clause 2 carries S$50.00",
                    parse_amount(second)[0] == 5000, str(parse_amount(second)))

        print(f"--- {label}: ...and the turn resolves both ---")
        r = await run_turn(TurnRequest(session_id=f"sess_ml_{label}", text=turn_text,
                                       via_voice=True, asr_confidence=0.96))
        ok &= check(f"{label}: no MULTI_INSTRUCTION_UNRESOLVED", unresolved(r) == [])
        ok &= check(f"{label}: state is CONFIRMATION_READY",
                    r.session.get("state") == "CONFIRMATION_READY", str(r.session.get("state")))
        body = " ".join(p["text"] for p in spoken(r))
        ok &= check(f"{label}: the S$1,000.00 move is read back", "1,000.00" in body, body[:90])
        ok &= check(f"{label}: Mei Tan is read back", "Mei Tan" in body, body[:90])

    print("\n--- the \\b anchor must still hold where it means something ---")
    ok &= check("'mum' does NOT match inside 'mumble'",
                match_in_text(CUST, "mumble") is None, str(match_in_text(CUST, "mumble")))
    ok &= check("'mum' still matches as a word", match_in_text(CUST, "send mum $50") is not None)
    ok &= check("'savings' does NOT match inside 'savingses'",
                parse_source_account_phrase("savingses") is None)

    print("\n--- regressions on the amount and source parsers ---")
    ok &= check("BM compound 'dua ribu' is still S$2,000.00",
                parse_amount("dua ribu")[0] == 200000, str(parse_amount("dua ribu")))
    ok &= check("EN 'from my savings account' still resolves the source",
                parse_source_account_phrase("from my savings account") == "savings")
    ok &= check("EN 'a grand' is still S$1,000.00",
                parse_amount("move a grand")[0] == 100000, str(parse_amount("move a grand")))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
