import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .config import Settings, now_iso, money
from .models import (SessionState, Draft, Confirmation, ValidationResult, GuardrailResult,
                      IntentResult, PaymentProposal, FieldCheck, GuardrailDecision, RiskCategory,
                      REQUIRED_VALIDATION_FIELDS)
from .data import CUSTOMERS, SAVED_PAYEES, POLICY
from .parsing import (parse_amount, parse_corrected_amount, last_committed_amount,
                       parse_payee_phrase, parse_reference, detect_language)
from .agents import GuardrailsAgent, IntentClassifierAgent, PaymentAgent, ValidationAgent
from .agent_runtime import AgentUnavailable
from .controls import (resolve_payee_and_account, compare_validation_result, evaluate_risk,
                        evaluate_safety_policy, canonical_payload, payload_hash, sign_payload,
                        Ledger, gateway_verify, GatewayDecision, GatewayResult, ComparatorResult,
                        RiskDecision, RiskOutcome, _not_expired)
from .audit import AUDIT
from .session import STAGES, Session, SESSIONS, get_session

# 10. The orchestrator — a deterministic state machine, not a graph of agents
# =====================================================================
class TurnRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:10]}")
    text: str
    via_voice: bool = True
    asr_confidence: float = 0.96
    # The mock audio-risk adapter's verdict (§6.2). Mocked evidence, never claimed as production.
    voice_risk: Optional[Literal["OK", "SPEAKER_MISMATCH", "SYNTHETIC"]] = None
    # demo-only fault injection so judges can watch independent validation catch a bad draft
    inject: Optional[Literal["PAYMENT_AGENT_AMOUNT_OVERRIDE"]] = None


class TurnResponse(BaseModel):
    session: dict
    events: List[dict]


