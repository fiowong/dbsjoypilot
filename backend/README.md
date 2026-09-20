# AuntieJoy backend

This is the same backend as `auntiejoy_backend.py` (Iggy's original single 2103-line file),
broken into one file per job. Nothing about how it behaves was changed — this was checked by
running the same 19-scenario self-test that the original file had, and it passes 19/19 with
identical results (state, outcome, ledger, audit chain) for every scenario.

## Folder layout

```
backend/
  requirements.txt
  app/
    __init__.py
    config.py        Settings (thresholds, secrets from env vars), now_iso(), money()
    models.py         All the typed data shapes (Pydantic models + enums) — Draft, Confirmation,
                       GuardrailResult, IntentResult, PaymentProposal, ValidationResult, etc.
    agent_runtime.py  Shared plumbing every agent uses: parsing a model's JSON reply, validating it
    data.py            The fake "bank" data — CUSTOMERS, SAVED_PAYEES, POLICY (all synthetic)
    parsing.py         Deterministic multilingual amount/payee/reference parsing (EN/ZH/BM)
    llm_client.py      The HTTP call to the real LLM (only used if a live API key is configured)
    agents.py           The 4 bounded agents: Guardrails, Intent Classifier, Payment, Validation
                       (each has a mock() and a live run())
    controls.py         Everything deterministic that's allowed to move money or freeze a payment:
                       payee/account resolution, risk policy, comparator, signing, the Ledger,
                       and the Zero-Trust Gateway check
    audit.py            The append-only, hash-chained audit log (SQLite)
    session.py         The 16-stage list (STAGES), the Session object, and the SESSIONS registry
    orchestrator.py    The state machine that actually runs a turn or a confirmation step
                       (run_turn, run_confirmation) — this is the biggest file, it's the "brain"
    scenarios.py        The 19 scripted BRD scenarios (HAPPY, A1-F3) and run_scenario()
    main.py             The FastAPI app and its routes (/health, /turn, /confirm, /scenario/...)
    selftest.py         The self-test that checks all 19 scenarios still behave correctly
```

## How the pieces depend on each other

Roughly bottom-up — each file only imports from the ones above it:

```
config → models → data → parsing → agent_runtime → llm_client → agents
                                                                    ↓
audit → session → controls → orchestrator → scenarios → main → selftest
```

Nothing here uses multiple copies of shared state by accident — `SESSIONS` (the dict of live
sessions), `AUDIT` (the audit log), and `Ledger` (the mock payment ledger) are each defined
in exactly one file and imported everywhere else, so they behave as the same singletons they
were in the single-file version.

## Running it

From this `backend/` folder:

```bash
pip install -r requirements.txt

# run the self-test (should print "19/19 scenarios passed")
python3 -m auntiejoy.selftest --selftest

# serve it
python3 -m auntiejoy.selftest --host 127.0.0.1 --port 8077
```

Or with uvicorn directly:

```bash
python3 -m uvicorn auntiejoy.main:app --host 127.0.0.1 --port 8077
```

The routes are identical to before: `/health`, `/stages`, `/scenarios`, `/turn`, `/confirm`,
`/scenario/{id}`, `/audit/{session_id}`, `/session/{session_id}`.

## What to touch for what

- Changing a threshold or limit → `config.py`
- Changing what a field looks like → `models.py`
- Changing the fake bank data (test customers/payees) → `data.py`
- Changing how "fifty dollars" gets parsed → `parsing.py`
- Changing an agent's prompt or its mock logic → `agents.py`
- Changing risk rules, signing, or the gateway check → `controls.py`
- Changing what happens step-by-step during a conversation → `orchestrator.py`
- Adding a new scripted demo scenario → `scenarios.py`
- Adding a new HTTP route → `main.py`
