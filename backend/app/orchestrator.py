import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .config import Settings, money
from .models import (SessionState, Draft, Confirmation, ValidationResult, GuardrailResult,
                      IntentResult, PaymentProposal, FieldCheck, GuardrailDecision, RiskCategory,
                      REQUIRED_VALIDATION_FIELDS)
from .data import POLICY
from .repository import get_repository
from .parsing import (parse_amount, parse_corrected_amount, last_committed_amount,
                       parse_payee_phrase, parse_reference, detect_language, instruction_text,
                       ambiguous_amount_pair, parse_card_phrase, parse_own_account_phrase,
                       parse_schedule, parse_currency, parse_biller_phrase,
                       parse_source_account_phrase, split_instructions, lookup_biller,
                       schedule_from_hint,
                       is_schedule_correction, schedule_named_but_unusable,
                       unknown_payee_phrase, colloquial_scale_phrase,
                       lookup_card, lookup_own_account)
from .agents import (GuardrailsAgent, IntentClassifierAgent, PaymentAgent, ValidationAgent,
                     PersonalizationAgent, CONFIRMATION_BYPASS_RE, LANGUAGE_SWITCH_RE,
                     AFFIRMATION_RE, NEGATION_RE, INSUFFICIENT_FUNDS_RE, SAFETY_HELP_RE,
                     MANDATORY_CONTROL_RE, ROLE_OVERRIDE_RE,
                     EMERGENCY_NOUN_RE, TRANSFER_WORD_RE)
from .agent_runtime import AgentUnavailable
from .llm_client import arm_turn_deadline, disarm_turn_deadline
# The learned-nickname store. Imported here (not via parsing) because the audit event needs to
# attribute the match itself: parsing converts a nickname to its display name before the resolver
# sees it, so only the store can say "the customer said a nickname, and it was this one". `teach`
# is the single write path, and it refuses any beneficiary that is not already a saved payee.
from .nicknames import match_in_text as nickname_match, teach as teach_nickname
from .controls import (resolve_payee_and_account, compare_validation_result, evaluate_risk,
                        evaluate_safety_policy, canonical_payload, payload_hash, sign_payload,
                        Ledger, gateway_verify, GatewayDecision, GatewayResult, ComparatorResult,
                        RiskDecision, RiskOutcome, _not_expired, freeze_message_key,
                        detect_multiple_recipients, DeterministicResolution)
from .messages import (say, interface_language, candidate_options, money,
                        customer_name, display_phrase, join_options,
                        readback_parts, source_account_phrase, multi_item, readback_multi,
                        readback_multi_lines, multi_first_label, colloquial_confirm,
                        _part)
from .audit import AUDIT
from .session import STAGES, Session, SESSIONS, get_session

# 10. The orchestrator, a deterministic state machine, not a graph of agents
# =====================================================================
async def evaluate_personalization(session: Session, emit) -> None:
    """Recompute the Personalization Agent's decision from this session's current signals
    and only emit an audit event when the decision actually changed, every other turn just
    keeps session.personalize_auto in sync silently."""
    signals = session.compute_personalization_signals()
    decision, engine = await PersonalizationAgent.run(signals)
    changed = session.personalization_decision is None or (
        session.personalization_decision.get("personalize") != decision.personalize
        or session.personalization_decision.get("score") != decision.score)
    session.personalize_auto = decision.personalize
    session.personalization_signals = signals.model_dump()
    session.personalization_decision = decision.model_dump()
    if changed:
        emit("PERSONALIZATION_RESULT", None, {"engine": engine, **decision.model_dump(),
                                              "signals": signals.model_dump()})


class TurnRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:10]}")
    text: str
    via_voice: bool = True
    # 0.96 is a PLACEHOLDER for "the engine reported nothing", not a measurement. It is only
    # ever used when asr_confidence_source == "unreported"; anything that reads this number
    # must read the source alongside it.
    asr_confidence: float = 0.96
    # Where that number came from. "engine_reported" = the STT vendor gave a real score;
    # "engine_stability" = derived from the vendor's per-word stable_flag;
    # "unreported" = nothing was available and the placeholder above is in force.
    asr_confidence_source: Literal["engine_reported", "engine_stability",
                                   "unreported", "typed"] = "unreported"
    # The mock audio-risk adapter's verdict (§6.2). Mocked evidence, never claimed as production.
    voice_risk: Optional[Literal["OK", "SPEAKER_MISMATCH", "SYNTHETIC"]] = None
    # demo-only fault injection so judges can watch independent validation catch a bad draft
    inject: Optional[Literal["PAYMENT_AGENT_AMOUNT_OVERRIDE"]] = None


class TurnResponse(BaseModel):
    session: dict
    events: List[dict]


def _say_payment_clarification(req: "TurnRequest", session: Session, proposal: PaymentProposal,
                               say_line) -> None:
    """Render the Stage-5 clarification question from the field the Payment Agent flagged.

    The agent's own ``clarification_question`` is an untrusted proposal, it stays in the audit
    payload but never reaches the customer. The deterministic layer picks the catalogue key from
    ``clarification_field``, and for an ambiguous amount it recomputes the two readings with the
    same predicate the agent used, so the question cannot contradict the reason it was asked.
    """
    if proposal.clarification_field == "AMOUNT":
        ambiguous = ambiguous_amount_pair(req.text, req.asr_confidence,
                                          session.amount_disambiguated)
        if ambiguous is not None:
            option_a, option_b = money(ambiguous[0]), money(ambiguous[1])
            say_line("AMOUNT_AMBIGUOUS", 5, option_a=option_a, option_b=option_b)
            return
        say_line("AMOUNT_MISSING", 5, phrase=proposal.recipient_phrase or "?")
        return
    say_line("RECIPIENT_MISSING", 5)


# =====================================================================
# 9a. Nicknames, the ask / confirm / teach turns
# =====================================================================
# A nickname is a REDIRECT RULE: "ah girl" points at a beneficiary the customer already holds. The
# three helpers below serve the three turns of that conversation, and none of them can invent a
# destination, every candidate comes from SAVED_PAYEES, and the store itself refuses to bind
# anything else (see app/nicknames.py).
def _ending(masked: str) -> str:
    """The digits of a masked destination, for a sentence that already says "account ending".

    The trusted record stores "ending 7788"; a template reading "account ending {masked}" must be
    handed "7788", or the customer hears "account ending ending 7788".
    """
    digits = re.sub(r"\D", "", masked or "")
    return digits or (masked or "").replace("ending", "").strip()


def _payee_by_id(customer_id: str, beneficiary_id: str):
    return next((p for p in get_repository().get_saved_payees(customer_id)
                 if p.beneficiary_id == beneficiary_id), None)


def _payees_named_in(text: str, customer_id: str) -> List:
    """The saved payees the customer named in an ANSWER, by full name, first name, or a nickname.

    Used only to read the reply to "who is Ah Girl?", never to bind a destination on its own. An
    answer naming two payees stays a question; an answer naming none binds nothing.

    The three readings are tried in PRECEDENCE, not together: a full name settles the question
    outright, so "john doe" names John Doe and must not also drag in John Smith merely because they
    share a first name. Only when no full name appears does a first name get a say, which is what
    an elderly customer usually says ("mei"), and which is exactly why two Johns must stay a
    question rather than being guessed at.
    """
    lowered = (text or "").lower()
    payees = get_repository().get_saved_payees(customer_id)

    def named(phrase: str) -> bool:
        return bool(re.search(rf"\b{re.escape(phrase.lower())}\b", lowered))

    full = [p for p in payees if named(p.display_name)]
    if full:
        return full

    nickname_hit = nickname_match(customer_id, text)
    if nickname_hit:
        return [nickname_hit[1]]

    return [p for p in payees if named(p.display_name.split()[0])]


def _saved_payee_options(customer_id: str) -> List[dict]:
    """The customer's saved payees, in the shape candidate_options() renders.

    Deliberately the same shape the duplicate-payee question uses, so "which one do you mean?" is
    worded by one function whether the ambiguity came from two identical names or from a name Joy
    has never heard before.
    """
    return [{"beneficiary_id": p.beneficiary_id, "displayName": p.display_name,
             "maskedDestination": p.masked} for p in get_repository().get_saved_payees(customer_id)]


async def run_turn(req: TurnRequest) -> TurnResponse:
    """Entry point for one committed customer turn.

    Its only job beyond delegating is to arm the turn's live-model budget, so that a slow or
    stalled upstream can never freeze the demo: once the budget is spent, the remaining agents
    in this turn fail closed to their deterministic mocks (see llm_client.arm_turn_deadline).
    The turn always completes, and always with a decision the deterministic chain owns.
    """
    arm_turn_deadline(Settings.TURN_DEADLINE_S)
    try:
        return await _run_turn(req)
    finally:
        disarm_turn_deadline()