async def run_turn(req: TurnRequest) -> TurnResponse:
    session = get_session(req.session_id)
    events: List[dict] = []

    def emit(event_type: str, stage: Optional[int], payload: dict) -> None:
        record = AUDIT.append(session.session_id, event_type, payload, stage)
        events.append({"event_type": event_type, "stage": stage, "ts": record["ts"],
                       "payload": payload, "hash": record["hash"]})
        if stage is not None:
            events[-1]["stage_label"] = STAGES[stage][1] if 0 <= stage < len(STAGES) else ""

    # ---------- Stage 0 — authenticated session ----------
    session.state = SessionState.CAPTURED
    emit("SESSION_AUTHENTICATED", 0, {
        "customer_id": session.customer_id,
        "device": "registered_device_simulated",
        "tier_source": "trusted_synthetic_customer_repository",
        "note": "Chat content can never change trusted state (§A.6 C3)."})

    # ---------- Stage 1 — audio-risk admission (mocked, §6.2) ----------
    # A2 (synthetic or replayed audio) never enters the agent pipeline.
    # A1 (another person at the device) is a speaker mismatch: it is admitted with a step-up,
    # and the transaction-specific biometric is what actually stops it.
    if req.via_voice:
        verdict = req.voice_risk
        if verdict is None:
            verdict = "SYNTHETIC" if req.asr_confidence < 0.60 else "OK"
        if verdict == "SYNTHETIC":
            mock_risk = {"result": "REJECTED", "reason_code": "SYNTHETIC_OR_REPLAY_SUSPECTED",
                         "liveness_score": round(req.asr_confidence, 2),
                         "production_capability": False}
        elif verdict == "SPEAKER_MISMATCH":
            mock_risk = {"result": "STEP_UP_REQUIRED", "reason_code": "SPEAKER_MISMATCH",
                         "voiceprint_score": 0.61, "threshold": 0.90,
                         "production_capability": False}
        else:
            mock_risk = {"result": "ADMITTED", "liveness_score": 0.93, "voiceprint_score": 0.94,
                         "threshold": 0.90, "production_capability": False}
        emit("AUDIO_RISK_ADMISSION", 1, mock_risk)
        if mock_risk["result"] == "REJECTED":
            session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "REJECTED"
            emit("AUDIO_ADMISSION_REJECTED", 1, {
                "agent_pipeline_entered": False, "transcript_supplied_to_agents": False,
                "payment_proposal_created": False, "ledger_invoked": False,
                "note": "Demonstrates the trust boundary, not production deepfake detection."})
            return TurnResponse(session=session.to_public(), events=events)
        if mock_risk["result"] == "STEP_UP_REQUIRED":
            session.step_up_required = True
            emit("VOICE_STEP_UP_REQUIRED", 1, {
                "reason_code": "SPEAKER_MISMATCH",
                "next_control": "transaction-specific mock FaceID",
                "note": "None of the four agents establishes identity. Identity evidence is "
                        "produced outside the LLM boundary."})

    # ---------- Stage 2 — immutable evidence commit ----------
    evidence_id = f"ev_{len(session.evidence) + 1:03d}"
    transcript_hash = "hash_" + hashlib.sha256(req.text.encode()).hexdigest()[:32]
    session.evidence.append(req.text)
    session.evidence_ids.append(evidence_id)
    emit("EVIDENCE_COMMITTED", 2, {
        "evidence_id": evidence_id, "transcript_hash": transcript_hash,
        "transcript": req.text, "immutable": True})

    # ---------- Stage 3 — Guardrails Agent ----------
    guardrail, engine = await GuardrailsAgent.run(req.text, {"session": session.to_public()})
    emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                  "prompt_version": Settings.PROMPT_VERSION,
                                  "schema_version": Settings.SCHEMA_VERSION})

    if guardrail.decision == GuardrailDecision.BLOCK:
        # BRD §8.5.7 deterministic routing: BLOCK -> session.state = "FROZEN".
        # The outcome vocabulary keeps the guardrails reason distinct from a validation freeze.
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "BLOCK"
        emit("CONTROL_STOPPED", 3, {
            "decision": guardrail.decision.value, "message_key": guardrail.message_key,
            "blocked_by": "GUARDRAILS_AGENT",
            "intent_classifier_invoked": False, "payment_agent_invoked": False,
            "draft_created": False, "gateway_available_to_agent": False,
            "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)

    if guardrail.decision == GuardrailDecision.SAFETY_INTERVENTION:
        # Stage 10 runs here for the safety path: the consequence is chosen by deterministic
        # policy, not by the model, and not by a severity word in the prompt (§8.10).
        safety = evaluate_safety_policy(guardrail, req.text)
        emit("SAFETY_POLICY_RESULT", 10, {
            **safety.model_dump(), "thresholds_source": "external configuration",
            "faceid_override_permitted": False,
            "note": "FaceID may show the registered device user is present. It cannot establish "
                    "that Auntie is acting freely."})
        session.cooling_off = True
        if safety.decision == RiskDecision.REJECT:
            # BRD Appendix A.7 D1: "State: FROZEN / Cooling-off status: REQUIRED /
            # Payment proposal permitted: NO / Mock FaceID override permitted: NO".
            # The state is FROZEN — the outcome vocabulary keeps the safety reason distinct.
            session.state, session.frozen = SessionState.FROZEN, True
            session.outcome = "SAFETY_INTERVENTION"
            emit("CONTROL_STOPPED", 3, {
                "decision": guardrail.decision.value, "message_key": guardrail.message_key,
                "reasons": safety.reasons,
                "payment_proposal_permitted": False, "draft_created": False,
                "cooling_off_status": "REQUIRED", "faceid_override_permitted": False,
                "ledger_invoked": False})
        else:
            session.state, session.outcome = SessionState.COOLING_OFF, "COOLING_OFF"
            emit("COOLING_OFF_APPLIED", 10, {
                "reasons": safety.reasons,
                "note": "Urgency alone does not prove fraud. Cooling-off is a delay, not an "
                        "accusation; the customer is asked to verify through a number they know.",
                "draft_created": False, "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)

    if guardrail.decision == GuardrailDecision.CLARIFY:
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        emit("CLARIFICATION_OPENED", 3, {"message_key": guardrail.message_key})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 4 — Intent Classifier ----------
    intent, engine = await IntentClassifierAgent.run(req.text, {
        "open_clarification_field": session.open_clarification_field,
        "draft": {"payee_phrase": session.payee_phrase,
                  "amount_minor": session.draft.amount_minor if session.draft else None} if session.draft or session.payee_phrase else None,
    })
    if intent.confidence < Settings.INTENT_CONFIDENCE_THRESHOLD:
        intent = IntentResult(intent="UNCLEAR", confidence=intent.confidence,
                              detected_language=intent.detected_language,
                              reason=f"confidence below configured threshold "
                                     f"{Settings.INTENT_CONFIDENCE_THRESHOLD}")
    emit("INTENT_RESULT", 4, {"engine": engine, **intent.model_dump(),
                              "threshold": Settings.INTENT_CONFIDENCE_THRESHOLD})

    if intent.intent == "CANCEL_OR_STOP":
        session.state, session.cancelled, session.outcome = SessionState.CANCELLED, True, "CANCEL"
        emit("PAYMENT_CANCELLED", 4, {"draft_discarded": bool(session.draft),
                                      "confirmation_invalidated": True,
                                      "signature_invalidated": bool(session._signature),
                                      "ledger_invoked": False})
        session.confirmation = None
        session._signature, session._payload, session._nonce = None, None, None
        return TurnResponse(session=session.to_public(), events=events)
    if intent.intent == "SAFETY_HELP":
        # BRD §8.6.5: invalidate the confirmation and the signature, then freeze.
        previous = session.confirmation.confirmation_id if session.confirmation else None
        session.confirmation = None
        session._signature, session._payload, session._nonce = None, None, None
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "SAFETY_INTERVENTION"
        emit("SAFETY_HELP_ROUTED", 4, {
            "template": "SAFETY_HELP",
            "invalidated_confirmation": previous,
            "signature_invalidated": True,
            "draft_discarded": False,
            "ledger_invoked": False,
            "note": "A customer who asks for scam help is never charged a fee for asking."})
        return TurnResponse(session=session.to_public(), events=events)
    if intent.intent == "UNCLEAR":
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        emit("NO_ACTIVE_CHALLENGE", 4, {
            "active_confirmation_challenge": None, "active_signing_challenge": None,
            "execution_permission": "DENIED", "signature_creatable": False,
            "reason": "A generic or replayed “yes” cannot create a signature."})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- EDIT_PAYMENT invalidates everything downstream (§8.11.3) ----------
    if intent.intent == "EDIT_PAYMENT" and session.draft:
        emit("EDIT_INVALIDATION", 4, {
            "confirmation_invalidated": session.confirmation.confirmation_id if session.confirmation else None,
            "signature_invalidated": bool(session._signature),
            "superseded_draft_version": session.current_draft_version,
            "old_field_confirmations_reused": False})
        session.confirmation = None
        session._signature, session._payload, session._nonce = None, None, None
        session.validation = None
        session.state = SessionState.DRAFTING

    if intent.intent == "PAYMENT_CLARIFICATION":
        session.amount_disambiguated = True
        session.open_clarification_field = None
        # BRD §A.8 E3 — the customer asks for the review in another language.
        # Draft values are untouched; every previous field confirmation is discarded and
        # the whole review restarts in the selected language.
        if session.draft is not None:
            previous = list(session.confirmation.confirmed_fields) if session.confirmation else []
            session.confirmation = None
            session.confirmation_language = intent.detected_language
            session.review_restarted = True
            emit("REVIEW_RESTARTED", 4, {
                "confirmation_language": intent.detected_language,
                "draft_values_changed": False,
                "previous_field_confirmations_retained": False,
                "discarded_field_confirmations": previous,
                "review_restarted": True,
                "note": "The amount was not re-interpreted. Only the presentation language changed."})

    # ---------- Stage 5 — Payment Agent ----------
    ctx = {
        "open_clarification_field": session.open_clarification_field,
        "amount_disambiguated": session.amount_disambiguated,
        "evidence": list(session.evidence),
        "asr_confidence": req.asr_confidence,
        "draft": {"payee_phrase": session.payee_phrase,
                  "amount_minor": session.draft.amount_minor if session.draft else None},
    }
    proposal, engine = await PaymentAgent.run(req.text, ctx)

    # A recipient phrase extracted from committed evidence survives a clarification turn,
    # so the answer only has to supply the missing field.
    if proposal.recipient_phrase:
        session.payee_phrase = proposal.recipient_phrase

    if req.inject == "PAYMENT_AGENT_AMOUNT_OVERRIDE" and proposal.status == "PROPOSAL_READY":
        emit("INJECTED_TEST_CONDITION", 5, {
            "fault": "PAYMENT_AGENT_AMOUNT_OVERRIDE",
            "note": "A faulty proposal is deliberately allowed into draft construction so that "
                    "independent validation can be observed catching it (BRD A.5 B3).",
            "correct_amount_minor": proposal.amount_minor})
        proposal.amount_minor = 5000

    emit("PAYMENT_PROPOSAL", 5, {"engine": engine, **proposal.model_dump(),
                                 "trusted": False,
                                 "note": "Untrusted proposal. No beneficiary id, token or approval."})

    if proposal.status != "PROPOSAL_READY":
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = proposal.clarification_field
        emit("CLARIFICATION_OPENED", 5, {
            "clarification_field": proposal.clarification_field,
            "clarification_question": proposal.clarification_question,
            "canonical_draft": "NOT_CREATED"})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 6 — deterministic payee and account resolution ----------
    session.payee_phrase = (session.payee_phrase or parse_payee_phrase(req.text)
                            or proposal.recipient_phrase)
    resolution = resolve_payee_and_account(session.customer_id, proposal, session.payee_phrase)
    emit("PAYEE_RESOLUTION", 6, {
        "outcome": resolution.outcome, "reason": resolution.reason,
        "candidates": resolution.candidates,
        "llm_generated_beneficiary_id": False,
        "note": "The LLM proposed a phrase. Deterministic code bound the authoritative record."})

    if resolution.outcome == "FREEZE":
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "FREEZE"
        emit("TRANSACTION_FROZEN", 6, {
            "reason": resolution.reason,
            "authoritative_destination_changed": False,
            "confirmation_permitted": False, "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)
    if resolution.outcome == "NEEDS_CLARIFICATION":
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = resolution.clarification_field
        emit("CLARIFICATION_OPENED", 6, {
            "clarification_field": resolution.clarification_field,
            "clarification_question": resolution.clarification_question,
            "candidates": resolution.candidates, "canonical_draft": "NOT_CREATED"})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 7 — canonical draft and schema gate ----------
    session.drafts_created += 1
    session.current_draft_version += 1
    draft = resolution.draft
    draft.draft_id = f"dr_{session.current_draft_version:03d}"
    draft.version = session.current_draft_version
    draft.reference = parse_reference(req.text) or draft.reference
    draft.evidence_ids = list(session.evidence_ids)
    session.draft = draft
    session.draft_versions.append(draft.version)
    session.state = SessionState.DRAFT_READY
    emit("CANONICAL_DRAFT_CREATED", 7, {
        "draft": draft.model_dump(), "schema_validation": "PASS",
        "version": draft.version, "previous_versions_superseded": draft.version > 1})

    # ---------- Stage 8 — Independent Validation Agent ----------
    verified = {"account_masked": draft.account_masked,
                "payee_masked": draft.payee_masked,
                "payee_display": draft.payee_display,
                "transaction_type": draft.transaction_type}
    validation, engine = await ValidationAgent.run(draft, session.evidence, verified)
    session.validation = validation
    emit("VALIDATION_RESULT", 8, {"engine": engine, **validation.model_dump(),
                                  "scope": "independent — no Payment Agent reasoning supplied"})

    # ---------- Stage 9 — deterministic comparator ----------
    comparator = compare_validation_result(validation, draft, session.frozen)
    emit("COMPARATOR_RESULT", 9, {
        **comparator.model_dump(),
        "required_fields": sorted(REQUIRED_VALIDATION_FIELDS),
        "field_check_count": len(validation.field_checks),
        "confirmation_permitted": comparator.result == "PASS"})
    if comparator.result == "FAIL":
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "FREEZE"
        emit("TRANSACTION_FROZEN", 9, {"reason": comparator.reason,
                                       "confirmation_permitted": False, "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 10 — risk and velocity policy ----------
    risk = evaluate_risk(draft, {"drafts_created": session.drafts_created}, guardrail)
    emit("RISK_POLICY_RESULT", 10, {**risk.model_dump(),
                                    "thresholds_source": "external configuration"})
    if risk.decision == RiskDecision.REJECT:
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "REJECT"
        emit("RISK_REJECTED", 10, {"reasons": risk.reasons, "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)
    if risk.decision == RiskDecision.COOLING_OFF:
        session.state, session.cooling_off, session.outcome = SessionState.COOLING_OFF, True, "COOLING_OFF"
        emit("COOLING_OFF_APPLIED", 10, {
            "reasons": risk.reasons,
            "note": "Urgency alone does not prove fraud. Cooling-off is a delay, not an accusation.",
            "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)
    if risk.decision == RiskDecision.STEP_UP:
        session.state, session.outcome = SessionState.CONFIRMATION_READY, "STEP_UP"
        emit("STEP_UP_REQUIRED", 10, {"reasons": risk.reasons})
    elif session.review_restarted:
        # The draft is ready again, but for a different reason than a first-time proposal:
        # the customer asked for the review in another supported language (§A.8 E3).
        session.state, session.outcome = (SessionState.CONFIRMATION_READY,
                                          "PAYMENT_CLARIFICATION")
    else:
        session.state, session.outcome = SessionState.CONFIRMATION_READY, "ALLOW"

    session.confirmation = Confirmation(
        confirmation_id=f"cnf_{session.current_draft_version:03d}",
        draft_id=draft.draft_id, draft_version=draft.version,
        language=session.confirmation_language)
    emit("CONFIRMATION_ELIGIBLE", 10, {
        "confirmation_id": session.confirmation.confirmation_id,
        "draft_id": draft.draft_id, "draft_version": draft.version,
        "fields_to_confirm": ["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
        "note": "One “yes” confirms only the displayed field. It never signs and never calls the gateway."})

    return TurnResponse(session=session.to_public(), events=events)


# =====================================================================
# 11. Stage 11–15 — confirmation, biometric, signing, gateway, ledger
# =====================================================================
class ConfirmRequest(BaseModel):
    session_id: str
    fields: List[Literal["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"]]
    language: Literal["EN", "ZH", "BM"] = "EN"
    # False = stop after the field review, to show that three "yes" still authorise nothing (§A.8 E1)
    biometric: bool = True
    faceid_result: Literal["SUCCESS", "FAILED"] = "SUCCESS"
    # demo-only attack surfaces
    forge: Optional[Literal["NO_SIGNATURE", "TAMPER_AMOUNT", "REPLAY_NONCE", "RETRY_IDEMPOTENT",
                            "EXPIRED"]] = None


async def run_confirmation(req: ConfirmRequest) -> TurnResponse:
    session = get_session(req.session_id)
    events: List[dict] = []

    def emit(event_type: str, stage: Optional[int], payload: dict) -> None:
        record = AUDIT.append(session.session_id, event_type, payload, stage)
        events.append({"event_type": event_type, "stage": stage, "ts": record["ts"],
                       "payload": payload, "hash": record["hash"]})

    if not session.draft or not session.confirmation:
        raise HTTPException(409, "No draft is awaiting confirmation.")
    if session.frozen or session.cancelled:
        raise HTTPException(409, "The transaction is frozen or cancelled.")

    draft, confirmation = session.draft, session.confirmation
    confirmation.language = req.language
    session.confirmation_language = req.language

    # ---------- Stage 11 — field-by-field confirmation ----------
    for field in req.fields:
        if field not in confirmation.confirmed_fields:
            confirmation.confirmed_fields.append(field)
        emit("FIELD_CONFIRMED", 11, {"field": field, "language": req.language,
                                     "draft_version": draft.version,
                                     "transaction_authorised": False})
    missing = [f for f in ("RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT")
               if f not in confirmation.confirmed_fields]
    if missing:
        emit("CONFIRMATION_INCOMPLETE", 11, {"missing_fields": missing,
                                             "transaction_authorised": False})
        return TurnResponse(session=session.to_public(), events=events)
    confirmation.status = "COMPLETE"
    session.state = SessionState.CONFIRMATION_COMPLETE
    session.outcome = "AWAITING_BIOMETRIC"
    emit("CONFIRMATION_COMPLETE", 11, {
        "confirmation_id": confirmation.confirmation_id,
        "confirmed_fields": confirmation.confirmed_fields,
        "all_material_fields_confirmed": True,
        "transaction_authorised": False,
        "note": "Still not authorised. No signature exists yet."})

    if not req.biometric:
        emit("AWAITING_BIOMETRIC", 12, {
            "signature_created": False, "gateway_call": "NOT_PERMITTED",
            "ledger_invoked": False,
            "note": "Three separate “yes” answers confirmed three fields. None of them "
                    "authorised a payment."})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 12 — mock biometric adapter ----------
    emit("MOCK_FACEID", 12, {"result": req.faceid_result, "simulation": True,
                             "confirmation_id": confirmation.confirmation_id,
                             "production_capability": False})
    if req.faceid_result == "FAILED":
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "REJECTED"
        emit("CONTROL_STOPPED", 12, {"reason_code": "BIOMETRIC_NOT_VERIFIED",
                                     "signature_created": False, "gateway_call": "NOT_PERMITTED",
                                     "ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 13 — canonical payload signing ----------
    # Sign once per draft version. The forgery demonstrations must reuse or mutate the real
    # artifact, otherwise they would be proving something about a freshly-signed payload.
    issued_at = datetime.now(timezone.utc)
    if session._payload is None:
        nonce = f"nonce_{uuid.uuid4().hex[:12]}"
        idem = f"idem_{uuid.uuid4().hex[:12]}"
        payload = canonical_payload(draft, confirmation, nonce, idem, issued_at)
        signature = sign_payload(payload)
        session._payload, session._signature = payload, signature
        session._nonce, session._idempotency_key, session._issued_at = nonce, idem, issued_at
        session.state = SessionState.SIGNED
        emit("PAYLOAD_SIGNED", 13, {
            "payload_hash": payload_hash(payload), "signature": signature,
            "mechanism": "HMAC-SHA256 — labelled demonstration mechanism, not a production HSM signature",
            "bound_to_draft_version": draft.version,
            "canonical_payload": payload})
    else:
        payload, signature = session._payload, session._signature
        emit("PAYLOAD_REUSED", 13, {
            "payload_hash": payload_hash(payload),
            "reason": "A previously signed payload is being resubmitted, not re-signed.",
            "nonce": payload["nonce"], "idempotency_key": payload["idempotency_key"]})

    # ---------- Stage 14 — Zero-Trust Gateway ----------
    claimed = {"approved": True, "faceIdPassed": True, "validationPassed": True}
    ledger_request = dict(payload)
    effective_payload = payload

    if req.forge == "NO_SIGNATURE":
        emit("GATEWAY_REQUEST_RECEIVED", 14, {"untrusted_client_claims": claimed})
        emit("UNTRUSTED_APPROVAL_CLAIMS_IGNORED", 14,
             {"ignored": ["approved", "faceIdPassed", "validationPassed"],
              "note": "The gateway trusts nothing it is told."})
        emit("SIGNATURE_MISSING", 14, {})
        gateway = GatewayResult(decision=GatewayDecision.REJECTED,
                                reason_code="MISSING_VALID_SIGNATURE",
                                checks={"signature": "FAIL"}, ledger_invoked=False)
        session.state, session.outcome = SessionState.REJECTED, "REJECTED"
        emit("GATEWAY_DECISION", 14, gateway.model_dump())
        emit("LEDGER_NOT_INVOKED", 14, {"ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)

    if req.forge == "TAMPER_AMOUNT":
        ledger_request = dict(payload, amount_minor=500_000)
        emit("GATEWAY_REQUEST_RECEIVED", 14, {
            "note": "The attacker changed amount_minor and kept the original signature.",
            "signed_amount_minor": payload["amount_minor"],
            "submitted_amount_minor": ledger_request["amount_minor"]})
    elif req.forge == "RETRY_IDEMPOTENT":
        emit("GATEWAY_REQUEST_RECEIVED", 14, {
            "note": "Exact technical retry: same payload, same nonce, same idempotency key.",
            "idempotency_key": payload["idempotency_key"]})
    elif req.forge == "REPLAY_NONCE":
        # a *new* request that reuses a consumed nonce
        ledger_request = dict(payload, idempotency_key=f"idem_{uuid.uuid4().hex[:12]}")
        emit("GATEWAY_REQUEST_RECEIVED", 14, {
            "note": "A new request reusing the already-consumed nonce.",
            "nonce": payload["nonce"], "idempotency_key": ledger_request["idempotency_key"]})
    elif req.forge == "EXPIRED":
        # legitimately signed, then submitted after its expiry
        expired = dict(payload)
        expired["issued_at"] = (issued_at - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expired["expires_at"] = (issued_at - timedelta(hours=2)
                                 + timedelta(seconds=Settings.PAYLOAD_TTL_SECONDS)
                                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        expired["idempotency_key"] = f"idem_{uuid.uuid4().hex[:12]}"
        expired["nonce"] = f"nonce_{uuid.uuid4().hex[:12]}"
        effective_payload = expired
        ledger_request = expired
        signature = sign_payload(expired)      # it really was signed — it is simply stale now
        emit("GATEWAY_REQUEST_RECEIVED", 14, {
            "note": "A validly signed request submitted after its expiry timestamp.",
            "expires_at": expired["expires_at"]})
    else:
        emit("GATEWAY_REQUEST_RECEIVED", 14, {"payload_hash": payload_hash(payload)})

    gateway = gateway_verify(effective_payload, signature, claimed, draft, confirmation,
                             session.validation, compare_validation_result(
                                 session.validation, draft, session.frozen),
                             evaluate_risk(draft, {"drafts_created": session.drafts_created}, None),
                             {"current_draft_version": session.current_draft_version,
                              "frozen": session.frozen, "cancelled": session.cancelled,
                              "gateway_origin_authorised": session.gateway_origin_authorised},
                             request_payload=ledger_request)
    emit("GATEWAY_DECISION", 14, gateway.model_dump())

    if gateway.decision == GatewayDecision.RETURN_EXISTING_RESULT:
        session.execution = {"transaction_id": gateway.transaction_id, "execution_count": 1,
                             "idempotent": True}
        emit("IDEMPOTENT_RETRY", 14, {
            "returned_transaction_id": gateway.transaction_id, "new_ledger_entry": False,
            "note": "An exact technical retry returns the original receipt."})
        return TurnResponse(session=session.to_public(), events=events)

    if gateway.decision == GatewayDecision.REJECTED:
        session.state, session.outcome = SessionState.REJECTED, "REJECTED"
        emit("LEDGER_NOT_INVOKED", 14, {"ledger_invoked": False})
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 15 — restricted mock payment capability ----------
    txn_id, count, created = Ledger.execute(payload["idempotency_key"], ledger_request)
    session.execution = {"transaction_id": txn_id, "status": "RECORDED",
                         "amount_minor": draft.amount_minor, "currency": draft.currency,
                         "execution_count": count, "new_entry": created}
    session.state, session.outcome = SessionState.EXECUTED, "ACCEPTED"
    emit("LEDGER_RECORDED", 15, {**session.execution,
                                 "gateway_authorised_only": True,
                                 "direct_agent_or_browser_calls": "REJECTED"})
    return TurnResponse(session=session.to_public(), events=events)


# =====================================================================
