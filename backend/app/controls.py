import difflib
import hashlib
from enum import Enum
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field
from .config import Settings, money
from .models import (Draft, Confirmation, PaymentProposal, ValidationResult, GuardrailResult,
                      REQUIRED_VALIDATION_FIELDS, SourceAccount)
from .data import POLICY
from .repository import get_repository
from .parsing import PAYEE_ALIASES
from . import nicknames

# 7. Deterministic controls, the half that is allowed to move money
# =====================================================================
class DeterministicResolution(BaseModel):
    outcome: Literal["RESOLVED", "NEEDS_CLARIFICATION", "FREEZE"]
    draft: Optional[Draft] = None
    clarification_field: Optional[str] = None
    # A message_key, not a sentence. Deterministic code says WHAT is unclear and hands over
    # the candidates; messages.py decides how that is phrased, in which language, and how the
    # customer is addressed. Nothing in this file writes prose the customer will read.
    clarification_key: Optional[str] = None
    candidates: List[dict] = Field(default_factory=list)
    reason: Optional[str] = None
    # Set on the FREEZE path so the customer can be told *which* record was suspicious.
    payee_display: Optional[str] = None
    # "display_name" or "nickname", how the payee was bound. Audited on PAYEE_RESOLUTION so the
    # inspector can show that a nickname pointed at a trusted beneficiary, and that the nickname
    # itself never supplied the destination.
    matched_via: Optional[str] = None


# Freeze reasons -> the catalogue key that explains them to the customer. Kept here because
# this module owns the reason vocabulary; messages.py owns the wording.
FREEZE_MESSAGE_KEYS = {
    "UNTRUSTED_PAYEE_METADATA_DETECTED": "FROZEN_UNTRUSTED_METADATA",
    "NO_ELIGIBLE_SOURCE_ACCOUNT": "FROZEN_NO_ACCOUNT",
}


def freeze_message_key(reason: Optional[str]) -> str:
    return FREEZE_MESSAGE_KEYS.get(reason or "", "FROZEN_RISK")


def detect_multiple_recipients(text: str, customer_id: str) -> Optional[List[str]]:
    """§MVP, one saved-payee transfer at a time. Return the display names of 2+ *distinct* saved
    payees named in the turn, else None.

    Matching is on the full display name or a full-name alias only, so a bare "john" (which is
    ambiguous between the two Johns) is NOT counted here, that case is the existing PAYEE_AMBIGUOUS
    clarification, a different situation. This keeps a request like "pay John" from being misread as
    "pay both Johns".
    """
    payees = get_repository().get_saved_payees(customer_id)
    lowered = text.lower()
    nick_index = nicknames.index(customer_id)
    found: List[str] = []
    for p in payees:
        hit = p.display_name.lower() in lowered
        if not hit:
            for alias, canon in PAYEE_ALIASES.items():
                if " " in alias and canon == p.display_name.lower() \
                        and re.search(rf"\b{re.escape(alias)}\b", lowered):
                    hit = True
                    break
        if not hit:
            # A nickname counts as naming that payee, so "pay mum and john" is still two people.
            for word, owner in nick_index.items():
                if owner.beneficiary_id == p.beneficiary_id \
                        and re.search(rf"\b{re.escape(word)}\b", lowered):
                    hit = True
                    break
        if hit:
            found.append(p.display_name)
    # de-dup, preserving order
    seen: set = set()
    distinct = [n for n in found if not (n in seen or seen.add(n))]
    return distinct if len(distinct) >= 2 else None


def _eligible_accounts(customer_id: str) -> List[SourceAccount]:
    """The customer's eligible funding accounts, read from the trusted repository.

    One helper so every transaction shape picks a source account the same way. An unknown
    customer yields no accounts, which the caller turns into NO_ELIGIBLE_SOURCE_ACCOUNT
    rather than a KeyError: a missing record must freeze the payment, never crash the turn.
    """
    record = get_repository().get_customer(customer_id) or {}
    return [a for a in record.get("accounts", []) if a.eligible]


