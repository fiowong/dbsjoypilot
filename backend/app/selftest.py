import argparse
import sys
from datetime import date
from .config import Settings
from .parsing import parse_schedule
from .session import STAGES
from .scenarios import SCENARIOS, run_scenario
from .main import app
from .agents import PersonalizationAgent
from .models import PersonalizationSignals

# The schedule extractor, unit-tested against a frozen clock the same way the Personalization
# Agent is. A date is the one field a demo cannot fake, so "which day did she mean" is checked
# here rather than left to whichever scenario happens to mention a weekday.
# (text, expected relative phrase) with TODAY frozen to Tuesday 29 September 2026.
SCHEDULE_TODAY = date(2026, 9, 29)
SCHEDULE_CASES = [
    # The parked case: two weekdays, the first is the one being REPLACED.
    ("instead of monday make it tuesday", "tuesday"),
    ("make it tuesday instead of monday", "tuesday"),
    ("rather than wednesday make it thursday", "thursday"),
    ("next monday instead of friday", "next monday"),
    # A single weekday is untouched, the extractor must not start second-guessing it.
    ("send it on monday", "monday"),
    ("on monday instead", "monday"),
    # Two weekdays with no cue at all: unchanged, the first one named.
    ("pay on monday and tuesday", "monday"),
    ("tomorrow", "tomorrow"),
]


def _test_schedule() -> int:
    failures = 0
    for text, expected in SCHEDULE_CASES:
        result = parse_schedule(text, today=SCHEDULE_TODAY)
        actual = result["relative"] if result else None
        ok = actual == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} schedule  {text!r} -> {actual!r}")
        if not ok:
            print(f"        -> expected {expected!r}")
    return failures

# 14a. Personalization Agent, unit-tested directly against its own documented weight
# table, the same way you'd test any pure scoring function (§ observed-friction policy).
# =====================================================================
PERSONALIZATION_CASES = [
    # (signals, expected personalize, expected reason_code), mirrors Settings.PERSONALIZATION_WEIGHTS
    (PersonalizationSignals(), False, "NONE"),
    (PersonalizationSignals(repeat_field_rejections=["AMOUNT"]), True, "REPEAT_REJECTION"),
    (PersonalizationSignals(repeat_clarifications=["PAYEE_UNCLEAR"]), True, "REPEAT_CLARIFICATION"),
    (PersonalizationSignals(stalled_intent=True), False, "STALLED_INTENT"),   # weight 1 alone, below threshold 2
    (PersonalizationSignals(repeat_field_rejections=["AMOUNT"], stalled_intent=True), True, "COMBINED"),
    (PersonalizationSignals(repeat_clarifications=["PAYEE_UNCLEAR"], repeat_field_rejections=["RECIPIENT"]), True, "COMBINED"),
]


def _test_personalization() -> int:
    failures = 0
    for signals, expected_personalize, expected_reason in PERSONALIZATION_CASES:
        decision = PersonalizationAgent.mock(signals)
        ok = decision.personalize == expected_personalize and decision.reason_code == expected_reason
        status = "PASS" if ok else "FAIL"
        failures += not ok
        print(f"{status} personalization  signals={signals.model_dump()} "
              f"-> personalize={decision.personalize} score={decision.score} reason={decision.reason_code}")
        if not ok:
            print(f"        -> expected personalize={expected_personalize}, reason={expected_reason}")
    return failures

