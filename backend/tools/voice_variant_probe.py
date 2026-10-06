"""DEMO 1 voice-vs-typed: prove the three ASR spelling differences all resolve.

The same command typed works but spoken does not, because ASR returns slightly different
WORDS for the same intent. Three differences were in that transcript, and this probe drives
the real parsing layer with each of them:

  1. "star hub" vs "StarHub"  -> the biller parser must match both (space-insensitive)
  2. "50"       vs "fifty"    -> both must read as 5000 minor (S$50.00)
  3. "Singaporean" vs "Singapore" -> both must read as SGD

Plus the nickname, which is the one that actually broke the demo:
  - "Ah Girl" is now SEEDED, so it also lands in the ASR hotword_list (prevention).
  - If ASR STILL mishears it as "a girl", the fuzzy matcher must turn that into a
    confirmable "did you mean Mei Tan?" rather than a dead-end "who should I pay?" (recovery).

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/voice.db AUNTIEJOY_NICKNAMES=/tmp/voice_nick.json \
      PYTHONPATH="$PWD" .venv/bin/python -B tools/voice_variant_probe.py
(Blank the key so every agent uses its deterministic mock instead of retrying a dead LLM.)
"""
import sys

sys.path.insert(0, ".")

from app.parsing import (  # noqa: E402
    parse_biller_phrase, parse_currency, split_instructions, parse_payee_phrase, parse_amount,
)
from app.controls import closest_saved_payee  # noqa: E402
from app.repository import get_repository  # noqa: E402
from app import nicknames as nick  # noqa: E402
from app.stt import build_hotword_list  # noqa: E402

CUST = "cust_auntie_001"

TYPED = ("Please help me pay my StarHub bill from my current account. "
         "Then transfer fifty Singapore dollars to Ah Girl from my savings account. "
         "Explain each transaction one at a time, slowly.")
# What ASR should now return: "Ah Girl" is a hotword, so the engine hears the name, not "a girl".
VOICE_OK = ("Please help me pay my star hub bill from my current account. "
            "Then transfer 50 Singaporean dollars to Ah Girl from my savings account. "
            "Explain each transaction one at a time, slowly.")
# What ASR actually returned before the fix. Still possible on a bad mic, so it must degrade
# to a SUGGESTION rather than a dead end.
VOICE_BAD = ("Please help me pay my star hub bill from my current account. "
             "Then transfer 50 Singaporean dollars to a girl from my savings account. "
             "Explain each transaction one at a time, slowly.")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


ok = True

print("--- 1. biller: 'star hub' vs 'StarHub' ---")
ok &= check("typed 'StarHub' resolves", parse_biller_phrase(TYPED, CUST) == "StarHub")
ok &= check("spoken 'star hub' resolves", parse_biller_phrase(VOICE_OK, CUST) == "StarHub")
ok &= check("a DIFFERENT word still does NOT match",
            parse_biller_phrase("pay my starbucks bill", CUST) is None)

print("\n--- 2. amount: 'fifty' vs '50' ---")
t_amt = parse_amount("fifty Singapore dollars")[0]
v_amt = parse_amount("50 Singaporean dollars")[0]
ok &= check("'fifty' -> 5000 minor (S$50.00)", t_amt == 5000, str(t_amt))
ok &= check("'50' -> 5000 minor (S$50.00)", v_amt == 5000, str(v_amt))
ok &= check("both spellings agree", t_amt == v_amt)

print("\n--- 3. currency: 'Singaporean' vs 'Singapore' ---")
ok &= check("'50 Singaporean dollars' -> SGD", parse_currency("50 Singaporean dollars") == "SGD")
ok &= check("'fifty Singapore dollars' -> SGD", parse_currency("fifty Singapore dollars") == "SGD")

print("\n--- 4. nickname 'Ah Girl' is SEEDED (prevention via the hotword list) ---")
mei = nick.find(CUST, "Ah Girl")
ok &= check("'Ah Girl' resolves to Mei Tan", mei is not None and mei.display_name == "Mei Tan",
            mei.display_name if mei else "None")
ok &= check("case-insensitive ('ah girl')", nick.find(CUST, "ah girl") is not None)
hot = build_hotword_list([p.display_name for p in get_repository().get_saved_payees(CUST)]
                         + list(nick.index(CUST).keys()))
# The hotword list stores the nickname in its stored (lowercase) form, so compare folded.
ok &= check("'Ah Girl' is inside the ASR hotword_list",
            "ah girl" in (hot or "").lower(), str(hot))

print("\n--- 5. the full spoken turn, ASR heard the name correctly ---")
ok &= check("VOICE_OK splits into 2 instructions", len(split_instructions(VOICE_OK, CUST)) == 2,
            str(split_instructions(VOICE_OK, CUST)))
ok &= check("VOICE_OK payee -> Mei Tan", parse_payee_phrase(VOICE_OK, CUST) == "Mei Tan",
            str(parse_payee_phrase(VOICE_OK, CUST)))
ok &= check("typed payee -> Mei Tan", parse_payee_phrase(TYPED, CUST) == "Mei Tan")

print("\n--- 6. the full spoken turn, ASR STILL misheard it as 'a girl' (recovery) ---")
ok &= check("'a girl' is not an exact nickname (so prevention is the real fix)",
            nick.find(CUST, "a girl") is None)
near = closest_saved_payee(CUST, "a girl")
ok &= check("'a girl' fuzzy-suggests Mei Tan, not a dead end",
            near is not None and near.display_name == "Mei Tan",
            near.display_name if near else "None")

print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
