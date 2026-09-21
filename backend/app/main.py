from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import Settings
from .session import STAGES, get_session
from .data import POLICY
from .audit import AUDIT
from .orchestrator import (TurnRequest, TurnResponse, ConfirmRequest, run_turn, run_confirmation,
                            FieldSignalRequest, run_field_signal)
from .scenarios import SCENARIOS, ScenarioResponse, run_scenario

# 13. FastAPI surface
# =====================================================================
app = FastAPI(title="AuntieJoy", version="1.0",
              description="DBS DCTA Scenario 1 — voice-first conversational payments. "
                          "Four bounded agents, sixteen deterministic controls.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "engine": "live" if Settings.LIVE_LLM else "mock",
        "model": Settings.LLM_MODEL if Settings.LIVE_LLM else "deterministic-mock",
        "prompt_version": Settings.PROMPT_VERSION,
        "schema_version": Settings.SCHEMA_VERSION,
        "agents": ["GUARDRAILS AGENT", "INTENT CLASSIFIER", "PAYMENT AGENT",
                   "INDEPENDENT VALIDATION AGENT", "PERSONALIZATION AGENT"],
        "stages": len(STAGES),
        "branches": "A–F / 18 nodes",
        "disclosure": POLICY["disclosure"],
        "audit_db": Settings.DB_PATH,
    }


@app.get("/stages")
async def stages() -> dict:
    return {"stages": [{"stage": n, "control": c, "type": t} for n, c, t in STAGES]}


@app.get("/scenarios")
async def scenarios() -> dict:
    branch_labels = {
        "A": "Wrong person or untrusted input",
        "B": "Auntie is misunderstood",
        "C": "Malicious instructions affect the workflow",
        "D": "Auntie is being manipulated",
        "E": "Auntie does not understand the payment",
        "F": "Wrong, forged or repeated payment executed",
    }
    return {
        "scenarios": [{"id": k, "branch": v["branch"], "title": v["title"],
                       "turns": len(v["turns"]), "confirmation": bool(v.get("confirm"))}
                      for k, v in SCENARIOS.items()],
        "branches": [{"branch": b, "label": label,
                      "scenarios": [k for k, v in SCENARIOS.items() if v["branch"] == b]}
                     for b, label in branch_labels.items()],
    }


@app.post("/turn", response_model=TurnResponse)
async def turn(req: TurnRequest) -> TurnResponse:
    return await run_turn(req)


@app.post("/confirm", response_model=TurnResponse)
async def confirm(req: ConfirmRequest) -> TurnResponse:
    return await run_confirmation(req)


@app.post("/field_signal", response_model=TurnResponse)
async def field_signal(req: FieldSignalRequest) -> TurnResponse:
    return await run_field_signal(req)


@app.post("/scenario/{scenario_id}", response_model=ScenarioResponse)
async def scenario(scenario_id: str) -> ScenarioResponse:
    return await run_scenario(scenario_id.upper())


@app.get("/audit/{session_id}")
async def audit(session_id: str) -> dict:
    return {"session_id": session_id, "events": AUDIT.events(session_id),
            "chain": AUDIT.verify_chain(session_id)}


@app.get("/session/{session_id}")
async def session_detail(session_id: str) -> dict:
    return get_session(session_id).to_public()


# =====================================================================