async def _run_turn(req: TurnRequest) -> TurnResponse:
    session = get_session(req.session_id)
    events: List[dict] = []
    # A previous turn's read-back may have been holding the confirm card back while it asked a
    # question. This turn IS the answer (or a new instruction), so the card is released, whatever
    # the customer says next, they should be able to act on the payment by then.
    session.awaiting_multi_go_ahead = False

    def emit(event_type: str, stage: Optional[int], payload: dict) -> None:
        record = AUDIT.append(session.session_id, event_type, payload, stage)
        events.append({"event_type": event_type, "stage": stage, "ts": record["ts"],
                       "payload": payload, "hash": record["hash"]})
        if stage is not None:
            events[-1]["stage_label"] = STAGES[stage][1] if 0 <= stage < len(STAGES) else ""

    async def finish() -> TurnResponse:
        await evaluate_personalization(session, emit)
        return TurnResponse(session=session.to_public(), events=events)

    # The language every customer-facing line this turn is spoken in. Decided once, from the
    # customer's own words, and used for every say() below, so a code-mixed turn is answered
    # consistently instead of each stage picking its own language.
    #
    # It then STICKS for the rest of the conversation. A first turn that is mostly English gets an
    # English answer, and the short replies that follow ("是", "ya", "ok") must not flip Auntie
    # into another language mid-conversation, that reads as the assistant changing its mind, not
    # as the customer being served. Only an explicit request to switch moves it.
    turn_lang = interface_language(req.text)
    if session.interface_language and not LANGUAGE_SWITCH_RE.search(req.text):
        lang = session.interface_language
    else:
        lang = turn_lang
    session.interface_language = lang

    def say_line(key: str, stage: Optional[int], **params) -> None:
        say(emit, key, stage, session, lang, **params)

    # ---------- Stage 0: authenticated session ----------
    session.state = SessionState.CAPTURED
    emit("SESSION_AUTHENTICATED", 0, {
        "customer_id": session.customer_id,
        "device": "registered_device_simulated",
        "tier_source": "trusted_synthetic_customer_repository",
        "note": "Chat content can never change trusted state (§A.6 C3)."})

    # ---------- Stage 1: audio-risk admission (mocked, §6.2) ----------
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
            say_line("AUDIO_REJECTED", 1)
            return await finish()
        if mock_risk["result"] == "STEP_UP_REQUIRED":
            session.step_up_required = True
            session.step_up_reason = "SPEAKER_MISMATCH"
            emit("VOICE_STEP_UP_REQUIRED", 1, {
                "reason_code": "SPEAKER_MISMATCH",
                "next_control": "transaction-specific mock FaceID",
                "note": "None of the four agents establishes identity. Identity evidence is "
                        "produced outside the LLM boundary."})
            say_line("VOICE_STEP_UP", 1)

    # ---------- Stage 2: immutable evidence commit ----------
    evidence_id = f"ev_{len(session.evidence) + 1:03d}"
    transcript_hash = "hash_" + hashlib.sha256(req.text.encode()).hexdigest()[:32]
    session.evidence.append(req.text)
    session.evidence_ids.append(evidence_id)
    emit("EVIDENCE_COMMITTED", 2, {
        "evidence_id": evidence_id, "transcript_hash": transcript_hash,
        "transcript": req.text, "immutable": True})

    # ---------- Stage 3: Guardrails Agent ----------
    guardrail, engine = await GuardrailsAgent.run(req.text, {"session": session.to_public()})
    emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                  "prompt_version": Settings.PROMPT_VERSION,
                                  "schema_version": Settings.SCHEMA_VERSION})

    # Deterministic policy (IN-08): a correction of the DATE on screen. Decided here, before the
    # model is asked anything, and split in two because the halves need opposite treatment:
    #   * a correction that resolves becomes an edit of the payment on screen, and
    #   * one that does NOT resolve must be ASKED about, never silently ignored, and must not
    #     invalidate the payment the customer is still looking at.
    # Only a turn that changes the date and nothing else counts (see is_schedule_correction), so an
    # amount or payee correction is untouched by this and still takes the ordinary edit path.
    # Computed ABOVE the guardrails gates on purpose: the UNSUPPORTED_ACTION relaxer below needs the
    # answer to undo an over-trigger, and a relaxer only works if it sits above the gate it undoes.
    # Both values are pure functions of the turn and the session, so hoisting them changes nothing.
    date_edit_attempt = (session.draft is not None and not session.frozen
                         and not session.cancelled and is_schedule_correction(req.text))
    date_correction = date_edit_attempt and parse_schedule(req.text) is not None

    # Deterministic safeguard (mirrors GuardrailsAgent.mock): a BLOCK/CONFIRMATION_BYPASS that does
    # NOT actually contain explicit skip / ignore / bypass language is a false positive, the live
    # model over-triggers on words like "confirm". Relaxing it to ALLOW means an ordinary confirmation
    # (e.g. a replayed "yes") can never be frozen by a misclassification, while a genuine bypass that
    # contains the explicit tokens still BLOCKs. A true bypass always carries the tokens, so this can
    # only ever relax a false positive; it never weakens the control.
    if (guardrail.decision == GuardrailDecision.BLOCK
            and guardrail.risk_category == RiskCategory.CONFIRMATION_BYPASS
            and not CONFIRMATION_BYPASS_RE.search(req.text)):
        guardrail = GuardrailResult(
            decision=GuardrailDecision.ALLOW, risk_category=RiskCategory.SAFE_PAYMENT_REQUEST,
            risk_level="LOW", detected_language=guardrail.detected_language,
            message_key="CONTINUE",
            developer_reason="CONFIRMATION_BYPASS relaxed to ALLOW: no explicit skip/ignore/bypass "
                             "language found (deterministic safeguard).")
        engine = "deterministic-safeguard"
        emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                      "prompt_version": Settings.PROMPT_VERSION,
                                      "schema_version": Settings.SCHEMA_VERSION,
                                      "note": "live CONFIRMATION_BYPASS relaxed to ALLOW by "
                                              "deterministic safeguard"})

    # Deterministic safeguard (E3): a request to present the current review in another language
    # (e.g. "可以用中文吗？我看不清楚。") is never a security threat and MUST reach the Intent
    # classifier, where the deterministic language-switch safeguard routes it to PAYMENT_CLARIFICATION.
    # The live model can mis-classify it as CLARIFY (unusable) or BLOCK, which would short-circuit at
    # the guardrails early-return and never let Intent run, so relax BLOCK/CLARIFY to ALLOW here.
    # (LANGUAGE_SWITCH_RE only matches E3's turn 2 across all 19 scenarios, so this is safely scoped.)
    if LANGUAGE_SWITCH_RE.search(req.text) and guardrail.decision in (
            GuardrailDecision.BLOCK, GuardrailDecision.CLARIFY):
        guardrail = GuardrailResult(
            decision=GuardrailDecision.ALLOW, risk_category=RiskCategory.SAFE_PAYMENT_REQUEST,
            risk_level="LOW", detected_language=guardrail.detected_language,
            message_key="CONTINUE",
            developer_reason="Language-switch request relaxed to ALLOW by deterministic safeguard "
                             "(must reach Intent stage).")
        engine = "deterministic-safeguard"
        emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                      "prompt_version": Settings.PROMPT_VERSION,
                                      "schema_version": Settings.SCHEMA_VERSION,
                                      "note": "live guardrails relaxed to ALLOW for language-switch "
                                              "by deterministic safeguard"})

    # Deterministic safeguard (UNUSABLE_INPUT over-trigger): the Guardrails prompt allows CLARIFY /
    # UNUSABLE_INPUT ONLY for a turn that is materially unusable, under 2 characters after
    # stripping, or pure punctuation. The live model over-triggers it anyway: "on 28th september
    # instead" is 24 usable characters, a plain correction of the date on screen, and it came back
    # CLARIFY, so the customer was told "I didn't quite catch that" and could never change the
    # date. This can only ever UNDO an over-trigger, never create one: a genuinely unusable turn
    # still has nothing to strip, so it keeps its CLARIFY. Mirrors the mock's own rule
    # (IntentClassifierAgent.mock / GuardrailsAgent.mock both gate on len(turn.strip()) < 2).
    if (guardrail.decision == GuardrailDecision.CLARIFY
            and guardrail.risk_category == RiskCategory.UNUSABLE_INPUT
            and len(req.text.strip()) >= 2):
        guardrail = GuardrailResult(
            decision=GuardrailDecision.ALLOW, risk_category=RiskCategory.SAFE_PAYMENT_REQUEST,
            risk_level="LOW", detected_language=guardrail.detected_language,
            message_key="CONTINUE",
            developer_reason="UNUSABLE_INPUT relaxed to ALLOW by deterministic safeguard "
                             "(the turn is usable text).")
        engine = "deterministic-safeguard"
        emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                      "prompt_version": Settings.PROMPT_VERSION,
                                      "schema_version": Settings.SCHEMA_VERSION,
                                      "note": "live UNUSABLE_INPUT relaxed to ALLOW, the turn is "
                                              "usable text, so the over-trigger is undone"})

    # Deterministic safeguard (date-correction over-trigger): the live Guardrails model reads the bare
    # verb "change" as rule 5, ask to change a mandatory control, so "change to this monday instead"
    # came back BLOCK / UNSUPPORTED_ACTION and Auntie answered "I can't change that safety setting.
    # It stays on to protect you." on a 26-character DATE correction, freezing the session. The mock is
    # narrower: it requires the verb AND a nearby control noun, which is why the same turn is ALLOW in
    # mock mode and the date correction works there. This relaxes the live verdict back to the mock's,
    # and ONLY for a turn that is a date-only correction of the payment on screen.
    # It can never weaken the control:
    #   * a genuine control change names the control ("change my transfer limit to 5000"), so
    #     MANDATORY_CONTROL_RE matches and the block stands, verified on all four control phrasings;
    #   * it carries no date, so date_correction is False anyway and the relaxer does not fire;
    #   * the turn must also have an open, unfrozen draft to correct, so it cannot act on a first turn;
    #   * INSTRUCTION_OVERRIDE is included because the same over-trigger happens on a denial word
    #     ("no, change it to friday"), but SCAM / coercion is a SAFETY_INTERVENTION, a different
    #     decision, and is deliberately left untouched.
    if (guardrail.decision == GuardrailDecision.BLOCK
            and guardrail.risk_category in (RiskCategory.UNSUPPORTED_ACTION,
                                            RiskCategory.INSTRUCTION_OVERRIDE)
            and date_correction
            and not MANDATORY_CONTROL_RE.search(req.text)):
        guardrail = GuardrailResult(
            decision=GuardrailDecision.ALLOW, risk_category=RiskCategory.SAFE_PAYMENT_REQUEST,
            risk_level="LOW", detected_language=guardrail.detected_language,
            message_key="CONTINUE",
            developer_reason="UNSUPPORTED_ACTION relaxed to ALLOW by deterministic safeguard "
                             "(the turn is a date-only correction, not a control change).")
        engine = "deterministic-safeguard"
        emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                      "prompt_version": Settings.PROMPT_VERSION,
                                      "schema_version": Settings.SCHEMA_VERSION,
                                      "note": "live guardrails relaxed to ALLOW, the turn only "
                                              "changes WHEN the payment runs, so the over-trigger "
                                              "is undone"})

    # Deterministic safeguard (affordability / insufficient funds, branch H): a customer who
    # volunteers they cannot cover the amount (e.g. "I don't have enough", "tak cukup duit",
    # "钱不够") gets an empathetic, non-pressuring response. The conversation stays open so they
    # can name a smaller amount or cancel, no draft is ever created from an affordability
    # statement alone. This is a deterministic policy, so it fires regardless of what the
    # (untrusted) Guardrails Agent might have classified the turn as.
    if INSUFFICIENT_FUNDS_RE.search(req.text):
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "INSUFFICIENT_FUNDS"
        emit("INSUFFICIENT_FUNDS_DETECTED", 3, {
            "customer_stated_affordability": True,
            "draft_created": False, "payment_proposal_permitted": False,
            "gateway_available_to_agent": False, "ledger_invoked": False,
            "note": "Empathetic abort, the system never pressures the customer to proceed."})
        say_line("INSUFFICIENT_FUNDS", 3)
        return await finish()

    # Deterministic ESCALATION (system-message injection): a turn that tries to change who
    # AuntieJoy is, or whose rules she follows, is never a payment request, whatever else it says.
    # The live Guardrails model can read such a turn as an ordinary instruction and return ALLOW,
    # which is exactly how an injection succeeds, so the deterministic layer must be able to
    # TIGHTEN, not only relax. This is the LAST thing computed before the gate below and therefore
    # sits BELOW every relaxer, so no relaxer can undo it; and it fires only on a turn carrying the
    # cue, so an ordinary turn is untouched. A turn the model already judged BLOCK keeps that
    # BLOCK, this never downgrades a verdict.
    #
    # SAFETY_INTERVENTION needed a decision, and it is the one case where the customer outranks
    # the machine. The live model can read an injection as coercion ("ignore all rules … transfer
    # S$10,000 sekarang" has both shapes in it) and answer SAFETY_INTERVENTION, which used to leave
    # the demo saying the scam line for a turn that was attacking the assistant, with an audit
    # reason that named the wrong thing. Overriding it blindly would be just as wrong the other
    # way: BLOCK_INJECTION is a refusal, and a refusal that ends by offering to send the money
    # anyway reads as an invitation to somebody who has just tried to hijack the assistant. So the
    # two are separated by the same cue the mock scam rule already uses:
    #   * the turn names an EMERGENCY next to a transfer -> a person is under pressure, the scam
    #     path below keeps it, only the audit records that the turn also carried an injection;
    #   * anything else -> the model over-triggered on urgency, escalate to BLOCK as before.
    if (ROLE_OVERRIDE_RE.search(req.text)
            and guardrail.decision == GuardrailDecision.SAFETY_INTERVENTION
            and EMERGENCY_NOUN_RE.search(req.text)
            and TRANSFER_WORD_RE.search(req.text)):
        emit("INJECTION_CUE_ON_SAFETY_PATH", 3, {
            "risk_category": guardrail.risk_category.value if guardrail.risk_category else None,
            "message_key": guardrail.message_key,
            "emergency_named": True,
            "note": "The turn carries a system-message injection AND a coercion story that names "
                    "an emergency. The customer is the one under attack, so the scam warning "
                    "still decides what is said; the injection is recorded here rather than "
                    "silently dropped, and the payment is stopped either way."})
    elif (ROLE_OVERRIDE_RE.search(req.text)
            and guardrail.decision in (GuardrailDecision.ALLOW, GuardrailDecision.CLARIFY,
                                       GuardrailDecision.SAFETY_INTERVENTION)):
        previous_decision = guardrail.decision.value
        previous_category = (guardrail.risk_category.value if guardrail.risk_category else "none")
        guardrail = GuardrailResult(
            decision=GuardrailDecision.BLOCK, risk_category=RiskCategory.INSTRUCTION_OVERRIDE,
            risk_level="HIGH", detected_language=guardrail.detected_language,
            message_key="BLOCK_INJECTION",
            developer_reason="System-message injection escalated by deterministic control: the "
                             "turn attempts to change the assistant's role or its rules, so no "
                             "payment field is read and nothing reaches the payment agent.")
        engine = "deterministic-safeguard"
        emit("GUARDRAILS_RESULT", 3, {"engine": engine, **guardrail.model_dump(),
                                      "prompt_version": Settings.PROMPT_VERSION,
                                      "schema_version": Settings.SCHEMA_VERSION,
                                      "note": "escalated to BLOCK by deterministic control, the "
                                              "verdict reaching the gate was " + previous_decision
                                              + "/" + previous_category
                                              + " on a turn carrying a system-message injection"})

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
        # The agent returned a code; the deterministic layer decides how it is said.
        say_line(guardrail.message_key, 3)
        return await finish()

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
            # The state is FROZEN, the outcome vocabulary keeps the safety reason distinct.
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
        # A scam pause and a cooling-off period are different consequences, so they get
        # different words, all from the catalogue, never from the model's own phrasing.
        if safety.decision == RiskDecision.REJECT:
            say_line("PAUSE_SCAM", 10)
        elif guardrail.risk_category == RiskCategory.SCAM_OR_COERCION_SIGNAL:
            # A coercion signal gets the cooling-off wording that tells the customer to verify
            # the story with a number they already know, not the generic "something looks odd".
            say_line("COOLING_OFF_SCAM", 10)
        else:
            say_line("COOLING_OFF", 10)
        return await finish()

    if guardrail.decision == GuardrailDecision.CLARIFY:
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.note_clarification("GUARDRAILS_CLARIFY")
        emit("CLARIFICATION_OPENED", 3, {"message_key": guardrail.message_key})
        say_line(guardrail.message_key, 3)
        return await finish()

    # (date_edit_attempt / date_correction were computed above the guardrails gates, see stage 3.)
    if date_edit_attempt and not date_correction:
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = "SCHEDULE"
        session.note_clarification("SCHEDULE_UNCLEAR")
        emit("CLARIFICATION_OPENED", 4, {
            "clarification_field": "SCHEDULE", "clarification_key": "SCHEDULE_UNCLEAR",
            "draft_preserved": True,
            "note": "The customer asked to change the date but the date could not be read. "
                    "Nothing was changed, the payment on screen is untouched."})
        say_line("SCHEDULE_UNCLEAR", 4)
        return await finish()

    # ---------- Stage 4: Intent Classifier ----------
    intent, engine = await IntentClassifierAgent.run(req.text, {
        "open_clarification_field": session.open_clarification_field,
        "customer_id": session.customer_id,
        "draft": {"payee_phrase": session.payee_phrase,
                  "amount_minor": session.draft.amount_minor if session.draft else None} if session.draft or session.payee_phrase else None,
    })
    if intent.confidence < Settings.INTENT_CONFIDENCE_THRESHOLD:
        intent = IntentResult(intent="UNCLEAR", confidence=intent.confidence,
                              detected_language=intent.detected_language,
                              reason=f"confidence below configured threshold "
                                     f"{Settings.INTENT_CONFIDENCE_THRESHOLD}")

    # Deterministic intent safeguards, these mirror IntentClassifierAgent.mock's reference behaviour.
    # The live model is non-deterministic, so on these two security-sensitive ambiguities the
    # deterministic control chain (not the model) decides the route, exactly as the mock does.
    text_l = req.text.lower()
    if session.draft is not None and LANGUAGE_SWITCH_RE.search(req.text):
        # E3, the customer asks to SEE the current review in another language. A draft exists; so this
        # is a presentation switch, not a new payment and not a field correction.
        intent = IntentResult(intent="PAYMENT_CLARIFICATION", confidence=0.95,
                              detected_language=detect_language(req.text),
                              reason="deterministic language-switch safeguard")
        engine = "deterministic-safeguard"
    elif (session.draft is None and session.open_clarification_field is None
          and AFFIRMATION_RE.match(text_l)
          and not re.search(r"\b(send|transfer|pay|hantar|bayar|kepada|buat|给|帮我|\d+)\b", text_l)):
        # A3, a bare "yes / confirm" with no active draft and no open question: a replayed or stray
        # confirmation that must never mint a signature (BRD A.3 / NO_ACTIVE_CHALLENGE).
        intent = IntentResult(intent="UNCLEAR", confidence=0.95,
                              detected_language=detect_language(req.text),
                              reason="deterministic replayed-yes safeguard")
        engine = "deterministic-safeguard"
    elif date_correction and intent.intent not in ("CANCEL_OR_STOP", "SAFETY_HELP"):
        # IN-08, "on 28th september instead" after a read-back that said 05 October. The live
        # model reads this as a fresh payment (or as nothing at all), which re-derives the amount
        # from the only number in the sentence and drops the date. It is an EDIT of the payment
        # already on screen, and the deterministic chain is what says so.
        #
        # Never over a stop or a safety report: "cancel it, do it tomorrow" is a cancel, and a
        # date correction must not be able to talk the customer out of one.
        intent = IntentResult(intent="EDIT_PAYMENT", confidence=0.95,
                              detected_language=detect_language(req.text),
                              reason="deterministic date-correction safeguard "
                                     "(the turn only changes WHEN the payment runs)")
        engine = "deterministic-safeguard"
    elif (intent.intent == "UNCLEAR" and session.open_clarification_field == "SCHEDULE"
          and parse_schedule(req.text)):
        # The customer is answering "which day did you mean?" with a date. The recipient and the
        # amount are recovered from the committed evidence by the Payment Agent, so this is a
        # payment turn. Mirrors IntentClassifierAgent.mock, so a live model cannot leave the
        # customer's answer stranded as UNCLEAR, the question must be answerable.
        intent = IntentResult(intent="PAYMENT", confidence=0.95,
                              detected_language=detect_language(req.text),
                              reason="deterministic open-SCHEDULE safeguard "
                                     "(the customer is answering the date question)")
        engine = "deterministic-safeguard"

    emit("INTENT_RESULT", 4, {"engine": engine, **intent.model_dump(),
                              "threshold": Settings.INTENT_CONFIDENCE_THRESHOLD})
    session.note_intent(intent.intent)   # feeds the Personalization Agent's stalled_intent signal

    # Deterministic safeguard (safety-help false positive, IN-02): a turn that merely opens
    # with "help me" to request a payment (e.g. Singlish "Help me send fifty dollar to Ah Girl
    # from savings, can?") must never freeze the session. The live model can mis-read "help me"
    # as a scam report; this relaxes such a turn to PAYMENT. A genuine scam/help turn still
    # carries the tokens in SAFETY_HELP_RE, so this can only ever undo an over-trigger; it
    # never weakens the control. Mirrors the mock IntentClassifier exactly.
    if intent.intent == "SAFETY_HELP" and not SAFETY_HELP_RE.search(req.text):
        intent = IntentResult(intent="PAYMENT", confidence=0.90,
                              detected_language=detect_language(req.text),
                              reason="SAFETY_HELP relaxed to PAYMENT by deterministic safeguard "
                                     "(no genuine scam/help tokens found).")
        engine = "deterministic-safeguard"
        emit("INTENT_RESULT", 4, {"engine": engine, **intent.model_dump(),
                                  "threshold": Settings.INTENT_CONFIDENCE_THRESHOLD,
                                  "note": "live SAFETY_HELP relaxed to PAYMENT by deterministic safeguard"})

    # Deterministic safeguard (IN-07 / IN-05): a turn that names the customer's own credit card
    # or own internal account as the destination is unambiguously a PAYMENT intent. The live
    # model may not know these transaction shapes and could return UNCLEAR; relax it to PAYMENT
    # so it reaches the Payment Agent, which resolves the shape deterministically. Mirrors the
    # other intent safeguards, it can only ever undo an over-trigger, never weaken a control.
    if intent.intent in ("UNCLEAR", "PAYMENT_CLARIFICATION") and session.draft is None and (
            parse_card_phrase(req.text, session.customer_id)
            or parse_own_account_phrase(req.text, session.customer_id)):
        intent = IntentResult(intent="PAYMENT", confidence=0.95,
                              detected_language=detect_language(req.text),
                              reason="card/own-account destination relaxed to PAYMENT by "
                                     "deterministic safeguard (transaction shape is a payment).")
        engine = "deterministic-safeguard"
        emit("INTENT_RESULT", 4, {"engine": engine, **intent.model_dump(),
                                  "threshold": Settings.INTENT_CONFIDENCE_THRESHOLD,
                                  "note": "live UNCLEAR relaxed to PAYMENT for card/own-account "
                                          "by deterministic safeguard"})

    # ---------- The nickname question, once it is open ----------
    # Two turns, both handled here rather than by the agents, because neither turn is a payment:
    #   1. the customer answers WHO the name means  -> bind it, then ask to remember it;
    #   2. the customer answers whether to REMEMBER -> teach it, then let the payment proceed.
    # The second turn deliberately falls back INTO the normal pipeline once the name is taught: from
    # then on "ah girl" is an ordinary nickname, so the Payment Agent reads it as the person it was
    # bound to and recovers the amount from the committed evidence. Teaching first and then reusing
    # the pipeline is what stops this becoming a second, divergent way to pay somebody.
    #
    # Placed after every intent relaxer so the intent is fully settled, and guarded against a stop or
    # a safety report, "cancel" and "help me, this is a scam" must never be read as an answer to a
    # nickname question.
    if (session.open_clarification_field == "PAYEE_NICKNAME" and session.pending_nickname
            and intent.intent not in ("CANCEL_OR_STOP", "SAFETY_HELP")):
        pending = session.pending_nickname
        nickname = pending["nickname"]

        if not pending.get("beneficiary_id"):
            # Turn 1, who does the customer mean?
            matches = _payees_named_in(req.text, session.customer_id)
            if len(matches) == 1:
                payee = matches[0]
                session.pending_nickname = {**pending, "beneficiary_id": payee.beneficiary_id}
                emit("NICKNAME_ANSWERED", 4, {
                    "nickname": nickname,
                    "beneficiary_id": payee.beneficiary_id,
                    "display_name": payee.display_name,
                    "bound": False,
                    "note": "The customer said who the name means. Nothing is written down yet, "
                            "the binding is confirmed first."})
                say_line("NICKNAME_CONFIRM", 4, nickname=display_phrase(nickname),
                         payee=payee.display_name, masked=_ending(payee.masked))
                return await finish()
            if len(matches) > 1:
                options = [{"beneficiary_id": p.beneficiary_id, "displayName": p.display_name,
                            "maskedDestination": p.masked} for p in matches]
                # Name the word that actually collided, not the nickname. The customer said "john"
                # and two saved payees share it, so "2 saved payees called John" is the question,
                # "2 saved payees called Ah Girl" would name a payee that does not exist. The two
                # matches always share the token they were found by (this branch is only reachable
                # from the first-name reading), so the first match supplies it.
                collided = display_phrase(matches[0].display_name.split()[0])
                session.note_clarification("NICKNAME_AMBIGUOUS")
                emit("NICKNAME_UNRESOLVED", 4, {
                    "nickname": nickname, "reason": "MULTIPLE_PAYEES_NAMED",
                    "matched_on": collided, "candidates": options, "bound": False,
                    "note": "The answer named more than one saved payee, so it binds nothing."})
                say_line("PAYEE_AMBIGUOUS", 4, count=len(matches),
                         phrase=collided, options=candidate_options(options, lang))
                return await finish()
            session.note_clarification("NICKNAME_UNKNOWN")
            emit("NICKNAME_UNRESOLVED", 4, {
                "nickname": nickname, "reason": "NO_SAVED_PAYEE_NAMED", "bound": False,
                "note": "The answer named nobody on the saved-payee list. No binding was created."})
            say_line("PAYEE_NOT_FOUND", 4, phrase=display_phrase(nickname))
            return await finish()

        # Turn 2, shall Joy remember it?
        payee = _payee_by_id(session.customer_id, pending["beneficiary_id"])
        if payee is None:
            # The payee was removed while the question was open. Drop the binding rather than
            # resurrect it, a nickname may only ever point at a payee that still exists.
            session.pending_nickname, session.open_clarification_field = None, None
            emit("NICKNAME_UNRESOLVED", 4, {
                "nickname": nickname, "reason": "PAYEE_NO_LONGER_SAVED", "bound": False})
            say_line("PAYEE_NOT_FOUND", 4, phrase=display_phrase(nickname))
            return await finish()

        if AFFIRMATION_RE.match(req.text.strip().lower()):
            taught = teach_nickname(session.customer_id, nickname, payee.beneficiary_id)
            session.pending_nickname, session.open_clarification_field = None, None
            if taught is not None:
                session.nickname_used = nickname
            emit("NICKNAME_LEARNED", 4, {
                "nickname": nickname,
                "beneficiary_id": payee.beneficiary_id,
                "display_name": payee.display_name,
                "confirmed_by_customer": True,
                "stored": taught is not None,
                "destination_from": "trusted saved-payee repository",
                "note": "The customer confirmed the binding, so it was written to the nickname "
                        "store. That store holds only a beneficiary id, never a name, an ending "
                        "or a destination token, and the record always comes from SAVED_PAYEES."})
            session.payee_phrase = payee.display_name
        elif NEGATION_RE.match(req.text.strip().lower()):
            session.pending_nickname, session.open_clarification_field = None, None
            emit("NICKNAME_DECLINED", 4, {
                "nickname": nickname, "beneficiary_id": payee.beneficiary_id,
                "stored": False, "confirmed_by_customer": False,
                "note": "The customer declined to have the name remembered. Nothing was written. "
                        "The payment continues, refusing to save a preference must not cost them "
                        "the payment they asked for."})
            say_line("NICKNAME_DECLINED", 4)
            session.payee_phrase = payee.display_name
        else:
            # Neither yes nor no, an unclear answer must not silently drop a binding the customer
            # has already half-made, so the same question is asked again.
            session.note_clarification("NICKNAME_CONFIRM_UNCLEAR")
            emit("CLARIFICATION_OPENED", 4, {
                "clarification_field": "PAYEE_NICKNAME",
                "clarification_key": "NICKNAME_CONFIRM_UNCLEAR",
                "canonical_draft": "NOT_CREATED"})
            say_line("NICKNAME_CONFIRM", 4, nickname=display_phrase(nickname),
                     payee=payee.display_name, masked=_ending(payee.masked))
            return await finish()

        # The name is settled, so this is now an ordinary payment turn. Routing it back through the
        # normal stages (rather than assembling a draft here) is what keeps stage 6-10 unchanged:
        # "ah girl" resolves, the amount comes from the committed evidence, and the draft, the
        # validation, the risk check and the read-back all run exactly as they do for any payment.
        intent = IntentResult(intent="PAYMENT", confidence=0.95,
                              detected_language=detect_language(req.text),
                              reason="deterministic nickname safeguard (the name has just been "
                                     "taught, so the original instruction resolves)")
        engine = "deterministic-safeguard"
        emit("INTENT_RESULT", 4, {"engine": engine, **intent.model_dump(),
                                  "threshold": Settings.INTENT_CONFIDENCE_THRESHOLD,
                                  "note": "routed back to PAYMENT after the nickname was resolved"})

    if intent.intent == "CANCEL_OR_STOP":
        session.state, session.cancelled, session.outcome = SessionState.CANCELLED, True, "CANCEL"
        emit("PAYMENT_CANCELLED", 4, {"draft_discarded": bool(session.draft),
                                      "confirmation_invalidated": True,
                                      "signature_invalidated": bool(session._signature),
                                      "ledger_invoked": False})
        session.confirmation = None
        session._signature, session._payload, session._nonce = None, None, None
        say_line("CANCELLED", 4)
        return await finish()
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
        say_line("SAFETY_HELP", 4)
        return await finish()
    if intent.intent == "UNCLEAR":
        # IN-10 continuation: a mumble or a stray "yes" while a multi-instruction queue is under
        # review. Route it to the merge handler, which treats a turn with no new instruction as a
        # pure acknowledgement and re-presents the open draft, the queue is preserved and nothing
        # is executed (a spoken "yes" still cannot bypass FaceID). Cancels/safety are handled
        # above, so reaching here means it is genuinely an unclear or acknowledgement turn.
        if _multi_in_progress(session):
            return await run_multi_merge(req, session, lang, emit, say_line, finish)
        # A payment may already be sitting on the approval card. A typed or spoken answer still
        # authorises NOTHING (that is the control working, not a fault), but "there's no payment
        # waiting for your confirmation" is plainly untrue while a confirmation is open, and it
        # reads to a customer as "I don't understand you". This state gets its own sentence.
        # Nothing else about the turn changes: still no challenge, still no signature.
        approval_pending = bool(session.draft is not None and session.confirmation is not None
                                and not session.frozen and not session.cancelled)
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        # The INTENT genuinely is unclear, so the personalization signal keeps its existing name,
        # repeated typed "yes" turns must still count as a repeated clarification.
        session.note_clarification("INTENT_UNCLEAR")
        emit("NO_ACTIVE_CHALLENGE", 4, {
            "active_confirmation_challenge": None, "active_signing_challenge": None,
            "execution_permission": "DENIED", "signature_creatable": False,
            "approval_pending_on_screen": approval_pending,
            "reason": "A generic or replayed “yes” cannot create a signature."})
        say_line("APPROVAL_PENDING" if approval_pending else "INTENT_UNCLEAR", 4)
        return await finish()

    # ---------- EDIT_PAYMENT invalidates everything downstream (§8.11.3) ----------
    if intent.intent == "EDIT_PAYMENT" and session.draft:
        emit("EDIT_INVALIDATION", 4, {
            "confirmation_invalidated": session.confirmation.confirmation_id if session.confirmation else None,
            "signature_invalidated": bool(session._signature),
            "superseded_draft_version": session.current_draft_version,
            "old_field_confirmations_reused": False,
            "date_only_correction": date_correction,
            "note": ("A date-only correction: the recipient, the amount and the source account are "
                     "carried forward unchanged, only WHEN the payment runs is re-resolved."
                     if date_correction else
                     "The payment on screen is superseded; every downstream result is discarded.")})
        session.confirmation = None
        session._signature, session._payload, session._nonce = None, None, None
        session.validation = None
        session.state = SessionState.DRAFTING

    if intent.intent == "PAYMENT_CLARIFICATION":
        session.amount_disambiguated = True
        session.open_clarification_field = None
        # BRD §A.8 E3, the customer asks for the review in another language.
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

    # ---------- Stage 5: Payment Agent ----------
    ctx = {
        "open_clarification_field": session.open_clarification_field,
        "customer_id": session.customer_id,
        "amount_disambiguated": session.amount_disambiguated,
        "evidence": list(session.evidence),
        "asr_confidence": req.asr_confidence,
        # Carried alongside every use of asr_confidence so the audit trail can always tell a
        # measured score from the 0.96 placeholder. Nothing downstream should read the number
        # without this.
        "asr_confidence_source": req.asr_confidence_source,
        # The trusted preferred name, so the mock agent's proposed question is rendered with
        # the same address the deterministic layer will use (§A.6 C3, never the chat text).
        "customer_name": customer_name(session),
        "draft": {"payee_phrase": session.payee_phrase,
                  "amount_minor": session.draft.amount_minor if session.draft else None},
    }
    if date_correction:
        # The payment on screen is already agreed, so the Payment Agent is not consulted at all.
        # Reading "on 28th september instead" back into a proposal could only re-derive the amount
        # from the one number in the sentence (S$28.00) and drop the date. The draft already on
        # screen IS the proposal, only WHEN it runs is still open.
        proposal = _proposal_from_existing_draft(session.draft, lang)
        engine = "deterministic (carried forward, date-only correction)"
    else:
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

    # §MVP, one saved-payee transfer at a time. A request that names 2+ payees is clarified down
    # to a single recipient before any draft is built. This is a deterministic policy, so it fires
    # regardless of what the (untrusted) Payment Agent returned, even a PROPOSAL_READY that carried
    # two names would be caught here instead of silently binding the first match.
    multi = detect_multiple_recipients(req.text, session.customer_id)
    if multi:
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = "RECIPIENT"
        session.note_clarification("MULTIPLE_RECIPIENTS")
        emit("CLARIFICATION_OPENED", 5, {
            "clarification_field": "RECIPIENT",
            "clarification_key": "MULTIPLE_RECIPIENTS",
            "candidates": [{"displayName": n} for n in multi],
            "canonical_draft": "NOT_CREATED"})
        say_line("MULTIPLE_RECIPIENTS", 5, recipients=join_options(multi, lang))
        return await finish()

    # ---------- IN-10: multiple independent instructions in one breath ----------
    # "Pay StarHub S$86.40, send Ah Girl S$50, and move S$1,000 to joint" names three distinct
    # payment targets. split_instructions only returns >=2 when that is genuinely the case, so
    # ordinary single payments (and two-saved-payee turns, handled above) are untouched.
    instructions = split_instructions(req.text, session.customer_id)
    # IN-10 continuation, a breath that arrives WHILE a multi-instruction queue is already
    # being reviewed. The customer may add a new instruction or amend one already queued
    # ("yes, and send Ah Girl from my Current Account"). Either way we MERGE into the existing
    # queue: we never rebuild it from scratch (that would discard the instruction under review)
    # and we never let a spoken "yes" execute anything, FaceID still stands between the
    # customer and the money (§A.8 E1). The leading acknowledgement is stripped by
    # instruction_text so the new instruction is resolved on its own.
    if _multi_in_progress(session):
        return await run_multi_merge(req, session, lang, emit, say_line, finish)
    if len(instructions) >= 2:
        return await run_multi_turn(req, session, lang, instructions, emit, say_line, finish)

    if proposal.status != "PROPOSAL_READY":
        # ---------- A name the customer used that we hold nobody for ----------
        # "Send Ah Girl S$100" arrives here as a payment with no recipient, because nothing in the
        # turn binds to a saved payee. Answering "who should I pay?" throws away the one thing the
        # customer did tell us, so when the turn names an unknown person we ask who THAT is, and
        # list the people we do hold. Once they answer, the name is remembered (see the nickname
        # block at stage 4, and app/nicknames.py).
        #
        # Only when the missing field IS the recipient: an unknown name alongside a missing amount
        # is a different question, and asking two at once is worse than asking one.
        unknown_name = (unknown_payee_phrase(req.text, session.customer_id)
                        if proposal.clarification_field == "RECIPIENT" else None)
        if unknown_name:
            options = _saved_payee_options(session.customer_id)
            session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
            session.open_clarification_field = "PAYEE_NICKNAME"
            session.pending_nickname = {"nickname": unknown_name}
            session.note_clarification("NICKNAME_UNKNOWN")
            emit("NICKNAME_LOOKUP_OPENED", 5, {
                "nickname": unknown_name,
                "candidates": options,
                "open_clarification_field": "PAYEE_NICKNAME",
                "canonical_draft": "NOT_CREATED",
                "note": "The customer used a name for a person that is not on their saved-payee "
                        "list. Nothing is guessed: a destination can only come from a payee the "
                        "customer already holds, and the binding is confirmed before it is saved."})
            say_line("NICKNAME_UNKNOWN", 5, nickname=display_phrase(unknown_name),
                     options=candidate_options(options, lang))
            return await finish()

        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = proposal.clarification_field
        session.note_clarification(proposal.clarification_field or "PAYMENT_UNCLEAR")
        emit("CLARIFICATION_OPENED", 5, {
            "clarification_field": proposal.clarification_field,
            # The agent's own proposed wording, kept for the audit trail. It is NOT what the
            # customer hears, the INTERFACE_LINE below is rendered from the catalogue.
            "agent_question_proposal": proposal.clarification_question,
            "canonical_draft": "NOT_CREATED"})
        _say_payment_clarification(req, session, proposal, say_line)
        return await finish()

    # ---------- Stage 6: deterministic payee and account resolution ----------
    # A spoken future-execution time (IN-08) is attached to the draft and rendered into the
    # read-back's SCHEDULE clause. TWO extractors feed ONE date computer:
    #   * the deterministic patterns in parsing (primary, they need no model at all), and
    #   * the Payment Agent's schedule_hint (a fallback for a phrasing the patterns cannot reach).
    # Neither produces a date. compute_schedule() does, from today's date, and it discards a hint
    # it cannot validate, so the model can never decide when money moves.
    schedule_dict = parse_schedule(req.text)
    schedule_source = "deterministic patterns (no model involved)" if schedule_dict else None
    if schedule_dict is None and proposal.schedule_hint is not None:
        schedule_dict = schedule_from_hint(proposal.schedule_hint)
        schedule_source = ("payment agent extracted the phrase; deterministic code computed the date"
                           if schedule_dict else None)
    if schedule_dict:
        if session.open_clarification_field == "SCHEDULE":
            # The "which day did you mean?" question has now been answered, so it must not stay
            # open, a stale open field would keep re-routing later turns that merely mention a date.
            session.open_clarification_field = None
        emit("SCHEDULE_RESOLVED", 6, {
            "extracted_by": schedule_source,
            "spoken": schedule_dict["relative"],
            "computed_by": "deterministic code (parsing.compute_schedule)",
            "absolute_date": schedule_dict["absolute"], "time": schedule_dict["time"],
            "note": "The extractor said WHAT was meant; code decided WHEN. No model produced a "
                    "date, and a hint that failed validation was dropped rather than used."})
    # A date the customer NAMED but that could not be turned into a day, "the 3rd of this month"
    # said on the 26th, "the 28th" with no month, "31 September". Silence is the one answer that
    # must not be given here: the read-back would carry no date at all, and nothing would tell the
    # customer it had been ignored. Checked after the schedule block so a resolvable date always
    # wins. (A date correction can never reach this: it only counts as one once it has resolved.)
    if schedule_dict is None and schedule_named_but_unusable(req.text):
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = "SCHEDULE"
        session.note_clarification("SCHEDULE_UNCLEAR")
        emit("CLARIFICATION_OPENED", 6, {
            "clarification_field": "SCHEDULE", "clarification_key": "SCHEDULE_UNCLEAR",
            "canonical_draft": "NOT_CREATED",
            "note": "A date was named and recognised but could not be turned into a day. No draft is "
                    "created, the customer is asked which day they meant, rather than being handed "
                    "a payment with no date on it."})
        say_line("SCHEDULE_UNCLEAR", 6)
        return await finish()

    # customer_id is passed so a nickname the customer says out loud ("ah girl") is recognised
    # even when ASR hands it to us in lowercase, parse_payee_phrase checks nicknames first.
    session.payee_phrase = (session.payee_phrase
                            or parse_payee_phrase(req.text, session.customer_id)
                            or proposal.recipient_phrase)
    # How the payee was bound, and the nickname the customer actually used (if any). The nickname
    # travels to the read-back, which is where "I found Ah Girl as Mei Tan" is spoken.
    matched_via: Optional[str] = None
    nickname_used: Optional[str] = None
    if date_correction:
        # Carry the resolution forward rather than re-deriving it. Re-deriving re-binds the source
        # account by a name match, and a near miss there would silently move the money from a
        # different account than the one the customer is looking at.
        resolution = DeterministicResolution(outcome="RESOLVED", draft=session.draft)
        emit("PAYEE_RESOLUTION", 6, {
            "outcome": "RESOLVED", "reason": "CARRIED_FORWARD_DATE_ONLY_CORRECTION",
            "candidates": [], "llm_generated_beneficiary_id": False,
            "note": "A date-only correction. The payee, the destination token and the source account "
                    "are the ones already resolved and shown to the customer."})
    else:
        resolution = resolve_payee_and_account(session.customer_id, proposal, session.payee_phrase)
        # Attribute the match honestly. parse_payee_phrase resolves a nickname to its display name
        # BEFORE the resolver sees it, so the resolver reports "display_name" even when the
        # customer never said that name. If a nickname they DID say points at this same payee,
        # then the nickname was the lookup key, and the audit should say so, because that is the
        # claim being made to a judge: the nickname supplied the key, never the destination.
        nickname_hit = nickname_match(session.customer_id, req.text)
        if nickname_hit is None and session.nickname_used:
            # The customer confirmed the binding in THIS turn ("yes"), so the name is not among the
            # words of this turn; it is in the instruction that opened the question.
            nickname_hit = nickname_match(session.customer_id, session.nickname_used)
        if (nickname_hit and resolution.outcome == "RESOLVED" and resolution.draft is not None
                and resolution.draft.beneficiary_id == nickname_hit[1].beneficiary_id):
            matched_via, nickname_used = "nickname", nickname_hit[0]
        if matched_via == "nickname":
            # Carry it to the read-back. This is the OTHER half of the nickname feature: a name the
            # customer TAUGHT in an earlier turn (or an earlier session) still gets "I found Ah Girl
            # as Mei Tan" spoken back, not just a name taught moments ago in the same turn.
            session.nickname_used = nickname_used
        emit("PAYEE_RESOLUTION", 6, {
            "outcome": resolution.outcome, "reason": resolution.reason,
            "candidates": resolution.candidates,
            "matched_via": matched_via,
            "nickname": nickname_used,
            "llm_generated_beneficiary_id": False,
            "note": "The LLM proposed a phrase. Deterministic code bound the authoritative record."
                    + (" A nickname supplied the lookup key only; the destination came from the "
                       "trusted repository." if matched_via == "nickname" else "")})

    if resolution.outcome == "FREEZE":
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "FREEZE"
        emit("TRANSACTION_FROZEN", 6, {
            "reason": resolution.reason,
            "authoritative_destination_changed": False,
            "confirmation_permitted": False, "ledger_invoked": False})
        say_line(freeze_message_key(resolution.reason), 6,
                 payee=resolution.payee_display or session.payee_phrase or "")
        return await finish()
    if resolution.outcome == "NEEDS_CLARIFICATION":
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        session.open_clarification_field = resolution.clarification_field
        session.note_clarification(resolution.clarification_field or "PAYEE_UNCLEAR")
        emit("CLARIFICATION_OPENED", 6, {
            "clarification_field": resolution.clarification_field,
            "clarification_key": resolution.clarification_key,
            "candidates": resolution.candidates, "canonical_draft": "NOT_CREATED"})
        # The duplicate-payee question NAMES the candidates. "Which one do you mean?" without
        # the two names is not a question an elderly customer can answer.
        if resolution.clarification_key == "PAYEE_AMBIGUOUS":
            say_line("PAYEE_AMBIGUOUS", 6,
                     count=len(resolution.candidates),
                     phrase=display_phrase(session.payee_phrase),
                     options=candidate_options(resolution.candidates, lang))
        else:
            say_line("PAYEE_NOT_FOUND", 6, phrase=display_phrase(session.payee_phrase))
        return await finish()

    # ---------- Stage 7: canonical draft and schema gate ----------
    session.drafts_created += 1
    session.current_draft_version += 1
    draft = resolution.draft
    draft.draft_id = f"dr_{session.current_draft_version:03d}"
    draft.version = session.current_draft_version
    draft.reference = parse_reference(req.text) or draft.reference
    draft.evidence_ids = list(session.evidence_ids)
    if schedule_dict:
        # "at" is part of the string so it reads naturally when spoken back on the SCHEDULED line
        # ("on 28 September 2026 at 9:00 AM Singapore time"). Nothing else consumes this field.
        draft.scheduled_for = (f"{schedule_dict['absolute']} at {schedule_dict['time']} "
                               f"Singapore time")
    session.draft = draft
    session.draft_versions.append(draft.version)
    session.state = SessionState.DRAFT_READY
    emit("CANONICAL_DRAFT_CREATED", 7, {
        "draft": draft.model_dump(), "schema_validation": "PASS",
        "version": draft.version, "previous_versions_superseded": draft.version > 1})

    # ---------- Stage 8: Independent Validation Agent ----------
    verified = {"account_masked": draft.account_masked,
                "payee_masked": draft.payee_masked,
                "payee_display": draft.payee_display,
                "transaction_type": draft.transaction_type}
    validation, engine = await ValidationAgent.run(draft, session.evidence, verified,
                                                   session.customer_id)
    session.validation = validation
    emit("VALIDATION_RESULT", 8, {"engine": engine, **validation.model_dump(),
                                  "scope": "independent, no Payment Agent reasoning supplied"})

    # ---------- Stage 9: deterministic comparator ----------
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
        say_line("FROZEN_MISMATCH", 9)
        return await finish()

    # ---------- Stage 10: risk and velocity policy ----------
    risk = evaluate_risk(draft, {"drafts_created": session.drafts_created}, guardrail)
    emit("RISK_POLICY_RESULT", 10, {**risk.model_dump(),
                                    "thresholds_source": "external configuration"})
    if risk.decision == RiskDecision.REJECT:
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "REJECT"
        emit("RISK_REJECTED", 10, {"reasons": risk.reasons, "ledger_invoked": False})
        say_line("FROZEN_RISK", 10)
        return await finish()
    if risk.decision == RiskDecision.COOLING_OFF:
        session.state, session.cooling_off, session.outcome = SessionState.COOLING_OFF, True, "COOLING_OFF"
        emit("COOLING_OFF_APPLIED", 10, {
            "reasons": risk.reasons,
            "note": "Urgency alone does not prove fraud. Cooling-off is a delay, not an accusation.",
            "ledger_invoked": False})
        say_line("COOLING_OFF", 10)
        return await finish()
    if risk.decision == RiskDecision.STEP_UP:
        session.state, session.outcome = SessionState.CONFIRMATION_READY, "STEP_UP"
        session.step_up_required = True
        emit("STEP_UP_REQUIRED", 10, {"reasons": risk.reasons})
        # Now that the high-value band makes this branch reachable, the customer must hear WHY
        # extra care is being taken (previously the STEP_UP_REQUIRED event fired with no spoken line).
        say_line("STEP_UP_REQUIRED", 10)
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
    # The read-back is generated from the canonical draft, the same values the customer will
    # be asked to confirm one field at a time, so what they hear and what they review agree.
    #
    # It speaks INITIATION_READBACK, the shape the test table specifies for a valid payment
    # initiation: "You want to send S$50.00 from DBS Savings ending 1234 to Mei Tan ending
    # 7788. I can prepare this payment for you to review. Is that correct?"
    #
    # This is NOT a clarification. The state here is CONFIRMATION_READY; the customer's answer
    # reaches us through /confirm and /field_signal, and a "no" is classified as EDIT_PAYMENT.
    # The closing question is a cue that review is starting, not a question that reopens one.
    # The read-back shape follows the transaction type. A bill payment reads back the biller
    # and the outstanding amount; a saved-payee transfer reads back the person and destination.
    _emit_draft_readback(draft, parse_reference(req.text) or None, schedule_dict,
                         lang, session.customer_id, emit, say_line,
                         nickname=session.nickname_used,
                         colloquial_phrase=colloquial_scale_phrase(req.text),
                         hold=_identity_hold(session))
    # Consumed. The nickname belongs to the payment that produced it; a later payment in the same
    # session must not inherit "I found Ah Girl as Mei Tan" because an earlier one was reached
    # through that name.
    session.nickname_used = None

    return await finish()