def _name_similarity(a: str, b: str) -> float:
    """How close two names are, 0..1. Blends whole-string similarity with token overlap so
    BOTH a one-letter miss ("me tan" vs "mei tan") and a surname alone ("Tan" vs "Mei Tan")
    score high. Pure difflib, no dependency and no model, so the result is reproducible."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    whole = difflib.SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    overlap = len(at & bt) / max(1, len(at))
    best_tok = max((difflib.SequenceMatcher(None, x, y).ratio() for x in at for y in bt),
                   default=0.0)
    return max(whole, overlap, best_tok)


def closest_saved_payee(customer_id: str, spoken: str,
                        threshold: float = 0.72, margin: float = 0.15):
    """The ONE saved payee a spoken name is closest to, when it is close enough AND clearly
    ahead of the runner-up. Returns None when nothing is close, or when two payees are equally
    close (that is the PAYEE_AMBIGUOUS question, not a guess).

    A fuzzy hit is a SUGGESTION only. This never binds anything: the caller must ask the customer
    to confirm, and the destination still comes from the trusted saved-payee repository, so a
    near-miss can offer a payee the customer already holds but can never invent one."""
    if not spoken or not spoken.strip():
        return None
    scored = sorted(((_name_similarity(spoken, p.display_name), p)
                     for p in get_repository().get_saved_payees(customer_id)),
                    key=lambda t: t[0], reverse=True)
    if not scored:
        return None
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < threshold or (best_score - second) < margin:
        return None
    return best


def resolve_payee_and_account(customer_id: str, proposal: PaymentProposal,
                              payee_phrase: Optional[str]) -> DeterministicResolution:
    """§8.3.2, the LLM proposes a phrase; deterministic code binds the authoritative record."""
    if proposal.status != "PROPOSAL_READY":
        return DeterministicResolution(outcome="NEEDS_CLARIFICATION",
                                       clarification_field=proposal.clarification_field,
                                       clarification_key="RECIPIENT_MISSING")

    # A bill payment binds a biller from the biller directory, not a saved person. The amount
    # comes from the proposal (the outstanding bill), and the source account is chosen exactly
    # as for a saved-payee transfer.
    if proposal.transaction_type == "BILL_PAYMENT":
        billers = get_repository().get_billers(customer_id)
        needle = (payee_phrase or proposal.recipient_phrase or "").lower()
        biller = next((b for b in billers
                       if b.display_name.lower().startswith(needle)
                       or needle in b.display_name.lower()), None)
        if not biller:
            return DeterministicResolution(outcome="NEEDS_CLARIFICATION",
                                           clarification_field="RECIPIENT",
                                           clarification_key="PAYEE_NOT_FOUND")
        eligible = _eligible_accounts(customer_id)
        if not eligible:
            return DeterministicResolution(outcome="FREEZE", reason="NO_ELIGIBLE_SOURCE_ACCOUNT",
                                           payee_display=biller.display_name)
        want = (proposal.source_account_phrase or "").lower()
        account = next((a for a in eligible if want and want in a.display_name.lower()), None)
        if account is None:
            account = eligible[0]
        return DeterministicResolution(outcome="RESOLVED", draft=Draft(
            draft_id="dr_pending", version=1,
            transaction_type="BILL_PAYMENT",
            source_account_id=account.account_id, beneficiary_id=biller.biller_id,
            destination_token=biller.masked,
            amount_minor=proposal.amount_minor or biller.latest_amount_minor, currency=proposal.currency,
            reference="", payee_display=biller.display_name, payee_masked=biller.masked,
            account_masked=account.masked,
            source_account_display=account.display_name))

    # A credit-card payment settles the customer's own card balance from a source account. The
    # amount is the card's latest balance (PRODUCT_RULE), a number they happened to say alongside
    # "credit card" is never the amount; that would be a false read-back, exactly as for a bill.
    if proposal.transaction_type == "CREDIT_CARD_PAYMENT":
        cards = get_repository().get_cards(customer_id)
        needle = (payee_phrase or proposal.recipient_phrase or "").lower()
        card = next((c for c in cards
                     if c.display_name.lower().startswith(needle)
                     or needle in c.display_name.lower()), None)
        if not card:
            return DeterministicResolution(outcome="NEEDS_CLARIFICATION",
                                           clarification_field="RECIPIENT",
                                           clarification_key="PAYEE_NOT_FOUND")
        eligible = _eligible_accounts(customer_id)
        if not eligible:
            return DeterministicResolution(outcome="FREEZE", reason="NO_ELIGIBLE_SOURCE_ACCOUNT",
                                           payee_display=card.display_name)
        want = (proposal.source_account_phrase or "").lower()
        account = next((a for a in eligible if want and want in a.display_name.lower()), None)
        if account is None:
            account = eligible[0]
        return DeterministicResolution(outcome="RESOLVED", draft=Draft(
            draft_id="dr_pending", version=1,
            transaction_type="CREDIT_CARD_PAYMENT",
            source_account_id=account.account_id, beneficiary_id=card.card_id,
            destination_token=card.masked,
            amount_minor=proposal.amount_minor or card.latest_balance_minor, currency=proposal.currency,
            reference="", payee_display=card.display_name, payee_masked=card.masked,
            account_masked=account.masked,
            source_account_display=account.display_name))

    # An own-account move shifts money between the customer's own accounts (e.g. savings -> joint).
    # The destination is an internal account, so it uses BODY_MOVE, not BODY_TRANSFER.
    if proposal.transaction_type == "OWN_ACCOUNT_TRANSFER":
        oas = get_repository().get_own_accounts(customer_id)
        needle = (payee_phrase or proposal.recipient_phrase or "").lower()
        oa = next((o for o in oas
                   if o.display_name.lower().startswith(needle)
                   or needle in o.display_name.lower()), None)
        if not oa:
            return DeterministicResolution(outcome="NEEDS_CLARIFICATION",
                                           clarification_field="RECIPIENT",
                                           clarification_key="PAYEE_NOT_FOUND")
        eligible = _eligible_accounts(customer_id)
        if not eligible:
            return DeterministicResolution(outcome="FREEZE", reason="NO_ELIGIBLE_SOURCE_ACCOUNT",
                                           payee_display=oa.display_name)
        want = (proposal.source_account_phrase or "").lower()
        account = next((a for a in eligible if want and want in a.display_name.lower()), None)
        if account is None:
            account = eligible[0]
        return DeterministicResolution(outcome="RESOLVED", draft=Draft(
            draft_id="dr_pending", version=1,
            transaction_type="OWN_ACCOUNT_TRANSFER",
            source_account_id=account.account_id, beneficiary_id=oa.account_id,
            destination_token=oa.masked,
            amount_minor=proposal.amount_minor or 0, currency=proposal.currency,
            reference="", payee_display=oa.display_name, payee_masked=oa.masked,
            account_masked=account.masked,
            source_account_display=account.display_name))

    payees = get_repository().get_saved_payees(customer_id)
    needle = (payee_phrase or proposal.recipient_phrase or "").lower()
    matches = [p for p in payees
               if p.display_name.lower().startswith(needle) or needle in p.display_name.lower()]
    matched_via = "display_name"
    if not matches and needle:
        # A NICKNAME the customer uses out loud. The nickname supplies the LOOKUP KEY only, the
        # payee record, its masked ending and its destination token all come from the trusted
        # repository, so a nickname can never introduce a destination of its own.
        by_nickname = nicknames.find(customer_id, needle)
        if by_nickname is not None:
            matches, matched_via = [by_nickname], "nickname"

    if not matches:
        # No exact/substring match. Before giving up with a bare "who would you like to pay?", ask
        # whether the spoken name is clearly closest to ONE saved payee ("me tan" -> "Mei Tan",
        # "Tan" -> "Mei Tan"). If so, offer it as a question ("did you mean Mei Tan?"). Still a
        # question: nothing binds until the customer confirms.
        near = closest_saved_payee(customer_id, payee_phrase or proposal.recipient_phrase or "")
        if near is not None:
            return DeterministicResolution(
                outcome="NEEDS_CLARIFICATION", clarification_field="RECIPIENT",
                clarification_key="PAYEE_DID_YOU_MEAN",
                candidates=[{"beneficiary_id": near.beneficiary_id,
                             "displayName": near.display_name,
                             "maskedDestination": near.masked}])
        return DeterministicResolution(outcome="NEEDS_CLARIFICATION",
                                       clarification_field="RECIPIENT",
                                       clarification_key="PAYEE_NOT_FOUND")
    if len(matches) > 1:
        # The candidates travel as structured records. The question that names them is built
        # from this list by messages.candidate_options(), so the customer is always told
        # WHICH John, instead of a generic "which one do you mean?".
        return DeterministicResolution(
            outcome="NEEDS_CLARIFICATION", clarification_field="RECIPIENT",
            clarification_key="PAYEE_AMBIGUOUS",
            candidates=[{"beneficiary_id": p.beneficiary_id, "displayName": p.display_name,
                         "maskedDestination": p.masked} for p in matches])

    payee = matches[0]
    # §6.4 C2 / §A.6, untrusted display metadata may never establish or replace the destination
    if payee.untrusted_note and re.search(
            r"ignore|skip|change the destination|instruction|override", payee.untrusted_note, re.I):
        return DeterministicResolution(outcome="FREEZE",
                                       reason="UNTRUSTED_PAYEE_METADATA_DETECTED",
                                       payee_display=payee.display_name)

    eligible = _eligible_accounts(customer_id)
    if not eligible:
        return DeterministicResolution(outcome="FREEZE", reason="NO_ELIGIBLE_SOURCE_ACCOUNT",
                                       payee_display=payee.display_name)

    # The agent proposes a phrase ("usd", "savings", "current"); deterministic code binds the
    # authoritative record. A phrase nothing matches, or no phrase at all, falls back to the
    # customer's FIRST eligible account, which is the one the demo's everyday transfers use.
    want = (proposal.source_account_phrase or "").lower()
    account = next((a for a in eligible if want and want in a.display_name.lower()), None)
    if account is None:
        account = eligible[0]

    return DeterministicResolution(outcome="RESOLVED", draft=Draft(
        draft_id="dr_pending", version=1,
        source_account_id=account.account_id, beneficiary_id=payee.beneficiary_id,
        destination_token=payee.destination_token,
        amount_minor=proposal.amount_minor or 0, currency=proposal.currency,
        reference="", payee_display=payee.display_name, payee_masked=payee.masked,
        account_masked=account.masked,
        source_account_display=account.display_name), matched_via=matched_via)


class ComparatorResult(BaseModel):
    result: Literal["PASS", "FAIL"]
    reason: Optional[str] = None


def compare_validation_result(validation: ValidationResult, draft: Draft,
                              frozen: bool) -> ComparatorResult:
    """§8.9, the Validation Agent's PASS value is insufficient by itself."""
    names = [c.field_name for c in validation.field_checks]
    if len(names) != len(REQUIRED_VALIDATION_FIELDS):
        return ComparatorResult(result="FAIL", reason="WRONG_FIELD_CHECK_COUNT")
    if set(names) != REQUIRED_VALIDATION_FIELDS:
        return ComparatorResult(result="FAIL", reason="MISSING_OR_UNKNOWN_FIELD")
    if len(names) != len(set(names)):
        return ComparatorResult(result="FAIL", reason="DUPLICATE_FIELD_CHECK")
    mismatched = [c for c in validation.field_checks if c.result != "MATCH"]
    if mismatched:
        return ComparatorResult(result="FAIL", reason=f"{mismatched[0].field_name}_MISMATCH")
    if validation.risk_flags:
        return ComparatorResult(result="FAIL", reason="VALIDATION_RISK_FLAGS_PRESENT")
    if validation.status != "PASS":
        return ComparatorResult(result="FAIL", reason="VALIDATION_NOT_PASS")
    if frozen:
        return ComparatorResult(result="FAIL", reason="TRANSACTION_FROZEN")
    return ComparatorResult(result="PASS")


