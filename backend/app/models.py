from __future__ import annotations
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, ValidationError, field_validator

# =====================================================================
# 1. Typed contracts — BRD §8.5.3, §8.6.3, §8.7.3, §8.8.3
# =====================================================================
# The currencies the demo can actually hold. This was a SGD-only literal, which is why
# "Send US$200" silently became a Singapore-dollar payment — the draft had nowhere to put a
# different currency. Widening the type is what lets the value the customer named survive
# all the way to the read-back they authorise.
Currency = Literal["SGD", "USD"]
class GuardrailDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    SAFETY_INTERVENTION = "SAFETY_INTERVENTION"
    CLARIFY = "CLARIFY"


class RiskCategory(str, Enum):
    SAFE_PAYMENT_REQUEST = "SAFE_PAYMENT_REQUEST"
    CONFIRMATION_BYPASS = "CONFIRMATION_BYPASS"
    INSTRUCTION_OVERRIDE = "INSTRUCTION_OVERRIDE"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    SCAM_OR_COERCION_SIGNAL = "SCAM_OR_COERCION_SIGNAL"
    UNUSABLE_INPUT = "UNUSABLE_INPUT"


class GuardrailResult(BaseModel):
    """§8.5.3. isSafe / shouldPassToIntentClassifier are deliberately absent — they duplicate decision."""
    decision: GuardrailDecision
    risk_category: RiskCategory
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    detected_language: Literal["EN", "ZH", "BM", "MIXED"]
    message_key: str
    developer_reason: str


Intent = Literal[
    "PAYMENT", "PAYMENT_CLARIFICATION", "EDIT_PAYMENT",
    "CANCEL_OR_STOP", "SAFETY_HELP", "UNCLEAR",
]


class IntentResult(BaseModel):
    """§8.6.3. targetAgent removed (orchestration maps intent); clarificationNeeded removed (duplicates UNCLEAR)."""
    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    detected_language: Literal["EN", "ZH", "BM", "MIXED"]
    reason: str


class ScheduleHint(BaseModel):
    """IN-08. A spoken future time, EXTRACTED by the Payment Agent — never resolved by it.

    The agent may only restate WHAT the customer said into this shape. It must not put a date, a
    day number or a year in here: it has no clock, and an agent that emitted a date would be
    deciding when money moves — the same reason no agent decides an amount.
    parsing.compute_schedule() validates every field and computes the actual date.

    `kind` is a plain string rather than a Literal on purpose. A Literal makes one hallucinated
    kind fail the whole PaymentProposal schema, which on the live path drops the entire turn back
    to the mock; an unknown kind here is simply rejected by the deterministic validator, and the
    turn carries on with no schedule.
    """

    kind: Optional[str] = None          # today | tomorrow | weekday | in_days | day_of_month |
                                        # named_date | end_of_month
    weekday: Optional[str] = None
    next: Optional[bool] = None         # weekday only: the customer said "next <day>"
    days: Optional[int] = None          # in_days only: a day count (weeks already multiplied out)
    day: Optional[int] = None           # day_of_month / named_date only: 1-31
    month: Optional[int] = None         # named_date only: 1-12, the month the customer named
    months_ahead: Optional[bool] = None # day_of_month / end_of_month: next month rather than this
    hour: Optional[int] = None          # 0-23
    minute: Optional[int] = None


class PaymentProposal(BaseModel):
    """§8.7.3. Untrusted. No beneficiary_id, no token, no approval, no signature."""
    status: Literal["PROPOSAL_READY", "NEEDS_CLARIFICATION", "RISK_DETECTED"]
    detected_language: Literal["EN", "ZH", "BM", "MIXED"]
    recipient_phrase: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Currency = "SGD"
    source_account_phrase: Optional[str] = None
    # The proposed transaction shape. The Payment Agent sets this so deterministic code can
    # bind the right kind of authoritative record (saved payee vs biller). Absent = a saved
    # payee transfer (the MVP default), exactly as before.
    transaction_type: Optional[str] = None
    # A spoken future-execution time, extracted but NOT resolved (see ScheduleHint). Consulted
    # only when the deterministic patterns in parsing.schedule_spec_from_text find no day word,
    # so a phrase the patterns already understand is never second-guessed by the model.
    schedule_hint: Optional[ScheduleHint] = None
    clarification_field: Optional[Literal["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"]] = None
    clarification_question: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)

    @field_validator("schedule_hint", mode="before")
    @classmethod
    def _a_fumbled_hint_must_not_cost_the_payment(cls, value):
        """Absorb a schedule_hint the agent fumbled, rather than failing the whole proposal.

        The live model occasionally returns a bare string where an object belongs, or a word
        where a number belongs. The agent runtime fails CLOSED on any schema deviation, so letting
        that error escape would drop the entire Payment Agent — and with it the payment — back to
        the mock over an optional detail. A hint that cannot be read is simply dropped: the
        payment is then exactly what it would have been without one. The deterministic patterns in
        parsing remain the primary extractor, and schedule_spec_from_hint validates every field of
        a hint that does survive.
        """
        if value is None or isinstance(value, ScheduleHint):
            return value
        if not isinstance(value, dict):
            return None
        try:
            ScheduleHint.model_validate(value)
        except ValidationError:
            return None
        return value


REQUIRED_VALIDATION_FIELDS = {"RECIPIENT", "AMOUNT", "CURRENCY", "SOURCE_ACCOUNT", "TRANSACTION_TYPE"}