# =====================================================================
# 10a. Initiation read-back, one helper, so the single-instruction path and the
# multi-instruction path (IN-10) speak the SAME sentence from the SAME catalogue.
# =====================================================================
def _nickname_for_index(session, index: int) -> Optional[str]:
    """The nickname recorded for queued draft `index`, or None.

    Indexed rather than read straight off the list because a queue is built in several places
    (the first multi turn, then a merge that can append or amend), and a payment added before
    this list existed must read as "no nickname" instead of raising in the middle of a demo.
    """
    nicknames = getattr(session, "pending_nicknames", None) or []
    return nicknames[index] if 0 <= index < len(nicknames) else None


def _identity_hold(session) -> bool:
    """True while a Stage 1 speaker mismatch is HOLDING the request.

    Only a speaker mismatch holds. The risk agent's own step-up (a large amount) still reads the
    payment back, because there the customer's identity was never in question - only the size of
    the transfer was. Conflating the two would hide the payment from a legitimate customer.
    """
    return bool(session.step_up_required) and session.step_up_reason == "SPEAKER_MISMATCH"


def _emit_draft_readback(draft: Draft, reference: Optional[str], schedule_dict: Optional[dict],
                         lang: str, customer_id: str, emit, say_line,
                         nickname: Optional[str] = None,
                         colloquial_phrase: Optional[str] = None,
                         hold: bool = False) -> None:
    """Emit the INITIATION_READBACK for a canonical draft.

    The read-back SHAPE follows the transaction type, a bill/card reads back the outstanding
    balance, an own-account move reads back both accounts, a saved-payee transfer reads back the
    person and destination. The wording comes from messages.readback_parts, so this stays the one
    place a customer-facing sentence is assembled.

    `nickname` is the name the customer used for the payee ("ah girl"). When it is set the read-back
    opens with "I found Ah Girl as Mei Tan, account ending 7788" and closes by asking whether that
    is the right person, readback_parts enforces both, and suppresses the destination in the body
    so the payee is not named twice. It only applies to a saved-payee transfer: a bill, a card or
    the customer's own account is never reached by a nickname.

    `colloquial_phrase` is the slang-scale words the customer said ("a grand", "5k"). When set the
    read-back appends a confirmation of the interpretation ("Please confirm that a 'a grand' means
    S$1,000.00."), the same "advise first" posture the demo takes for every other field. Standard
    scale words ("thousand") are never flagged.
    """
    # IDENTITY HOLD. When Stage 1 could not verify the speaker the request is held, and Joy must
    # not read the payment back: doing so would present the amount and the payee to someone whose
    # identity is unverified, and would contradict the hold message she just spoke. She says only
    # VOICE_STEP_UP; the payment is presented once FaceID on this device passes.
    if hold:
        return
    if draft.transaction_type == "BILL_PAYMENT":
        body, biller, destination = "BODY_BILL", draft.payee_display, ""
    elif draft.transaction_type == "CREDIT_CARD_PAYMENT":
        body, biller, destination = "BODY_CARD", draft.payee_display, ""
    elif draft.transaction_type == "OWN_ACCOUNT_TRANSFER":
        body, biller, destination = ("BODY_MOVE", "",
                                     f"{draft.payee_display} {draft.payee_masked}".strip())
    else:
        body, biller, destination = ("BODY_TRANSFER", "",
                                     f"{draft.payee_display} {draft.payee_masked}".strip())
    nickname_part = None
    if nickname and draft.transaction_type == "SAVED_PAYEE_TRANSFER":
        nickname_part = {"nickname": display_phrase(nickname), "name": draft.payee_display,
                         "masked": _ending(draft.payee_masked)}
    colloquial_text = ""
    if colloquial_phrase:
        colloquial_text = " " + colloquial_confirm(lang, colloquial_phrase,
                                                   draft.amount_minor, draft.currency)
    say_line("INITIATION_READBACK", 10, **readback_parts(
        lang, body=body, closer="CLOSE_REVIEW",
        amount=money(draft.amount_minor, draft.currency),
        source=source_account_phrase(customer_id, draft.source_account_id),
        destination=destination, biller=biller,
        reference=reference,
        schedule=schedule_dict if schedule_dict else None,
        nickname=nickname_part,
        colloquial=colloquial_text))