class RiskDecision(str, Enum):
    PASS = "PASS"
    STEP_UP = "STEP_UP"
    COOLING_OFF = "COOLING_OFF"
    REJECT = "REJECT"


class RiskOutcome(BaseModel):
    decision: RiskDecision
    reasons: List[str] = Field(default_factory=list)
    limit_minor: int
    disclosure: str


def evaluate_risk(draft: Draft, session: dict, guardrail: Optional[GuardrailResult]) -> RiskOutcome:
    """§8.10: risk consequences are selected by configuration, never by a model."""
    reasons: List[str] = []
    if draft.amount_minor > POLICY["max_transfer_minor"]:
        reasons.append("AMOUNT_ABOVE_SYNTHETIC_LIMIT")
    elif draft.amount_minor > Settings.STEP_UP_HIGH_VALUE_MINOR:
        reasons.append("HIGH_VALUE_STEP_UP")
    if session.get("drafts_created", 0) >= Settings.VELOCITY_MAX_DRAFTS_PER_SESSION:
        reasons.append("VELOCITY_EXCEEDED")
    if draft.beneficiary_id in POLICY["high_risk_payee_ids"]:
        reasons.append("HIGH_RISK_PAYEE")
    if guardrail and guardrail.risk_level in ("HIGH", "CRITICAL") and \
       guardrail.risk_category == RiskCategory.SCAM_OR_COERCION_SIGNAL:
        reasons.append("SCAM_INDICATOR")

    if "AMOUNT_ABOVE_SYNTHETIC_LIMIT" in reasons or "HIGH_RISK_PAYEE" in reasons:
        decision = RiskDecision.REJECT
    elif "SCAM_INDICATOR" in reasons:
        decision = RiskDecision.COOLING_OFF
    elif reasons:
        decision = RiskDecision.STEP_UP
    else:
        decision = RiskDecision.PASS

    return RiskOutcome(decision=decision, reasons=reasons,
                       limit_minor=POLICY["max_transfer_minor"], disclosure=POLICY["disclosure"])


