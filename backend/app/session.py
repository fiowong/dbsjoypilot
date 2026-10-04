from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional
from .config import Settings
from .models import SessionState, Draft, Confirmation, ValidationResult, PersonalizationSignals
from .repository import get_repository

# 9. The 16-stage control chain, §8.2
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
        # IN-10, a queue of independent instructions raised in one breath. Each entry is a
        # fully-resolved, validated draft reviewed and confirmed one at a time. session.draft
        # always points at the instruction currently being confirmed (pending_drafts[draft_index]).
        self.pending_drafts: List[Draft] = []
        self.pending_validations: List[ValidationResult] = []
        self.pending_schedules: List[Optional[dict]] = []
        self.pending_references: List[Optional[str]] = []
        # The nickname the customer used for THIS queued payment, one entry per draft in the same
        # order as pending_drafts. session.nickname_used is a single slot that the first payment
        # consumes, so without a per-draft list the read-back of the second and later payments
        # falls back to the payee's own name ("to Mei Tan ending 7788") and loses the "I found
        # Ah Girl as Mei Tan" attribution the first read-back gave for the very same payment.
        self.pending_nicknames: List[Optional[str]] = []
        self.draft_index: int = 0
        self.multi_total: int = 0
        # True while a multi-instruction read-back has just ASKED the customer something
        # (confirming a slang amount like "a grand", or which account a payment should come from).
        # The review is technically CONFIRMATION_READY, but showing the "Confirm & Slide to Pay"
        # card at that moment puts two competing actions on screen at once, the card and the
        # question above it, and the customer cannot tell which to do first. The client defers
        # the card until they have answered. Cleared on the next turn, whatever they say.
        self.awaiting_multi_go_ahead: bool = False
        self.confirmation: Optional[Confirmation] = None
        self.validation: Optional[ValidationResult] = None
        self.open_clarification_field: Optional[str] = None
        self.payee_phrase: Optional[str] = None
        # The nickname question, while it is open. `{"nickname": "ah girl"}` once Joy has asked who
        # that is; `beneficiary_id` is added once the customer has answered, and the second turn
        # (the confirm) is what actually writes it to the store. Cleared the moment it is resolved,
        # so a stale binding can never leak into a later, unrelated payment.
        self.pending_nickname: Optional[dict] = None
        # The open "did you mean X?" suggestion. Set when a spoken payee name matches no saved
        # payee exactly but is clearly closest to ONE of them: `{"beneficiary_id", "display_name",
        # "spoken"}`. A fuzzy hit is only ever a SUGGESTION, nothing is bound until the customer
        # answers, and the destination still comes from the trusted saved-payee repository.
        self.pending_payee_suggestion: Optional[dict] = None
        # The nickname this draft was reached through ("ah girl"), carried to the read-back so Joy
        # can say "I found Ah Girl as Mei Tan", and carried in the audit so the claim is checkable.
        self.nickname_used: Optional[str] = None
        self.amount_disambiguated: bool = False
        self.review_restarted: bool = False
        self.frozen: bool = False
        self.cancelled: bool = False
        self.cooling_off: bool = False
        self.drafts_created: int = 0
        self.step_up_required: bool = False
        # Why a step-up was raised. "SPEAKER_MISMATCH" = Stage 1 could not verify the speaker, so
        # the request is HELD: the read-back is suppressed and the payment is not presented until
        # FaceID passes. "RISK" = the ordinary risk-agent step-up (e.g. a large amount), which
        # still reads the payment back. Only the speaker mismatch holds.
        self.step_up_reason: Optional[str] = None
        self.gateway_origin_authorised: bool = True
        self.outcome: str = "-"
        self.confirmation_language: str = "EN"
        # The language this conversation is being SPOKEN in, decided on the first turn from the
        # customer's own words and then held. Without it a code-mixed first turn gets an English
        # answer and a short Mandarin "是" on the next turn flips the whole conversation to
        # Chinese, so Auntie appears to change language mid-sentence for no reason. An explicit
        # request to switch (LANGUAGE_SWITCH_RE) is the only thing that moves it.
        self.interface_language: Optional[str] = None
        self._nonce: Optional[str] = None
        self._idempotency_key: Optional[str] = None
        self._payload: Optional[dict] = None
        self._signature: Optional[str] = None
        self._issued_at: Optional[datetime] = None
        self.execution: Optional[dict] = None

        # ---- Personalization signals: structured facts only, never the transcript ----
        self.field_rejection_counts: Dict[str, int] = {}
        self.clarification_history: List[str] = []
        self.intent_history: List[str] = []
        self.personalize_auto: bool = False
        self.personalization_signals: Optional[dict] = None
        self.personalization_decision: Optional[dict] = None

    def note_field_rejection(self, field: str) -> None:
        self.field_rejection_counts[field] = self.field_rejection_counts.get(field, 0) + 1

    def note_clarification(self, point: str) -> None:
        self.clarification_history.append(point)

    def note_intent(self, intent: str) -> None:
        self.intent_history.append(intent)

    def compute_personalization_signals(self) -> PersonalizationSignals:
        repeat_rejections = sorted(f for f, c in self.field_rejection_counts.items() if c >= 2)
        counts = Counter(self.clarification_history)
        repeat_clarifications = sorted(p for p, c in counts.items() if c >= 2)
        stalled = (len(self.intent_history) >= 2
                   and self.intent_history[-1] == "UNCLEAR"
                   and self.intent_history[-2] == "UNCLEAR")
        return PersonalizationSignals(repeat_field_rejections=repeat_rejections,
                                      repeat_clarifications=repeat_clarifications,
                                      stalled_intent=stalled)

    def to_public(self) -> dict:
        # The preferred name is trusted data from the customer record, never taken from
        # anything the customer typed (§A.6 C3). The app addresses the customer with it.
        customer = get_repository().get_customer(self.customer_id) or {}
        return {
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "customer_preferred_name": customer.get("preferred_name"),
            "state": self.state.value,
            "outcome": self.outcome,
            "evidence": self.evidence,
            "evidence_ids": self.evidence_ids,
            "draft": self.draft.model_dump() if self.draft else None,
            "current_draft_version": self.current_draft_version,
            # How many further independent instructions are still queued behind the current one
            # (IN-10). Drives the frontend's "review one at a time" loop; 0 means a single draft
            # or the last of a multi-instruction batch.
            "pending_remaining": max(0, len(self.pending_drafts) - self.draft_index - 1),
            "multi_total": self.multi_total,
            # The client reads this to hold back the confirm card while the read-back above it
            # still has an open question (see the field's note above).
            "awaiting_multi_go_ahead": self.awaiting_multi_go_ahead,
            "confirmation": self.confirmation.model_dump() if self.confirmation else None,
            "open_clarification_field": self.open_clarification_field,
            "frozen": self.frozen,
            "cancelled": self.cancelled,
            "cooling_off": self.cooling_off,
            # Identity step-up: the client raises FaceID BEFORE presenting the payment, and must
            # not present it at all while the request is held.
            "step_up_required": self.step_up_required,
            "step_up_reason": self.step_up_reason,
            "ledger": "RECORDED" if self.execution else "NOT_INVOKED",
            "signature": "VALID" if self._signature else "NONE",
            "execution": self.execution,
            "engine": "live" if Settings.LIVE_LLM else "mock",
            "personalize_auto": self.personalize_auto,
            "personalization_signals": self.personalization_signals,
            "personalization_decision": self.personalization_decision,
            # The language this conversation is being spoken in, settled by the backend on the
            # first turn and then held. Exposed so every client surface, the field review, the
            # biometric prompt, the receipt, reads the SAME language, instead of each one
            # re-guessing from whatever sentence the customer happened to say last.
            "interface_language": self.interface_language,
        }


SESSIONS: Dict[str, Session] = {}


def get_session(session_id: str) -> Session:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = Session(session_id)
    return SESSIONS[session_id]


# =====================================================================