def _proposal_from_existing_draft(draft: Draft, lang: str) -> PaymentProposal:
    """The draft already on screen, restated as the proposal it originally came from.

    Used ONLY for a date-only correction, where the recipient, the amount and the source account
    are not in question. It keeps the Payment Agent out of the loop completely, so no model gets
    the chance to re-read the amount out of the day number in "on 28th september instead", which
    is exactly how that turn used to turn a S$50 payment into a S$28 one.
    """
    return PaymentProposal(
        status="PROPOSAL_READY", detected_language=lang,
        recipient_phrase=draft.payee_display, amount_minor=draft.amount_minor,
        currency=draft.currency, source_account_phrase=None,
        transaction_type=draft.transaction_type)


def _instruction_proposal(text: str, customer_id: str) -> Optional[PaymentProposal]:
    """A PaymentProposal for ONE instruction, using the SAME deterministic detection order as the
    single-instruction Payment Agent mock (bill → card → own-account → saved payee). Returns None
    when the piece names no supported target."""
    biller_name = parse_biller_phrase(text, customer_id)
    if biller_name:
        biller = lookup_biller(customer_id, biller_name)
        return PaymentProposal(
            status="PROPOSAL_READY", detected_language=detect_language(text),
            recipient_phrase=biller_name,
            amount_minor=biller.latest_amount_minor if biller else None,
            currency=parse_currency(text),
            source_account_phrase=parse_source_account_phrase(text),
            transaction_type="BILL_PAYMENT")
    card_name = parse_card_phrase(text, customer_id)
    if card_name:
        card = lookup_card(customer_id, card_name)
        return PaymentProposal(
            status="PROPOSAL_READY", detected_language=detect_language(text),
            recipient_phrase=card_name,
            amount_minor=card.latest_balance_minor if card else None,
            currency=parse_currency(text),
            source_account_phrase=parse_source_account_phrase(text),
            transaction_type="CREDIT_CARD_PAYMENT")
    own_name = parse_own_account_phrase(text, customer_id)
    if own_name:
        amount, _ = parse_amount(instruction_text(text))
        return PaymentProposal(
            status="PROPOSAL_READY", detected_language=detect_language(text),
            recipient_phrase=own_name, amount_minor=amount,
            currency=parse_currency(text),
            source_account_phrase=parse_source_account_phrase(text),
            transaction_type="OWN_ACCOUNT_TRANSFER")
    payee_phrase = parse_payee_phrase(text, customer_id)
    if payee_phrase:
        amount, _ = parse_amount(instruction_text(text))
        return PaymentProposal(
            status="PROPOSAL_READY", detected_language=detect_language(text),
            recipient_phrase=payee_phrase, amount_minor=amount,
            currency=parse_currency(text),
            source_account_phrase=parse_source_account_phrase(text))
    return None


