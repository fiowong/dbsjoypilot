"""The live Payment Agent's AMOUNT over-trigger, and the safeguard that undoes it.

The bug (found while investigating a reported "BM FaceID doesn't work"): the LIVE Payment Agent
sometimes fails to copy the amount out of a turn that states it plainly and reports
amount_minor=None. "Hantar 50 dolar kepada Mei." and "Hantar lima puluh dolar kepada Mei." both
came back with no amount, although parse_amount reads S$50 from either, so Joy answered "how
much?" and the turn died at stage 5 with no draft - in Voice Mode, nothing for FaceID to release.

The safeguard (orchestrator.py, the AMOUNT_RECOVERED block, right after the PAYMENT_PROPOSAL
emit): if the model proposed no amount but the turn names a payment destination and the ordinary
parser reads an amount out of it, deterministic code owns the field.

This probe drives that path by standing in for the live agent - there is no API key here, so
`PaymentAgent.run` is replaced with one that returns the exact over-trigger the model produced.
Everything downstream is the real orchestrator, resolver and catalogue.

It also pins down what the safeguard must NOT do, which is the part that matters:
  * never adopt a number from a bare acknowledgement ("yes 1000 is correct");
  * never answer an ambiguous amount ("one fifty") with a third figure;
  * never answer a scale-ambiguous amount at low ASR confidence (fifty vs fifty THOUSAND).

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/la_audit.db AUNTIEJOY_REPO_DB=/tmp/la.db \
      AUNTIEJOY_NICKNAMES=/tmp/la.json PYTHONPATH="$PWD" .venv/bin/python -B \
      tools/live_amount_probe.py
(Use FRESH /tmp db paths: the seeded nickname for Mei Tan only exists after a reseed.)
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.agents import PaymentAgent                       # noqa: E402
from app.models import PaymentProposal                    # noqa: E402
from app.orchestrator import TurnRequest, run_turn        # noqa: E402

REAL_RUN = PaymentAgent.run

BM_DIGIT = "Hantar 50 dolar kepada Mei."
BM_WORDS = "Hantar lima puluh dolar kepada Mei."
NO_AMOUNT = "Hantar duit kepada Mei."
AMBIGUOUS = "Send one fifty to Mei."
BARE_ACK = "yes 1000 is correct"

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    ok = ok and bool(cond)


def over_trigger(amount_minor, field="RECIPIENT", recipient_phrase=None):
    """A PaymentAgent.run that reproduces the live model's over-trigger exactly."""
    async def run(turn, ctx):
        return (PaymentProposal(
            status="NEEDS_CLARIFICATION", detected_language="BM",
            recipient_phrase=recipient_phrase, amount_minor=amount_minor, currency="SGD",
            clarification_field=field, clarification_question="How much?",
            risk_flags=[]), "live-simulated")
    return run


def keys(resp):
    return [ev["payload"]["message_key"]
            for ev in resp.events if ev["event_type"] == "INTERFACE_LINE"]


def has(resp, event_type):
    return any(ev["event_type"] == event_type for ev in resp.events)


def proposal_amount(resp):
    for ev in resp.events:
        if ev["event_type"] == "PAYMENT_PROPOSAL":
            return ev["payload"].get("amount_minor")
    return "NO_PROPOSAL"