# A reported "safe account" instruction is the one unambiguous scam pattern: stop it outright.
# Every other safety signal is weighed by the deterministic policy, which may prefer a
# cooling-off, a delay, not an accusation (§A.7 D1 vs D3).
HARD_STOP_SCAM = re.compile(r"\bsafe account\b", re.I)


def evaluate_safety_policy(guardrail: GuardrailResult, text: str) -> RiskOutcome:
    """Deterministic safety consequence. Never selected by a model (§8.10)."""
    reasons: List[str] = []
    if HARD_STOP_SCAM.search(text):
        reasons.append("SAFE_ACCOUNT_SCAM_PATTERN")
        decision = RiskDecision.REJECT
    else:
        reasons.extend(["SCAM_INDICATOR", f"RISK_LEVEL_{guardrail.risk_level}"])
        decision = RiskDecision.COOLING_OFF
    return RiskOutcome(decision=decision, reasons=reasons,
                       limit_minor=POLICY["max_transfer_minor"], disclosure=POLICY["disclosure"])


# ---------- signing, gateway, ledger (§8.11.4-8.11.6) ----------
def canonical_payload(draft: Draft, confirmation: Confirmation, nonce: str,
                      idempotency_key: str, issued_at: datetime) -> dict:
    return {
        "draft_id": draft.draft_id,
        "draft_version": draft.version,
        "confirmation_id": confirmation.confirmation_id,
        "transaction_type": draft.transaction_type,
        "source_account_id": draft.source_account_id,
        "beneficiary_id": draft.beneficiary_id,
        "destination_token": draft.destination_token,
        "amount_minor": draft.amount_minor,
        "currency": draft.currency,
        "reference": draft.reference,
        "nonce": nonce,
        "idempotency_key": idempotency_key,
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (issued_at + timedelta(seconds=Settings.PAYLOAD_TTL_SECONDS))
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def payload_hash(payload: dict) -> str:
    return "hash_" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def sign_payload(payload: dict) -> str:
    """HMAC, explicitly a demonstration mechanism. A bare SHA-256 is not a signature (§8.11.4)."""
    return "sig_" + hmac.new(Settings.SIGNING_SECRET.encode(),
                             json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                             hashlib.sha256).hexdigest()[:32]


class GatewayDecision(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    RETURN_EXISTING_RESULT = "RETURN_EXISTING_RESULT"


class GatewayResult(BaseModel):
    decision: GatewayDecision
    reason_code: str
    checks: Dict[str, str] = Field(default_factory=dict)
    ledger_invoked: bool = False
    transaction_id: Optional[str] = None


class Ledger:
    """The restricted mock payment capability. Only the gateway may call it (§5.2)."""
    _rows: Dict[str, dict] = {}
    _nonces: set[str] = set()
    _idempotency: Dict[str, str] = {}

    @classmethod
    def execute(cls, idempotency_key: str, payload: dict) -> Tuple[str, int, bool]:
        if idempotency_key in cls._idempotency:
            txn = cls._idempotency[idempotency_key]
            return txn, 1, False                      # idempotent retry, no new entry
        txn_id = f"txn_{len(cls._rows) + 1:03d}"
        cls._rows[txn_id] = {"transaction_id": txn_id, "status": "RECORDED",
                             "amount_minor": payload["amount_minor"],
                             "currency": payload["currency"], "execution_count": 1}
        cls._idempotency[idempotency_key] = txn_id
        return txn_id, 1, True

    @classmethod
    def rows(cls) -> List[dict]:
        return list(cls._rows.values())

    @classmethod
    def reset(cls) -> None:
        cls._rows, cls._nonces, cls._idempotency = {}, set(), {}


def gateway_verify(payload: dict, signature: str, claimed: dict, draft: Draft,
                   confirmation: Confirmation, validation: ValidationResult,
                   comparator: ComparatorResult, risk: RiskOutcome, session: dict,
                   request_payload: Optional[dict] = None) -> GatewayResult:
    """§8.11.5. The gateway ignores client or agent claims entirely."""
    subject = request_payload if request_payload is not None else payload
    checks: Dict[str, str] = {}
    checks["payload_schema"] = "PASS" if set(subject) >= set(payload) else "FAIL"
    checks["recalculated_payload_hash"] = "PASS" if payload_hash(subject) == payload_hash(payload) else "FAIL"
    checks["signature"] = "PASS" if hmac.compare_digest(sign_payload(subject), signature) else "FAIL"
    checks["draft_version_current"] = "PASS" if draft.version == session.get("current_draft_version") else "FAIL"
    checks["confirmation_valid"] = "PASS" if confirmation.status == "COMPLETE" else "FAIL"
    checks["confirmation_matches_draft"] = (
        "PASS" if confirmation.draft_version == draft.version and confirmation.draft_id == draft.draft_id
        else "FAIL")
    checks["validation"] = "PASS" if validation.status == "PASS" else "FAIL"
    checks["comparator"] = "PASS" if comparator.result == "PASS" else "FAIL"
    # STEP_UP is an allowed risk outcome whose extra verification (the transaction-specific
    # FaceID at stage 12) has already been satisfied by the time we reach the gateway, so it
    # passes, exactly like a PASS. REJECT/COOLING_OFF still fail. This makes the amount-based
    # step-up symmetric with the voice-based step-up (which keeps risk=PASS and only sets
    # session.step_up_required).
    checks["risk_policy"] = "PASS" if risk.decision in (RiskDecision.PASS, RiskDecision.STEP_UP) else "FAIL"
    checks["freeze_status"] = "PASS" if not session.get("frozen") else "FAIL"
    checks["cancellation_status"] = "PASS" if not session.get("cancelled") else "FAIL"
    checks["nonce_fresh"] = "PASS" if payload["nonce"] not in Ledger._nonces else "FAIL"
    checks["expiry"] = "PASS" if _not_expired(payload) else "FAIL"
    checks["origin"] = "PASS" if session.get("gateway_origin_authorised", True) else "FAIL"

    # --- technical retry with a consumed idempotency key returns the original receipt
    if payload["idempotency_key"] in Ledger._idempotency:
        txn = Ledger._idempotency[payload["idempotency_key"]]
        return GatewayResult(decision=GatewayDecision.RETURN_EXISTING_RESULT,
                             reason_code="IDEMPOTENT_RETRY", checks=checks,
                             ledger_invoked=False, transaction_id=txn)

    order = ["payload_schema", "recalculated_payload_hash", "signature", "draft_version_current",
             "confirmation_valid", "confirmation_matches_draft", "validation", "comparator",
             "risk_policy", "freeze_status", "cancellation_status", "nonce_fresh", "expiry", "origin"]
    for key in order:
        if checks[key] == "FAIL":
            reason = {"recalculated_payload_hash": "SIGNATURE_PAYLOAD_MISMATCH",
                      "signature": "MISSING_VALID_SIGNATURE",
                      "nonce_fresh": "NONCE_ALREADY_USED",
                      "expiry": "PAYLOAD_EXPIRED",
                      "confirmation_valid": "CONFIRMATION_NOT_COMPLETE",
                      "risk_policy": "RISK_POLICY_BLOCKED",
                      "freeze_status": "TRANSACTION_FROZEN"}.get(key, key.upper() + "_FAILED")
            return GatewayResult(decision=GatewayDecision.REJECTED, reason_code=reason,
                                 checks=checks, ledger_invoked=False)

    Ledger._nonces.add(payload["nonce"])
    return GatewayResult(decision=GatewayDecision.ACCEPTED, reason_code="ALL_CHECKS_PASSED",
                         checks=checks, ledger_invoked=True)


def _not_expired(payload: dict) -> bool:
    try:
        expires = datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:                                  # noqa: BLE001
        return False
    return datetime.now(timezone.utc) <= expires


# =====================================================================