def _multi_clause(lang: str, draft: Draft, customer_id: str, *,
                  matched_via: Optional[str] = None, spoken_nickname: Optional[str] = None,
                  source_stated: bool = True, colloquial_phrase: Optional[str] = None) -> str:
    """One bare-verb clause for the multi-instruction read-back, from a resolved draft.

    Three behaviours mirror the single-instruction read-back so the two paths cannot drift:
      * a destination reached through a nickname the customer said out loud leads with that spoken
        name and the resolved person ("to Ah Girl, whom I found as Mei Tan ending 7788");
      * an instruction that named no source account asks for one instead of silently defaulting;
      * a slang scale word ("a grand") appends a confirmation of the interpretation it assumed.
    """
    amount = money(draft.amount_minor, draft.currency)
    if matched_via == "nickname" and spoken_nickname:
        destination = _part("MULTI_NICKNAME_DEST", lang,
                            spoken=display_phrase(spoken_nickname),
                            name=draft.payee_display, masked=_ending(draft.payee_masked))
    else:
        destination = f"{draft.payee_display} {draft.payee_masked}".strip()

    if (not source_stated
            and draft.transaction_type in ("OWN_ACCOUNT_TRANSFER", "SAVED_PAYEE_TRANSFER")):
        clause = multi_item(lang, shape=("MULTI_ITEM_MOVE_NOSRC"
                                         if draft.transaction_type == "OWN_ACCOUNT_TRANSFER"
                                         else "MULTI_ITEM_SEND_NOSRC"),
                            amount=amount, destination=destination)
        clause = clause + ". " + _part("MULTI_ASK_SOURCE", lang)
    elif draft.transaction_type == "BILL_PAYMENT":
        clause = multi_item(lang, shape="MULTI_ITEM_PAY_BILL", amount=amount,
                            source=source_account_phrase(customer_id, draft.source_account_id),
                            biller=draft.payee_display)
    elif draft.transaction_type == "CREDIT_CARD_PAYMENT":
        clause = multi_item(lang, shape="MULTI_ITEM_PAY_CARD", amount=amount,
                            source=source_account_phrase(customer_id, draft.source_account_id))
    elif draft.transaction_type == "OWN_ACCOUNT_TRANSFER":
        clause = multi_item(lang, shape="MULTI_ITEM_MOVE", amount=amount,
                            source=source_account_phrase(customer_id, draft.source_account_id),
                            destination=destination)
    else:
        clause = multi_item(lang, shape="MULTI_ITEM_SEND", amount=amount,
                            source=source_account_phrase(customer_id, draft.source_account_id),
                            destination=destination)
    if colloquial_phrase:
        clause = clause + ". " + colloquial_confirm(lang, colloquial_phrase,
                                                   draft.amount_minor, draft.currency)
    return clause


