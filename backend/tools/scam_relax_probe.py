"""Verify the SCAM_FALSE_POSITIVE deterministic safeguard (orchestrator.py).

The live Guardrails LLM can false-positive a benign customer continuation as SCAM and freeze the
session (Issue 2). The relaxer downgrades a SCAM verdict to ALLOW ONLY when the turn names NEITHER
scam cue (no emergency noun, no authority/safe-account coercion). A genuine scam still freezes.

The verdict arrives labelled EITHER BLOCK or SAFETY_INTERVENTION (the reported turn came back
BLOCK, and keying the safeguard on SAFETY_INTERVENTION alone meant it never fired), so both labels
are covered below - and a BLOCK for a NON-scam category must still stand, or the fix would have
widened the wrong thing.

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

    orig_run = GuardrailsAgent.run

    # The live model uses BOTH labels for this category. The reported turn actually came back
    # decision=BLOCK (not SAFETY_INTERVENTION), which the first version of the safeguard did not
    # accept, so it never fired and the payment stayed frozen. Both labels must be relaxed.
    print("--- benign BM continuation: simulated live SCAM must be relaxed to ALLOW ---")
    for label in (GuardrailDecision.BLOCK, GuardrailDecision.SAFETY_INTERVENTION):
        async def fake_run(turn, ctx, _label=label):
            if turn.strip() == BENIGN:
                return GuardrailResult(
                    decision=_label,
                    risk_category=RiskCategory.SCAM_OR_COERCION_SIGNAL,
                    risk_level="HIGH", detected_language="BM", message_key="PAUSE_SCAM",
                    developer_reason="SIMULATED live over-trigger"), "live"
            return GuardrailsAgent.mock(turn, ctx), "mock"

        GuardrailsAgent.run = staticmethod(fake_run)
        r = await run_turn(TurnRequest(session_id=f"sess_sr_benign_{label.value}", text=BENIGN,
                                       via_voice=False, asr_confidence=0.96))
        ok &= check(f"benign turn with decision={label.value} is NOT frozen",
                    not r.session.get("frozen"), str(r.session.get("state")))
        ok &= check(f"benign turn with decision={label.value} proceeds (CONFIRMATION_READY)",
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

    # --- A BLOCK for a reason OTHER than scam must NOT be relaxed (only the SCAM category is
    #     touched; widening the decision check must not turn into widening the category check). ---
    print("--- a BLOCK for a non-scam reason is left exactly as the model gave it ---")
    async def other_run(t, c):
        return GuardrailResult(decision=GuardrailDecision.BLOCK,
                               risk_category=RiskCategory.UNSUPPORTED_ACTION,
                               risk_level="HIGH", detected_language="EN", message_key="BLOCKED",
                               developer_reason="not a scam category"), "live"
    GuardrailsAgent.run = staticmethod(other_run)
    ro = await run_turn(TurnRequest(session_id="sess_sr_other", text="send fifty to ah girl",
                                    via_voice=False, asr_confidence=0.96))
    ok &= check("a non-SCAM BLOCK is NOT relaxed",
                ro.session.get("state") in ("FROZEN", "COOLING_OFF", "BLOCKED"),
                str(ro.session.get("state")))

    GuardrailsAgent.run = staticmethod(orig_run)

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


asyncio.run(main())
