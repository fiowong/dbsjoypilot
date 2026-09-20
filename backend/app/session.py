from datetime import datetime
from typing import Dict, List, Optional
from .config import Settings
from .models import SessionState, Draft, Confirmation, ValidationResult

# 9. The 16-stage control chain — §8.2
# =====================================================================
STAGES = [
    (0,  "Authenticated session",             "Deterministic or mocked adapter"),
    (1,  "Audio-risk admission and ASR",      "Mocked control plus ASR"),
    (2,  "Immutable evidence commit",         "Deterministic"),
    (3,  "Guardrails Agent",                  "LLM with typed schema"),
    (4,  "Intent Classifier",                 "LLM with typed schema"),
    (5,  "Payment Agent",                     "LLM with typed schema"),
    (6,  "Payee and account resolution",      "Deterministic"),
    (7,  "Canonical draft and schema gate",   "Deterministic"),
    (8,  "Independent Validation Agent",      "LLM with typed schema"),
    (9,  "Validation comparator",             "Deterministic"),
    (10, "Risk and velocity policy",          "Deterministic"),
    (11, "Field-by-field confirmation",       "Deterministic UI"),
    (12, "Mock biometric adapter",            "Trusted simulated adapter"),
    (13, "Canonical payload signing",         "Deterministic cryptographic service"),
    (14, "Zero-Trust Gateway",                "Deterministic"),
    (15, "Restricted mock payment capability", "Deterministic"),
]


class Session:
    """Session state lives here, never in the chat transcript (§A.6 C3)."""
    def __init__(self, session_id: str, customer_id: str = "cust_auntie_001"):
        self.session_id = session_id
        self.customer_id = customer_id
        self.state: SessionState = SessionState.IDLE
        self.evidence: List[str] = []
        self.evidence_ids: List[str] = []
        self.draft: Optional[Draft] = None
        self.draft_versions: List[int] = []
        self.current_draft_version: int = 0
        self.confirmation: Optional[Confirmation] = None
        self.validation: Optional[ValidationResult] = None
        self.open_clarification_field: Optional[str] = None
        self.payee_phrase: Optional[str] = None
        self.amount_disambiguated: bool = False
        self.review_restarted: bool = False
        self.frozen: bool = False
        self.cancelled: bool = False
        self.cooling_off: bool = False
        self.drafts_created: int = 0
        self.step_up_required: bool = False
        self.gateway_origin_authorised: bool = True
        self.outcome: str = "—"
        self.confirmation_language: str = "EN"
        self._nonce: Optional[str] = None
        self._idempotency_key: Optional[str] = None
        self._payload: Optional[dict] = None
        self._signature: Optional[str] = None
        self._issued_at: Optional[datetime] = None
        self.execution: Optional[dict] = None

    def to_public(self) -> dict:
        return {
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "state": self.state.value,
            "outcome": self.outcome,
            "evidence": self.evidence,
            "evidence_ids": self.evidence_ids,
            "draft": self.draft.model_dump() if self.draft else None,
            "current_draft_version": self.current_draft_version,
            "confirmation": self.confirmation.model_dump() if self.confirmation else None,
            "open_clarification_field": self.open_clarification_field,
            "frozen": self.frozen,
            "cancelled": self.cancelled,
            "cooling_off": self.cooling_off,
            "ledger": "RECORDED" if self.execution else "NOT_INVOKED",
            "signature": "VALID" if self._signature else "NONE",
            "execution": self.execution,
            "engine": "live" if Settings.LIVE_LLM else "mock",
        }


SESSIONS: Dict[str, Session] = {}


def get_session(session_id: str) -> Session:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = Session(session_id)
    return SESSIONS[session_id]


# =====================================================================