async def run_multi_turn(req: TurnRequest, session, lang: str, instructions: List[str],
                         emit, say_line, finish) -> TurnResponse:
    """IN-10, several independent instructions in one breath.

    Each instruction is resolved to its own canonical draft and queued; the customer reviews and
    confirms them one at a time (session.draft always points at the instruction under review). The
    first response reads ALL of them back, so the customer can confirm the system understood the
    whole breath before anything is prepared.
    """
    drafts: List[Draft] = []
    validations: List[ValidationResult] = []
    schedules: List[Optional[dict]] = []
    references: List[Optional[str]] = []
    nicknames: List[Optional[str]] = []
    clauses: List[str] = []
    # Whether the read-back is about to ASK the customer something. If it is, the confirm card is
    # held back for one turn (session.awaiting_multi_go_ahead), otherwise the card and the question
    # above it sit on screen together and the customer cannot tell which to answer first.
    open_questions: List[str] = []

    for text in instructions:
        proposal = _instruction_proposal(text, session.customer_id)
        if proposal is None or proposal.amount_minor is None:
            continue
        resolution = resolve_payee_and_account(session.customer_id, proposal,
                                               proposal.recipient_phrase)
        if resolution.outcome != "RESOLVED" or resolution.draft is None:
            continue
        draft = resolution.draft
        draft.draft_id = f"dr_{len(drafts) + 1:03d}"
        draft.version = len(drafts) + 1
        draft.reference = parse_reference(text) or ""
        draft.evidence_ids = list(session.evidence_ids)
        sched = parse_schedule(text)
        if sched:
            draft.scheduled_for = f"{sched['absolute']} at {sched['time']} Singapore time"
        # Per-instruction signals for the read-back: whether the payee was reached through a
        # nickname the customer said, the spoken word for it, whether a source account was named,
        # and any slang scale word whose interpretation we should confirm.
        # NOTE: _instruction_proposal canonicalises a spoken nickname ("Ah Girl") to its display
        # name ("Mei Tan") before resolve_payee_and_account runs, so the resolver reports
        # matched_via="display_name" even when the customer used a nickname. The authoritative
        # signal for "did the customer say a nickname" is therefore match_in_text on the raw
        # text, not the resolver's matched_via; we use that to drive the attribution.
        nick_match = nickname_match(session.customer_id, text)
        spoken = nick_match[0] if nick_match else None
        matched_via = "nickname" if spoken else getattr(resolution, "matched_via", None)
        source_stated = bool(proposal.source_account_phrase)
        colloquial = colloquial_scale_phrase(text)
        # Deterministic mock validation (the reference policy), the multi-instruction read-back is
        # about the SPLIT, and each draft is still validated independently before it is queued.
        # customer_id is the FOURTH argument, not an entry in `verified`; the mock reads it from
        # its own parameter. It matters for the same reason it does in split_instructions: the
        # RECIPIENT check re-reads the payee from the evidence, and a nickname the customer
        # TAUGHT only resolves with it. Without it the check found no evidence phrase, came back
        # MISMATCH, and froze the payment.
        validation = ValidationAgent.mock(draft, [text], {
            "account_masked": draft.account_masked, "payee_masked": draft.payee_masked,
            "payee_display": draft.payee_display, "transaction_type": draft.transaction_type},
            session.customer_id)
        if validation.status != "PASS":
            continue
        drafts.append(draft)
        validations.append(validation)
        schedules.append(sched)
        references.append(parse_reference(text) or None)
        nicknames.append(spoken)
        clauses.append(_multi_clause(lang, draft, session.customer_id,
                                     matched_via=matched_via, spoken_nickname=spoken,
                                     source_stated=source_stated,
                                     colloquial_phrase=colloquial))
        # Two things the read-back asks about, and both need the customer to speak before the
        # review card makes sense: a slang amount to confirm ("a grand"), and a payment with no
        # source account named. A bill or a card payment always has its source resolved, so only
        # a transfer / an own-account move can ask the second question.
        if colloquial:
            open_questions.append("COLLOQUIAL_AMOUNT")
        if (not source_stated
                and draft.transaction_type in ("OWN_ACCOUNT_TRANSFER", "SAVED_PAYEE_TRANSFER")):
            open_questions.append("SOURCE_ACCOUNT")

    if len(drafts) < 2:
        # Nothing safely splittable after resolution, say so rather than silently dropping one.
        session.state, session.outcome = SessionState.CLARIFICATION_REQUIRED, "CLARIFY"
        emit("MULTI_INSTRUCTION_UNRESOLVED", 5, {
            "resolved": len(drafts), "requested": len(instructions),
            "canonical_draft": "NOT_CREATED"})
        say_line("INTENT_UNCLEAR", 5)
        return await finish()

    session.pending_drafts = drafts
    session.pending_validations = validations
    session.pending_schedules = schedules
    session.pending_references = references
    session.pending_nicknames = nicknames
    session.multi_total = len(drafts)
    session.draft_index = 0
    session.draft = drafts[0]
    session.validation = validations[0]
    session.current_draft_version = drafts[0].version
    session.draft_versions = [d.version for d in drafts]
    session.confirmation = Confirmation(
        confirmation_id=f"cnf_{drafts[0].version:03d}", draft_id=drafts[0].draft_id,
        draft_version=drafts[0].version, language=session.confirmation_language)
    session.state, session.outcome = SessionState.CONFIRMATION_READY, "MULTI_INSTRUCTION"
    # Hold the confirm card back for one turn when the read-back has just asked something, so the
    # customer answers the question FIRST and then acts on the payment. Nothing about the control
    # changes, the review is still CONFIRMATION_READY and still needs FaceID; this only stops two
    # competing actions (card vs question) appearing at once.
    session.awaiting_multi_go_ahead = bool(open_questions)

    emit("MULTI_INSTRUCTION_DETECTED", 5, {
        "count": len(drafts),
        "instructions": [{"draft_id": d.draft_id, "transaction_type": d.transaction_type,
                          "amount_minor": d.amount_minor, "payee": d.payee_display}
                         for d in drafts],
        "open_questions": open_questions,
        "confirm_card_deferred": bool(open_questions),
        "note": "One breath split into independent instructions; reviewed and confirmed one at a "
                "time. Each is validated independently before it is queued."
                + ((" The read-back asks about "
                    + ", ".join(sorted(set(open_questions)))
                    + ", so the review card is held back until the customer answers.")
                   if open_questions else "")})
    for d, v in zip(drafts, validations):
        emit("CANONICAL_DRAFT_CREATED", 7, {
            "draft": d.model_dump(), "schema_validation": "PASS", "version": d.version,
            "previous_versions_superseded": False})
        emit("VALIDATION_RESULT", 8, {"engine": "mock", **v.model_dump(),
                                      "scope": "independent, no Payment Agent reasoning supplied"})
    # One bubble, line-separated: the customer asked for it one at a time, but they want to
    # *read* it as one card, not four separate ones. Joining with "\n" and letting the CSS
    # preserve newlines (white-space: pre-line on .bub.iface) is what the demo sheet shows.
    first_label = multi_first_label(
        lang, transaction_type=drafts[0].transaction_type,
        biller=drafts[0].payee_display,
        destination=f"{drafts[0].payee_display} {drafts[0].payee_masked}".strip())
    say_line("MULTI_REVIEW_INTRO", 5,
             text=readback_multi(lang, clauses, first_label))
    return await finish()