# 14. Self-test, every scenario, asserting the BRD's own acceptance rules (§A.12)
# =====================================================================
EXPECTED = {
    "HAPPY": {"state": "EXECUTED", "outcome": "ACCEPTED", "ledger": "RECORDED"},
    "A1":    {"state": "FROZEN", "outcome": "REJECTED", "ledger": "NOT_INVOKED"},
    "A2":    {"state": "FROZEN", "outcome": "REJECTED", "ledger": "NOT_INVOKED"},
    "A3":    {"state": "CLARIFICATION_REQUIRED", "outcome": "CLARIFY", "ledger": "NOT_INVOKED"},
    "B1":    {"state": "CONFIRMATION_READY", "outcome": "ALLOW", "ledger": "NOT_INVOKED"},
    "B2":    {"state": "CONFIRMATION_READY", "outcome": "ALLOW", "ledger": "NOT_INVOKED"},
    "B3":    {"state": "FROZEN", "outcome": "FREEZE", "ledger": "NOT_INVOKED"},
    # B4: haste is not a scam. "sekarang" ("now") used to trip the family-emergency rule, so this
    # ordinary Malay transfer was answered with the scam pause. It must reach confirmation.
    "B4":    {"state": "CONFIRMATION_READY", "outcome": "ALLOW", "ledger": "NOT_INVOKED"},
    "C1":    {"state": "FROZEN", "outcome": "BLOCK", "ledger": "NOT_INVOKED"},
    "C2":    {"state": "FROZEN", "outcome": "FREEZE", "ledger": "NOT_INVOKED"},
    "C3":    {"state": "FROZEN", "outcome": "BLOCK", "ledger": "NOT_INVOKED"},
    # C4: a system-message injection that changes the assistant's role rather than asking to skip
    # confirmation. Same consequence as C1 (frozen, blocked, ledger never touched), and the point
    # of the case is that a turn carrying a recipient and an amount still produces NO draft.
    "C4":    {"state": "FROZEN", "outcome": "BLOCK", "ledger": "NOT_INVOKED"},
    "D1":    {"state": "FROZEN", "outcome": "SAFETY_INTERVENTION", "ledger": "NOT_INVOKED"},
    "D2":    {"state": "FROZEN", "outcome": "BLOCK", "ledger": "NOT_INVOKED"},
    "D3":    {"state": "COOLING_OFF", "outcome": "COOLING_OFF", "ledger": "NOT_INVOKED"},
    "E1":    {"state": "CONFIRMATION_COMPLETE", "outcome": "AWAITING_BIOMETRIC", "ledger": "NOT_INVOKED"},
    "E2":    {"state": "EXECUTED", "outcome": "ACCEPTED", "ledger": "RECORDED"},
    "E3":    {"state": "CONFIRMATION_READY", "outcome": "PAYMENT_CLARIFICATION", "ledger": "NOT_INVOKED"},
    "F1":    {"state": "REJECTED", "outcome": "REJECTED", "ledger": "NOT_INVOKED"},
    "F2":    {"state": "REJECTED", "outcome": "REJECTED", "ledger": "NOT_INVOKED"},
    "F3":    {"state": "REJECTED", "outcome": "REJECTED", "ledger": "RECORDED"},
    "G1":    {"state": "CLARIFICATION_REQUIRED", "outcome": "CLARIFY", "ledger": "NOT_INVOKED"},
    # H1 (branch H, added after the original 20): the affordability safeguard fires
    # deterministically at stage 3, empathetic abort, the conversation stays open so the
    # customer can name a smaller amount or drop it. No draft is created from an affordability
    # statement alone, so the ledger is never invoked. See orchestrator.py's
    # INSUFFICIENT_FUNDS_RE branch (sets state=CLARIFICATION_REQUIRED, outcome=INSUFFICIENT_FUNDS).
    "H1":    {"state": "CLARIFICATION_REQUIRED", "outcome": "INSUFFICIENT_FUNDS", "ledger": "NOT_INVOKED"},
}


async def _ledger_rows_in(scenario_ids) -> int:
    total = 0
    for scenario_id in scenario_ids:
        total += len((await run_scenario(scenario_id)).ledger_rows)
    return total


async def selftest() -> int:
    import asyncio
    print(f"AuntieJoy self-test, engine={'live' if Settings.LIVE_LLM else 'mock'} "
          f"({Settings.LLM_MODEL if Settings.LIVE_LLM else 'deterministic'})")
    print(f"prompt={Settings.PROMPT_VERSION}  schema={Settings.SCHEMA_VERSION}  "
          f"stages={len(STAGES)}  scenarios={len(SCENARIOS)}\n")
    failures = 0
    for scenario_id in SCENARIOS:
        result = await run_scenario(scenario_id)
        expected = EXPECTED[scenario_id]
        problems = []
        for key in ("state", "outcome", "ledger"):
            actual = result.session[key] if key != "ledger" else result.session["ledger"]
            if actual != expected[key]:
                problems.append(f"{key}: expected {expected[key]}, got {actual}")
        if not result.audit["intact"]:
            problems.append(f"audit chain broken at {result.audit.get('broken_at_seq')}")
        if expected["ledger"] == "NOT_INVOKED" and result.ledger_rows:
            problems.append(f"ledger was invoked {len(result.ledger_rows)} time(s)")
        if expected["ledger"] == "RECORDED" and len(result.ledger_rows) != 1:
            problems.append(f"expected exactly 1 ledger row, got {len(result.ledger_rows)}")

        status = "FAIL" if problems else "PASS"
        failures += bool(problems)
        print(f"{status} {scenario_id:<6} {result.branch:<2} "
              f"state={result.session['state']:<24} outcome={result.session['outcome']:<20} "
              f"ledger={result.session['ledger']:<12} "
              f"events={sum(len(t['events']) for t in result.turns):<3} "
              f"rows={len(result.ledger_rows)}")
        for problem in problems:
            print(f"        -> {problem}")

    print()
    personalization_failures = _test_personalization()
    failures += personalization_failures
    print()
    schedule_failures = _test_schedule()
    failures += schedule_failures

    scenario_failures = failures - personalization_failures - schedule_failures
    print(f"\n{len(SCENARIOS) - scenario_failures}/{len(SCENARIOS)} scenarios passed.")
    print(f"{len(PERSONALIZATION_CASES) - personalization_failures}/{len(PERSONALIZATION_CASES)} "
          "personalization cases passed.")
    print(f"{len(SCHEDULE_CASES) - schedule_failures}/{len(SCHEDULE_CASES)} schedule cases passed.")
    print("Ledger invocations across all scenarios: "
          f"{sum(1 for s in ('HAPPY', 'E2', 'F3'))} expected, "
          f"{await _ledger_rows_in(('HAPPY', 'E2', 'F3'))} observed "
          "in the three legitimate execution paths.")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args()
    if args.selftest:
        import asyncio
        sys.exit(asyncio.run(selftest()))
    import uvicorn
    print(f"AuntieJoy backend → http://{args.host}:{args.port}   "
          f"engine={'live' if Settings.LIVE_LLM else 'mock'}")
    print(f"  /health   /stages   /scenarios   /turn   /confirm   /scenario/{{id}}   /audit/{{sid}}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
