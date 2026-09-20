from __future__ import annotations
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field

# =====================================================================
# 1. Typed contracts — BRD §8.5.3, §8.6.3, §8.7.3, §8.8.3
# =====================================================================
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


class PaymentProposal(BaseModel):
    """§8.7.3. Untrusted. No beneficiary_id, no token, no approval, no signature."""
    status: Literal["PROPOSAL_READY", "NEEDS_CLARIFICATION", "RISK_DETECTED"]
    detected_language: Literal["EN", "ZH", "BM", "MIXED"]
    recipient_phrase: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Literal["SGD"] = "SGD"
    source_account_phrase: Optional[str] = None
    clarification_field: Optional[Literal["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"]] = None
    clarification_question: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)


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


class Draft(BaseModel):
    draft_id: str
    version: int
    transaction_type: Literal["SAVED_PAYEE_TRANSFER"] = "SAVED_PAYEE_TRANSFER"
    source_account_id: str
    beneficiary_id: str
    destination_token: str
    amount_minor: int
    currency: Literal["SGD"] = "SGD"
    reference: str
    payee_display: str
    payee_masked: str
    account_masked: str
    evidence_ids: List[str] = Field(default_factory=list)


class Confirmation(BaseModel):
    confirmation_id: str
    draft_id: str
    draft_version: int
    confirmed_fields: List[str] = Field(default_factory=list)
    language: Literal["EN", "ZH", "BM"] = "EN"
    status: Literal["OPEN", "COMPLETE", "INVALIDATED"] = "OPEN"