# =====================================================================
# 10a-ii. Split-and-confirm continuation, a turn that arrives while a multi-instruction
# queue is already under review.
#
# Two things can happen, and both are MERGES into the existing queue (never a rebuild):
#   * a NEW instruction is named ("send Ah Girl from my Current Account"), if its recipient
#     matches a payment already queued we UPDATE that queued draft (e.g. set its source
#     account), otherwise we APPEND a fresh one; and
#   * a bare acknowledgement ("yes"), which authorises nothing; we re-present the open draft
#     so the review continues unchanged.
#
# The open draft stays the one under review and keeps its identity, so the confirm card the
# customer is already looking at is updated in place (not duplicated) and the backend's
# /confirm still drives the actual execution behind FaceID.
# =====================================================================
def _multi_in_progress(session) -> bool:
    """True while a multi-instruction queue is still being reviewed (not fully drained)."""
    if not session.pending_drafts or len(session.pending_drafts) < 1:
        return False
    # The pointer has run off the end, nothing left to review.
    if session.draft_index >= len(session.pending_drafts):
        return False
    # Once the last item has been confirmed the session leaves CONFIRMATION_READY for a terminal
    # state; a fresh turn after that is a new payment, not a merge.
    if session.state in (SessionState.EXECUTED, SessionState.SCHEDULED, SessionState.REJECTED,
                         SessionState.FROZEN, SessionState.CANCELLED, SessionState.COOLING_OFF):
        return False
    return True


def _find_queued(session, beneficiary_id: Optional[str], transaction_type: Optional[str]):
    """Find an existing queued draft by its authoritative destination + shape.

    beneficiary_id is populated for every transaction type (biller id, card id, own-account
    id, saved-payee beneficiary id), so it is the reliable match key across the four shapes.
    """
    if not beneficiary_id:
        return None
    for existing in session.pending_drafts:
        if (existing.beneficiary_id == beneficiary_id
                and existing.transaction_type == transaction_type):
            return existing
    return None


def _ensure_confirmation(session) -> None:
    """A review cannot be CONFIRMATION_READY without a live confirmation object.

    A turn the model reads as PAYMENT_CLARIFICATION nulls the confirmation ON PURPOSE (§A.8 E3,
    every previous field confirmation is discarded and the review restarts). When such a turn also
    merges a new instruction, the merge would otherwise leave the session holding a draft but no
    confirmation: /confirm then answers 409 "No draft is awaiting confirmation", and the review card
    the customer is looking at can never be acted on. Recreating it here restarts the review exactly
    as E3 intends, fresh, with no field carried over, and drops any signature bound to the
    discarded confirmation, so the next /confirm must sign again.
    """
    draft = session.draft
    if draft is None:
        return
    if session.confirmation is None or session.confirmation.draft_id != draft.draft_id:
        session.confirmation = Confirmation(
            confirmation_id=f"cnf_{draft.version:03d}", draft_id=draft.draft_id,
            draft_version=draft.version, language=session.confirmation_language)
        # A signature is bound to the confirmation it was made for; re-sign against the new one.
        session._payload, session._signature = None, None


async def run_multi_merge(req: TurnRequest, session, lang: str,
                          emit, say_line, finish) -> TurnResponse:
    """IN-10 continuation, merge a fresh instruction (or a bare ack) into the live queue."""
    remainder = instruction_text(req.text)
    new_instrs = split_instructions(remainder, session.customer_id) if remainder else []

    # A pure acknowledgement (a stray "yes" while a draft is under review) carries no new
    # instruction. It must authorise nothing, the open draft stays under review and FaceID is
    # still required. Nudge the customer to the card rather than re-reading the payment from the
    # top (re-reading reads as a restart and leaves them repeating "yes" forever).
    if not new_instrs:
        emit("MULTI_ACK_ONLY", 5, {
            "draft_preserved": True,
            "note": "A spoken yes during review authorises nothing; the open draft stays under "
                    "review and FaceID is still required to proceed."})
        say_line("APPROVAL_PENDING", 5)
        _ensure_confirmation(session)
        session.state, session.outcome = SessionState.CONFIRMATION_READY, "MULTI_INSTRUCTION"
        return await finish()

    updated, appended = 0, 0
    changed = None
    for text in new_instrs:
        proposal = _instruction_proposal(text, session.customer_id)
        if proposal is None:
            continue
        resolution = resolve_payee_and_account(session.customer_id, proposal,
                                               proposal.recipient_phrase)
        if resolution.outcome != "RESOLVED" or resolution.draft is None:
            continue
        new_draft = resolution.draft
        sched = parse_schedule(text)
        # Same attribution rule as the first multi turn: the resolver only ever sees the
        # canonicalised display name, so the customer's own word has to be read back off the
        # raw instruction. Kept per queued draft so the second read-back still says who was meant.
        nick_match = nickname_match(session.customer_id, text)
        spoken = nick_match[0] if nick_match else None
        match = _find_queued(session, new_draft.beneficiary_id, new_draft.transaction_type)

        if match is not None:
            # UPDATE the queued payment with only the fields this instruction actually supplied,
            # the rest of it is left exactly as the customer set it (one change at a time).
            if proposal.source_account_phrase:
                match.source_account_id = new_draft.source_account_id
                match.source_account_display = new_draft.source_account_display
                match.account_masked = new_draft.account_masked
            if proposal.amount_minor is not None:
                match.amount_minor = new_draft.amount_minor
            if sched:
                match.scheduled_for = (f"{sched['absolute']} at {sched['time']} "
                                       f"Singapore time")
            # Re-validate so the comparator stays honest about the changed fields.
            idx = session.pending_drafts.index(match)
            # Only overwrite when this instruction actually used a nickname: an amendment that
            # just fixes the amount must not erase the attribution the first one earned.
            if spoken and idx < len(session.pending_nicknames):
                session.pending_nicknames[idx] = spoken
            v = ValidationAgent.mock(match, list(session.evidence), {
                "account_masked": match.account_masked,
                "payee_masked": match.payee_masked,
                "payee_display": match.payee_display,
                "transaction_type": match.transaction_type}, session.customer_id)
            session.pending_validations[idx] = v
            if match is session.draft:
                session.validation = v
            emit("MULTI_ITEM_UPDATED", 7, {
                "draft_id": match.draft_id, "beneficiary_id": match.beneficiary_id,
                "updated_fields": {
                    "source_account": bool(proposal.source_account_phrase),
                    "amount": proposal.amount_minor is not None,
                    "schedule": bool(sched)},
                "source_account_display": match.source_account_display,
                "amount_minor": match.amount_minor,
                "note": "An already-queued payment was amended in place; no duplicate was created."})
            updated += 1
            if changed is None:
                changed = match
        else:
            # A genuinely new recipient: only append when it resolves fully (it carries an amount).
            # An under-specified one is noted and left for a later turn, so we never push an
            # incomplete payment into the queue.
            if proposal.amount_minor is None:
                emit("MULTI_ITEM_SKIPPED", 7, {
                    "beneficiary_id": new_draft.beneficiary_id, "reason": "NO_AMOUNT",
                    "note": "A new payee without an amount is not added yet; ask for the amount "
                            "first."})
                continue
            new_draft.draft_id = f"dr_{len(session.pending_drafts) + 1:03d}"
            new_draft.version = len(session.pending_drafts) + 1
            new_draft.reference = parse_reference(text) or ""
            new_draft.evidence_ids = list(session.evidence_ids)
            if sched:
                new_draft.scheduled_for = (f"{sched['absolute']} at {sched['time']} "
                                           f"Singapore time")
            v = ValidationAgent.mock(new_draft, list(session.evidence), {
                "account_masked": new_draft.account_masked,
                "payee_masked": new_draft.payee_masked,
                "payee_display": new_draft.payee_display,
                "transaction_type": new_draft.transaction_type}, session.customer_id)
            if v.status != "PASS":
                continue
            session.pending_drafts.append(new_draft)
            session.pending_validations.append(v)
            session.pending_schedules.append(sched)
            session.pending_references.append(parse_reference(text) or None)
            session.pending_nicknames.append(spoken)
            session.multi_total = len(session.pending_drafts)
            emit("MULTI_ITEM_APPENDED", 7, {
                "draft_id": new_draft.draft_id, "beneficiary_id": new_draft.beneficiary_id,
                "amount_minor": new_draft.amount_minor,
                "source_account_display": new_draft.source_account_display})
            appended += 1
            if changed is None:
                changed = new_draft

    # The open draft keeps its identity, so the confirm card on screen is updated in place rather
    # than duplicated, and the spoken "yes" that may have led this turn executed nothing.
    # A turn the model read as PAYMENT_CLARIFICATION has already discarded the confirmation (E3),
    # so it is recreated here, otherwise /confirm 409s and the card can never be acted on.
    _ensure_confirmation(session)
    session.state, session.outcome = SessionState.CONFIRMATION_READY, "MULTI_INSTRUCTION"
    emit("MULTI_MERGE_SUMMARY", 7, {
        "updated": updated, "appended": appended,
        "queue_size": len(session.pending_drafts),
        "open_draft_id": session.draft.draft_id,
        "note": "The open draft stays under review. A spoken yes does not execute it; FaceID is "
                "still required to move money."})
    # Deliberately NOT a full read-back of the payment under review. Re-reading it from the top
    # made the merge look like a restart: the customer heard the same "is that correct?" again,
    # answered "yes", and looped forever without ever reaching the review card. The merge is
    # acknowledged in one line and the review continues exactly where it was, the card on
    # screen is still the thing to act on, and FaceID still gates the money.
    if changed is not None:
        say_line("MULTI_MERGE_NOTED", 7, payee=changed.payee_display,
                 source=changed.source_account_display)
    else:
        # Nothing resolved in this turn, so the only true thing to say is where to act.
        say_line("APPROVAL_PENDING", 7)
    session.nickname_used = None
    return await finish()