class FieldCheck(BaseModel):
    field_name: Literal["RECIPIENT", "AMOUNT", "CURRENCY", "SOURCE_ACCOUNT", "TRANSACTION_TYPE"]
    result: Literal["MATCH", "MISMATCH", "UNCERTAIN"]
    evidence_source: Literal[
        "TRANSCRIPT", "CLARIFICATION_TURN", "VERIFIED_SESSION_FACT",
        "PRODUCT_RULE", "MODEL_INFERENCE", "EXTERNAL_UNTRUSTED_DATA",
    ]
    evidence_value: Optional[str] = None
    draft_value: Optional[str] = None


class ValidationResult(BaseModel):
    """§8.8.3. PASS means only that the audit found the draft supported by evidence."""
    status: Literal["PASS", "FREEZE"]
    field_checks: List[FieldCheck]
    risk_flags: List[str] = Field(default_factory=list)


class SessionState(str, Enum):
    """BRD §8.5.7 / §8.6.5 deterministic routing.

    Note: BLOCK and SAFETY_INTERVENTION both route to FROZEN — the *reason* is carried by
    the outcome vocabulary, not by inventing extra states. FROZEN means the same thing to
    every downstream control: no confirmation, no signature, no gateway call.
    """
    IDLE = "IDLE"
    CAPTURED = "CAPTURED"
    EVIDENCE_COMMITTED = "EVIDENCE_COMMITTED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    DRAFTING = "DRAFTING"
    DRAFT_READY = "DRAFT_READY"
    FROZEN = "FROZEN"
    COOLING_OFF = "COOLING_OFF"
    CONFIRMATION_READY = "CONFIRMATION_READY"
    CONFIRMATION_COMPLETE = "CONFIRMATION_COMPLETE"
    SIGNED = "SIGNED"
    # A future-dated payment the customer has confirmed. Distinct from EXECUTED on purpose: the
    # ledger was never invoked, no money moved, and no receipt exists. "Scheduled" and "executed"
    # must never be the same state to a downstream control or to the customer.
    SCHEDULED = "SCHEDULED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class SourceAccount(BaseModel):
    account_id: str
    display_name: str
    masked: str
    eligible: bool = True


class Payee(BaseModel):
    beneficiary_id: str
    display_name: str
    masked: str
    destination_token: str
    verified_saved_payee: bool = True
    untrusted_note: Optional[str] = None       # external, untrusted — never authoritative (§8.3.2)


class Biller(BaseModel):
    """A company the customer pays a recurring bill to (e.g. StarHub). Distinct from a saved
    payee: a biller is selected from a biller directory, not registered as a person. The masked
    field is the bill reference the customer recognises, not a bank account ending."""
    biller_id: str
    display_name: str
    masked: str
    latest_amount_minor: int
    currency: Currency = "SGD"


class Card(BaseModel):
    """The customer's own credit card. Paid from a source account (typically in full). Distinct
    from a biller: a card balance is settled from the customer's own money, not a third-party
    bill. The masked field is the card reference the customer recognises."""
    card_id: str
    display_name: str
    masked: str
    latest_balance_minor: int
    currency: Currency = "SGD"


class OwnAccount(BaseModel):
    """An internal destination the customer can move money to (e.g. their joint account). Distinct
    from a saved payee: it is the customer's OWN account, not a third party, so a move stays
    inside the customer's banking relationship. The masked field is the account ending digits."""
    account_id: str
    display_name: str
    masked: str


class Draft(BaseModel):
    draft_id: str
    version: int
    transaction_type: Literal[
        "SAVED_PAYEE_TRANSFER", "BILL_PAYMENT", "CREDIT_CARD_PAYMENT", "OWN_ACCOUNT_TRANSFER"
    ] = "SAVED_PAYEE_TRANSFER"
    source_account_id: str
    beneficiary_id: str
    destination_token: str
    amount_minor: int
    currency: Currency = "SGD"
    reference: str
    payee_display: str
    payee_masked: str
    account_masked: str
    # A future execution time the customer named (e.g. "tomorrow morning at nine"). Deterministic
    # code parses it from the turn; it is an annotation on the read-back, not an execution trigger
    # in this MVP (the mock ledger still records on confirmation — a scheduled send is out of scope).
    scheduled_for: Optional[str] = None
    evidence_ids: List[str] = Field(default_factory=list)


class Confirmation(BaseModel):
    confirmation_id: str
    draft_id: str
    draft_version: int
    confirmed_fields: List[str] = Field(default_factory=list)
    language: Literal["EN", "ZH", "BM"] = "EN"
    status: Literal["OPEN", "COMPLETE", "INVALIDATED"] = "OPEN"


# =====================================================================
# Personalization Agent — 5th bounded agent. Reasons ONLY over structured,
# already-computed facts about what happened mechanically in this session.
# It is never given the transcript, so it cannot infer age, ability, accent
# or identity from how the customer speaks or types (see agents.py docstring).
# =====================================================================
class PersonalizationSignals(BaseModel):
    """Every field here is a fact about what happened, never about how it was said.

    A single rejection/clarification is normal system behaviour (the system caught
    or asked about something) — only a REPEAT on the same point is a friction signal.
    """
    repeat_field_rejections: List[Literal["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"]] = Field(default_factory=list)
    repeat_clarifications: List[str] = Field(default_factory=list)
    stalled_intent: bool = False


class PersonalizationDecision(BaseModel):
    """Output is a score + reason, not a black-box probability — every point is traceable
    to one of the three PersonalizationSignals fields (see PERSONALIZATION_WEIGHTS)."""
    personalize: bool
    score: int
    reason_code: Literal["NONE", "REPEAT_REJECTION", "REPEAT_CLARIFICATION", "STALLED_INTENT", "COMBINED"]
    developer_reason: str


