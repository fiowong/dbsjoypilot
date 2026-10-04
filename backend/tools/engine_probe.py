"""Print the `engine` label of every agent card, per turn.

WHY THIS EXISTS
---------------
`app.selftest` prints `engine=live` when a key is present, and asserts only
state/outcome/ledger. All three are reachable with the deterministic mocks, so a
green 29/29 does NOT prove the model was ever called. This script asks the only
question that matters: for each turn, which agents actually went live?

The scenario text is read from `app.scenarios.SCENARIOS`, so it can never drift
from the regression suite.

USAGE (from backend/)
    AUNTIEJOY_DB=/tmp/probe_audit.db PYTHONPATH="$PWD" .venv/bin/python -B tools/engine_probe.py
    ... tools/engine_probe.py DEMO1 DEMO3          # only these
    ... tools/engine_probe.py --all                # every scenario, not just the demos

Override the model without editing .env:
    AUNTIEJOY_LLM_MODEL=deepseek-v4-flash-202605 PYTHONPATH="$PWD" .venv/bin/python -B tools/engine_probe.py

READING THE OUTPUT
    GUARDRAILS=live  INTENT=mock  ...
    A `mock` after a `live` on a long turn usually means the 15s turn budget ran
    out, not that the model is broken. ALL-mock with ~16s turns means the FIRST
    call never returned: check the model is not a reasoning-only endpoint that
    puts everything in `reasoning_content` and leaves `content` null.

    `deterministic by design` marks the IN-10 multi-instruction path: `_instruction_proposal`
    is parsing.py, and `run_multi_turn` validates with ValidationAgent.mock, emitting
    engine="deterministic (reference policy)". That one is NOT a fallback, nothing was tried
    and nothing failed. Only `!! ... genuinely fell back to mock` is worth chasing.
"""
import asyncio
import sys
import time

from app.config import Settings
from app.orchestrator import TurnRequest, run_turn
from app.scenarios import SCENARIOS

AGENT_EVENTS = {"GUARDRAILS_RESULT", "INTENT_RESULT", "PAYMENT_PROPOSAL",
                "VALIDATION_RESULT", "PERSONALIZATION_RESULT"}


def as_dict(event):
    return event if isinstance(event, dict) else event.model_dump()


async def run_one(label: str, spec: dict):
    """Replay one scenario's first turn in a fresh session, printing each agent's engine."""
    turn = spec["turns"][0]
    started = time.monotonic()
    resp = await run_turn(TurnRequest(
        session_id=f"probe_{label.lower()}", text=turn["text"], via_voice=True,
        asr_confidence=turn.get("asr_confidence", 0.96),
        voice_risk=turn.get("voice_risk"), inject=turn.get("inject")))
    took = time.monotonic() - started

    events = [as_dict(e) for e in resp.events]
    engines = [f"{e['event_type'].split('_')[0]}={e['payload'].get('engine')}"
               for e in events if e["event_type"] in AGENT_EVENTS]
    live = sum(1 for x in engines if x.endswith("=live"))
    # Three labels, three meanings (see the emit site in run_multi_turn):
    #   "live"                             the model answered
    #   "deterministic (reference policy)" by design, no model needed
    #   "mock"                             the model was tried and FAILED  <- the only real problem
    designed = sum(1 for x in engines if "deterministic" in x)
    failed = sum(1 for x in engines if x.endswith("=mock"))

    print(f"{label:<7} {took:5.1f}s  {'  '.join(engines) or '(no agent card)'}")
    summary = f"live {live}/{len(engines)}"
    if designed:
        summary += f" · {designed} deterministic by design"
    print(f"{'':<7}        {summary}")
    if failed:
        print(f"{'':<7}        !! {failed} agent(s) genuinely fell back to mock")


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        chosen = [(k, SCENARIOS[k]) for k in args if k in SCENARIOS]
    elif "--all" in sys.argv:
        chosen = list(SCENARIOS.items())
    else:
        chosen = [(k, v) for k, v in SCENARIOS.items() if k.startswith("DEMO")]
        chosen.append(("HAPPY", SCENARIOS["HAPPY"]))

    print(f"LIVE_LLM={Settings.LIVE_LLM}  model={Settings.LLM_MODEL}  "
          f"turn_budget={Settings.TURN_DEADLINE_S}s  attempt_timeout={Settings.AGENT_TIMEOUT_S}s\n")
    for label, spec in chosen:
        await run_one(label, spec)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
