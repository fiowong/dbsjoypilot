"""Verify the SCAM_FALSE_POSITIVE deterministic safeguard (orchestrator.py).

The live Guardrails LLM can false-positive a benign customer continuation as SCAM and freeze the
session (Issue 2). The relaxer downgrades SAFETY_INTERVENTION/SCAM to ALLOW ONLY when the turn
names NEITHER scam cue (no emergency noun, no authority/safe-account coercion). A genuine scam
still freezes.

We simulate the live over-trigger by monkeypatching GuardrailsAgent.run to return a SCAM verdict
for a specific benign turn, then confirm the relaxer overrides it, while real-scam turns keep the
freeze.

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/sr_audit.db AUNTIEJOY_REPO_DB=/tmp/sr.db \
      AUNTIEJOY_NICKNAMES=/tmp/sr.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/scam_relax_probe.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.models import GuardrailDecision, RiskCategory, GuardrailResult  # noqa: E402
from app.agents import GuardrailsAgent  # noqa: E402
from app.orchestrator import TurnRequest, run_turn  # noqa: E402

CUST = "cust_auntie_001"
BENIGN = "1000 dollar betul. hantarlah duitnya ke ah girl dari akun semasa."   # the reported turn
REAL_AUTHORITY = "the bank officer says move it to a safe account now"          # genuine scam
REAL_EMERGENCY = "my son had an accident, please send money right now"          # genuine scam


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


async def main():
    ok = True

    # --- Simulate the live LLM returning SCAM for the BENIGN turn, ALLOW otherwise ---
    orig_run = GuardrailsAgent.run

    async def fake_run(turn, ctx):
        if turn.strip() == BENIGN:
            return GuardrailResult(
                decision=GuardrailDecision.SAFETY_INTERVENTION,
                risk_category=RiskCategory.SCAM_OR_COERCION_SIGNAL,
                risk_level="HIGH", detected_language="BM", message_key="PAUSE_SCAM",
                developer_reason="SIMULATED live over-trigger"), "live"
        return GuardrailsAgent.mock(turn, ctx), "mock"

    GuardrailsAgent.run = staticmethod(fake_run)

    print("--- benign BM continuation: simulated live SCAM must be relaxed to ALLOW ---")
    r = await run_turn(TurnRequest(session_id="sess_sr_benign", text=BENIGN,
                                   via_voice=False, asr_confidence=0.96))
    ok &= check("benign turn is NOT frozen", not r.session.get("frozen"), str(r.session.get("state")))
    ok &= check("benign turn proceeds (CONFIRMATION_READY)",
                r.session.get("state") == "CONFIRMATION_READY", str(r.session.get("state")))

    # --- Genuine scams must STILL freeze (relaxer must not fire) ---
    print("--- real scam (authority coercion): must stay frozen ---")
    async def auth_run(t, c):
        return GuardrailResult(decision=GuardrailDecision.SAFETY_INTERVENTION,
                               risk_category=RiskCategory.SCAM_OR_COERCION_SIGNAL,
                               risk_level="CRITICAL", detected_language="EN", message_key="PAUSE_SCAM",
                               developer_reason="real scam"), "live"
    GuardrailsAgent.run = staticmethod(auth_run)
    ra = await run_turn(TurnRequest(session_id="sess_sr_auth", text=REAL_AUTHORITY,
                                    via_voice=False, asr_confidence=0.96))
    ok &= check("authority-coercion turn IS protected (not allowed)",
                ra.session.get("state") in ("FROZEN", "COOLING_OFF"),
                str(ra.session.get("state")))

    print("--- real scam (named emergency): must stay frozen ---")
    async def emer_run(t, c):
        return GuardrailResult(decision=GuardrailDecision.SAFETY_INTERVENTION,
                               risk_category=RiskCategory.SCAM_OR_COERCION_SIGNAL,
                               risk_level="HIGH", detected_language="EN", message_key="PAUSE_SCAM",
                               developer_reason="real scam"), "live"
    GuardrailsAgent.run = staticmethod(emer_run)
    re_ = await run_turn(TurnRequest(session_id="sess_sr_emer", text=REAL_EMERGENCY,
                                     via_voice=False, asr_confidence=0.96))
    ok &= check("named-emergency turn IS protected (not allowed)",
                re_.session.get("state") in ("FROZEN", "COOLING_OFF"),
                str(re_.session.get("state")))

    GuardrailsAgent.run = staticmethod(orig_run)

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
