"""Fix #5: a BM-dominant turn that uses the English loanword "dollar" must answer in BM,
not English.

Root cause: detect_language() saw "dollar" (EN_MARKERS) beside a BM marker like "kepada"
and returned MIXED; interface_language() then sent the turn to dominant_language(), which
scored BM and English evenly and let EN win the tie -> an English answer.

Fix: "dollar" is now a BM marker and no longer an EN marker, so the turn is detected as BM
and answered in BM. Pure-English turns are unaffected (transfer/send/pay/account/confirm still
set en_markers).

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/bm_audit.db AUNTIEJOY_REPO_DB=/tmp/bm.db \
      AUNTIEJOY_NICKNAMES=/tmp/bm.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/bm_interface_probe.py
(Use a FRESH repo db: the seeded nickname "ah girl" -> Mei Tan only exists after a reseed.)
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.parsing import detect_language  # noqa: E402
from app.messages import interface_language  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"

# The reported turn: BM with the English currency loanword "dollar".
BM_TURN = "Pindahkan seribu dollar kepada ah girl."
# Regression guard: a genuine English turn that also says "dollars".
EN_TURN = "Send 50 dollars to Ah Girl."


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def spoken(resp):
    return [ev["payload"] for ev in resp.events if ev["event_type"] == "INTERFACE_LINE"]


async def main():
    ok = True

    print("--- #5: the BM turn is detected and answered in BM ---")
    ok &= check("detect_language(BM_TURN) == 'BM'",
                detect_language(BM_TURN) == "BM", detect_language(BM_TURN))
    ok &= check("interface_language(BM_TURN) == 'BM'",
                interface_language(BM_TURN) == "BM", interface_language(BM_TURN))

    r = await run_turn(TurnRequest(session_id="sess_bm_5", text=BM_TURN,
                                   via_voice=True, asr_confidence=0.96))
    body = " ".join(p["text"] for p in spoken(r))
    ok &= check("BM turn reaches CONFIRMATION_READY",
                r.session.get("state") == "CONFIRMATION_READY", str(r.session.get("state")))
    ok &= check("BM turn answers in BM ('Saya' present)",
                "Saya" in body, body[:120])
    ok &= check("BM turn does NOT answer in English ('I will' absent)",
                "I will" not in body, body[:120])
    ok &= check("BM turn still resolves the payee (Mei Tan)",
                "Mei Tan" in body, body[:120])
    ok &= check("BM turn still reads back S$1,000.00",
                "1,000.00" in body, body[:120])

    print("--- regression: a pure-English turn still answers in English ---")
    ok &= check("detect_language(EN_TURN) == 'EN'",
                detect_language(EN_TURN) == "EN", detect_language(EN_TURN))
    ok &= check("interface_language(EN_TURN) == 'EN'",
                interface_language(EN_TURN) == "EN", interface_language(EN_TURN))
    re_ = await run_turn(TurnRequest(session_id="sess_bm_en", text=EN_TURN,
                                     via_voice=False, asr_confidence=0.96))
    en_body = " ".join(p["text"] for p in spoken(re_))
    ok &= check("EN turn answers in English ('want to send' present)",
                "want to send" in en_body, en_body[:120])
    ok &= check("EN turn still resolves the payee (Mei Tan)",
                "Mei Tan" in en_body, en_body[:120])

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