async def main():
    # ---- The reported bug: a BM turn whose amount the live model dropped ----
    print("--- the live agent drops the amount: deterministic code supplies it ---")
    for label, text in (("digits '50 dolar'", BM_DIGIT), ("words 'lima puluh'", BM_WORDS)):
        PaymentAgent.run = over_trigger(amount_minor=None)
        r = await run_turn(TurnRequest(session_id=f"la_{label}", text=text,
                                       via_voice=False, asr_confidence=0.99,
                                       asr_confidence_source="typed"))
        d = r.session.get("draft") or {}
        check(f"{label} recovers S$50", d.get("amount_minor") == 5000, str(d.get("amount_minor")))
        check(f"{label} still binds Mei Tan", d.get("payee_display") == "Mei Tan",
              str(d.get("payee_display")))
        check(f"{label} reaches CONFIRMATION_READY",
              r.session.get("state") == "CONFIRMATION_READY", str(r.session.get("state")))
        check(f"{label} emits AMOUNT_RECOVERED", has(r, "AMOUNT_RECOVERED"))
        # The model's own proposal must still be in the audit, unchanged, with the override as a
        # SEPARATE event - so the over-trigger is visible rather than silently rewritten.
        check(f"{label} still records the model's own amountless proposal",
              proposal_amount(r) is None, str(proposal_amount(r)))

    # ---- Must NOT: invent an amount when the customer never stated one ----
    print("--- a turn with no amount at all is still asked, not answered ---")
    PaymentAgent.run = over_trigger(amount_minor=None)
    r = await run_turn(TurnRequest(session_id="la_none", text=NO_AMOUNT,
                                   via_voice=False, asr_confidence=0.99,
                                   asr_confidence_source="typed"))
    check("no amount recovered", not has(r, "AMOUNT_RECOVERED"))
    check("AMOUNT_MISSING is still asked", "AMOUNT_MISSING" in keys(r), str(keys(r)))
    check("no draft was built", not r.session.get("draft"))

    # ---- Must NOT: answer an ambiguous amount with a third figure ----
    # "one fifty" is S$1.50 or S$150; parse_amount reads S$50 out of it, which is neither.
    print("--- an ambiguous amount is still put to the customer ---")
    PaymentAgent.run = over_trigger(amount_minor=None)
    r = await run_turn(TurnRequest(session_id="la_amb", text=AMBIGUOUS,
                                   via_voice=False, asr_confidence=0.99,
                                   asr_confidence_source="typed"))
    check("no amount recovered from an ambiguous phrase", not has(r, "AMOUNT_RECOVERED"))
    check("AMOUNT_AMBIGUOUS is asked", "AMOUNT_AMBIGUOUS" in keys(r), str(keys(r)))
    check("no draft was built", not r.session.get("draft"))

    # ---- Must NOT: answer a scale-ambiguous amount at low ASR confidence ----
    # "lima puluh" is fifty or fifty THOUSAND (S$50 / S$50,000). At the 0.41 confidence Voice
    # Mode declares, that is material, so the customer must be asked, not guessed for.
    print("--- a scale-ambiguous amount at low ASR confidence is still asked ---")
    PaymentAgent.run = over_trigger(amount_minor=None)
    r = await run_turn(TurnRequest(session_id="la_scale", text=BM_WORDS,
                                   via_voice=True, asr_confidence=0.41,
                                   asr_confidence_source="engine_reported",
                                   voice_risk="SPEAKER_MISMATCH"))
    check("no amount recovered while the scale is ambiguous", not has(r, "AMOUNT_RECOVERED"))
    check("no draft was built", not r.session.get("draft"))

    # ---- Must NOT: adopt the number out of a bare acknowledgement ----
    print("--- a bare acknowledgement's number is never adopted as an amount ---")
    PaymentAgent.run = over_trigger(amount_minor=None)
    r = await run_turn(TurnRequest(session_id="la_ack", text=BARE_ACK,
                                   via_voice=False, asr_confidence=0.99,
                                   asr_confidence_source="typed"))
    check("no amount recovered from 'yes 1000 is correct'", not has(r, "AMOUNT_RECOVERED"))
    check("no draft was built", not r.session.get("draft"))

    # ---- The real agent is untouched: a proposal that HAS an amount needs no recovery ----
    print("--- the real mock agent still resolves normally (no override) ---")
    PaymentAgent.run = REAL_RUN
    r = await run_turn(TurnRequest(session_id="la_real", text=BM_DIGIT,
                                   via_voice=False, asr_confidence=0.99,
                                   asr_confidence_source="typed"))
    check("no override when the agent supplied the amount", not has(r, "AMOUNT_RECOVERED"))
    d = r.session.get("draft") or {}
    check("still S$50 to Mei Tan", d.get("amount_minor") == 5000
          and d.get("payee_display") == "Mei Tan", str(d))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