# =====================================================================
# 10b. Field-review signal, reported by the client the instant a customer accepts or
# rejects a field on review (BRD §8.11.2 UI), independent of and before /confirm.
# This is what makes "the same field rejected twice" a real, server-tracked fact instead
# of something the client claims after it already happened.
# =====================================================================
class FieldSignalRequest(BaseModel):
    session_id: str
    field: Literal["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"]
    result: Literal["CONFIRMED", "REJECTED"]


async def run_field_signal(req: FieldSignalRequest) -> TurnResponse:
    session = get_session(req.session_id)
    events: List[dict] = []

    def emit(event_type: str, stage: Optional[int], payload: dict) -> None:
        record = AUDIT.append(session.session_id, event_type, payload, stage)
        events.append({"event_type": event_type, "stage": stage, "ts": record["ts"],
                       "payload": payload, "hash": record["hash"]})

    if req.result == "REJECTED":
        session.note_field_rejection(req.field)
    emit("FIELD_REVIEW_SIGNAL", 11, {"field": req.field, "result": req.result,
                                     "note": "UI-only signal, never signs and never calls the gateway."})
    if req.result == "REJECTED":
        # The "no worries, tell me the correct X" line comes from the same catalogue as every
        # other sentence, instead of being assembled in the browser.
        say(emit, "FIELD_EDIT_REQUESTED", 11, session, session.confirmation_language,
            field=req.field.lower().replace("_", " "))
    await evaluate_personalization(session, emit)
    return TurnResponse(session=session.to_public(), events=events)


# =====================================================================
# 11. Stage 11-15: confirmation, biometric, signing, gateway, ledger
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
    # get_session() creates a session on demand, which is right for /turn (that is how a
    # conversation starts) but wrong here. A /confirm for an id this server has never seen means
    # the session was LOST (sessions are in-memory, so a backend restart wipes them), and
    # fabricating an empty one is what turned that into the baffling
    # "No draft is awaiting confirmation (draft=NONE, confirmation=NONE)".
    if req.session_id not in SESSIONS:
        raise HTTPException(409, f"Unknown session '{req.session_id}'. Sessions are held in "
                                 f"memory, so a backend restart loses them. Please start a new "
                                 f"conversation.")
    session = get_session(req.session_id)
    events: List[dict] = []

    def emit(event_type: str, stage: Optional[int], payload: dict) -> None:
        record = AUDIT.append(session.session_id, event_type, payload, stage)
        events.append({"event_type": event_type, "stage": stage, "ts": record["ts"],
                       "payload": payload, "hash": record["hash"]})

    # The review language is the one this CONVERSATION is already being spoken in, not a re-guess
    # at the customer's wording and not simply whatever the client sends. By this point run_turn
    # has settled on a language for the session, and every surface the customer now sees, the
    # field review, the biometric prompt, the next instruction's read-back, the receipt, has to
    # stay in it, or Auntie appears to change language between two screens of one payment.
    # req.language (the explicit choice behind §A.8 E3) is still the fallback for a session that
    # has not spoken yet.
    lang = session.interface_language or req.language
    session.interface_language = lang

    def say_line(key: str, stage: Optional[int], **params) -> None:
        say(emit, key, stage, session, lang, **params)

    if not session.draft or not session.confirmation:
        raise HTTPException(409, f"No draft is awaiting confirmation "
                                 f"(draft={'present' if session.draft else 'NONE'}, "
                                 f"confirmation={'present' if session.confirmation else 'NONE'}).")
    if session.frozen or session.cancelled:
        raise HTTPException(409, f"The transaction is frozen or cancelled "
                                 f"(frozen={session.frozen}, cancelled={session.cancelled}). "
                                 f"Start a new conversation to continue.")

    draft, confirmation = session.draft, session.confirmation
    # The review is conducted in the language this conversation already settled on (lang), not
    # the raw language the client happened to send. A client that guesses "ZH" from the Han
    # characters in a code-mixed command must not flip the confirm page or the next instruction
    # into Chinese, see the note above at the lang assignment.
    confirmation.language = lang
    session.confirmation_language = lang

    # ---------- Stage 11: field-by-field confirmation ----------
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
        # The teaching moment (§A.8 E1): the customer must HEAR that three "yes" answers
        # authorised nothing. Without this line the live E1 path was silent.
        say_line("CONFIRM_NEEDS_BIOMETRIC", 12)
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 12: mock biometric adapter ----------
    emit("MOCK_FACEID", 12, {"result": req.faceid_result, "simulation": True,
                             "confirmation_id": confirmation.confirmation_id,
                             "production_capability": False})
    if req.faceid_result == "FAILED":
        session.state, session.frozen, session.outcome = SessionState.FROZEN, True, "REJECTED"
        emit("CONTROL_STOPPED", 12, {"reason_code": "BIOMETRIC_NOT_VERIFIED",
                                     "signature_created": False, "gateway_call": "NOT_PERMITTED",
                                     "ledger_invoked": False})
        say_line("BIOMETRIC_FAILED", 12)
        return TurnResponse(session=session.to_public(), events=events)

    # Identity is now established on this device, so any Stage 1 hold is RELEASED. Without this a
    # second payment in the same session would stay hidden behind a step-up already satisfied:
    # _identity_hold() would still be true and its read-back would never be spoken.
    session.step_up_required = False
    session.step_up_reason = None

    # ---------- Stage 13: canonical payload signing ----------
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
            "mechanism": "HMAC-SHA256, labelled demonstration mechanism, not a production HSM signature",
            "bound_to_draft_version": draft.version,
            "canonical_payload": payload})
    else:
        payload, signature = session._payload, session._signature
        emit("PAYLOAD_REUSED", 13, {
            "payload_hash": payload_hash(payload),
            "reason": "A previously signed payload is being resubmitted, not re-signed.",
            "nonce": payload["nonce"], "idempotency_key": payload["idempotency_key"]})

    # ---------- Stage 14: Zero-Trust Gateway ----------
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
        say_line("REJECTED", 14)
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
        signature = sign_payload(expired)      # it really was signed; it is simply stale now
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
        # Previously silent: an idempotent retry returned the receipt with no spoken line,
        # while the other forgeries said REJECTED. The customer now hears the original reference.
        say_line("IDEMPOTENT_RETRY_DONE", 14, reference=gateway.transaction_id)
        return TurnResponse(session=session.to_public(), events=events)

    if gateway.decision == GatewayDecision.REJECTED:
        session.state, session.outcome = SessionState.REJECTED, "REJECTED"
        emit("LEDGER_NOT_INVOKED", 14, {"ledger_invoked": False})
        say_line("REJECTED", 14)
        return TurnResponse(session=session.to_public(), events=events)

    # ---------- Stage 15: restricted mock payment capability ----------
    payee_phrase = f"{draft.payee_display} {draft.payee_masked}".strip()

    if draft.scheduled_for:
        # A future-dated payment is HELD, not EXECUTED. The customer has approved it, but the date
        # has not arrived, so no money has moved: the ledger must NOT be written and no receipt may
        # exist. This is a deterministic consequence of the date already on the draft, no agent
        # decides it. (The browser renders the receipt card from LEDGER_RECORDED, so withholding
        # that event is what stops a receipt appearing for a payment that has not been sent.)
        session.state, session.outcome = SessionState.SCHEDULED, "SCHEDULED"
        session.execution = {"transaction_id": None, "status": "SCHEDULED",
                             "scheduled_for": draft.scheduled_for,
                             "amount_minor": draft.amount_minor, "currency": draft.currency,
                             "execution_count": 0, "new_entry": False}
        emit("LEDGER_NOT_INVOKED", 14, {
            "ledger_invoked": False, "reason": "FUTURE_DATED_PAYMENT_HELD",
            "scheduled_for": draft.scheduled_for,
            "note": "The payment is held until its date. Nothing has been sent, so no ledger "
                    "entry is written and no receipt exists."})
        emit("SCHEDULE_HELD", 15, {**session.execution,
                                   "gateway_authorised_only": True,
                                   "direct_agent_or_browser_calls": "REJECTED"})
        say_line("SCHEDULED", 15, amount=money(draft.amount_minor), payee=payee_phrase,
                 when=draft.scheduled_for)
    else:
        txn_id, count, created = Ledger.execute(payload["idempotency_key"], ledger_request)
        session.execution = {"transaction_id": txn_id, "status": "RECORDED",
                             "amount_minor": draft.amount_minor, "currency": draft.currency,
                             "execution_count": count, "new_entry": created}
        session.state, session.outcome = SessionState.EXECUTED, "ACCEPTED"
        emit("LEDGER_RECORDED", 15, {**session.execution,
                                     "gateway_authorised_only": True,
                                     "direct_agent_or_browser_calls": "REJECTED"})
        say_line("EXECUTED", 15, amount=money(draft.amount_minor), payee=payee_phrase,
                 reference=txn_id)

    # IN-10, if more independent instructions are queued behind this one, advance to the next and
    # read it back, so the customer confirms them one at a time. A fresh signature is required for
    # the new draft (the old payload is bound to the previous draft version).
    if session.pending_drafts and session.draft_index + 1 < len(session.pending_drafts):
        session.draft_index += 1
        nxt = session.pending_drafts[session.draft_index]
        session.draft = nxt
        session.validation = session.pending_validations[session.draft_index]
        session.current_draft_version = nxt.version
        session.confirmation = Confirmation(
            confirmation_id=f"cnf_{nxt.version:03d}", draft_id=nxt.draft_id,
            draft_version=nxt.version, language=session.confirmation_language)
        session._payload, session._signature = None, None
        session.state, session.outcome = SessionState.CONFIRMATION_READY, "MULTI_INSTRUCTION"
        emit("NEXT_INSTRUCTION_QUEUED", 15, {
            "draft_index": session.draft_index, "multi_total": session.multi_total,
            "remaining": len(session.pending_drafts) - session.draft_index - 1,
            "next_draft_id": nxt.draft_id, "transaction_type": nxt.transaction_type})
        # The nickname belonging to THIS draft, not the single session.nickname_used slot: in a
        # multi-instruction breath the nickname belongs to ONE of the payments and each of the
        # others is a different person, so a shared slot gives the first payment the attribution
        # ("I found Ah Girl as Mei Tan") and silently drops it for the rest.
        _emit_draft_readback(nxt, session.pending_references[session.draft_index],
                             session.pending_schedules[session.draft_index], lang,
                             session.customer_id, emit, say_line,
                             nickname=_nickname_for_index(session, session.draft_index),
                             hold=_identity_hold(session))
        session.nickname_used = None
    return TurnResponse(session=session.to_public(), events=events)


# =====================================================================
