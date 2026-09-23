import json
import re
import sys
from typing import get_args, get_origin, Literal, Union, Optional
from pydantic import BaseModel, ValidationError

# =====================================================================
# 2. Shared typed-agent runtime — BRD §8.4
#    All four agents go through this. Fail closed on any deviation.
# =====================================================================
class AgentUnavailable(Exception):
    """Any timeout, malformed JSON, unknown enum, incomplete field set or schema failure."""


def _log_fallback(agent_hint: str, detail: str, raw: str) -> None:
    """Surface the REAL reason a live agent fell back to mock. The fail-closed path swallows
    the exception, which hid live-mode failures during the hackathon (same gap as the earlier
    'every agent shows mock' mystery). Logging to stderr only — `raw` is the MODEL REPLY, never
    the customer's transcribed speech."""
    snippet = (raw or "")[:400].replace("\n", " ")
    print(f"[AGENT_LIVE_FALLBACK] {agent_hint}: {detail} | raw={snippet!r}",
          file=sys.stderr, flush=True)


# Known synonyms a live model may emit instead of the exact Literal token. deepseek-v4-flash is
# non-deterministic, so each run can spell enums differently ("PROPOSAL" / "ready" / "PROPOSAL_READY";
# "en" / "english" / "EN"; "SGD" / "S$"). The strict contract stays intact for the DETERMINISTIC
# control chain; this ONLY absorbs harmless model variance so a benign spelling never silently drops a
# live agent to mock. Mock replies never pass through here (they call model_validate directly).
_LANG_ALIASES = {
    "EN": {"en", "eng", "english", "en-us", "en_sg", "en-sg", "en_us", "en-gb"},
    "ZH": {"zh", "zho", "chinese", "mandarin", "cmn", "zh-cn", "zh_cn", "zh-tw", "zh_sg",
           "zh-hans", "zh-hant", "zh_sg"},
    "BM": {"bm", "ms", "may", "bahasa", "melayu", "malay", "ms-my", "ms_my", "ms_sg"},
    "MIXED": {"mixed", "mix", "code-switch", "codeswitch", "code switch"},
}
_ENUM_ALIASES = {
    # field-name -> {lowercased-synonym: canonical Literal}
    "status": {
        "proposal": "PROPOSAL_READY", "proposalready": "PROPOSAL_READY", "ready": "PROPOSAL_READY",
        "proposed": "PROPOSAL_READY", "ready_for_confirmation": "PROPOSAL_READY",
        "proposal_for_confirmation": "PROPOSAL_READY", "for_confirmation": "PROPOSAL_READY",
        "awaiting_confirmation": "PROPOSAL_READY",
        "draft": "PROPOSAL_READY", "new": "PROPOSAL_READY", "complete": "PROPOSAL_READY",
        "completed": "PROPOSAL_READY", "ok": "PROPOSAL_READY", "done": "PROPOSAL_READY",
        "created": "PROPOSAL_READY",
        "clarification": "NEEDS_CLARIFICATION", "clarify": "NEEDS_CLARIFICATION",
        "needsclarification": "NEEDS_CLARIFICATION", "needsclear": "NEEDS_CLARIFICATION",
        "needs_clarify": "NEEDS_CLARIFICATION", "ambiguous": "NEEDS_CLARIFICATION",
        "risk": "RISK_DETECTED", "riskdetected": "RISK_DETECTED", "flagged": "RISK_DETECTED",
        "flag": "RISK_DETECTED", "suspicious": "RISK_DETECTED",
    },
    "currency": {
        "sgd": "SGD", "s$": "SGD", "s$": "SGD", "sg": "SGD", "sing": "SGD", "singapore": "SGD",
    },
}


def _literal_tokens(ann) -> list:
    """Collect the string Literal tokens of a field annotation, handling Optional[Literal[...]]."""
    if get_origin(ann) is Literal:
        return [a for a in get_args(ann) if isinstance(a, str)]
    if get_origin(ann) is Union:
        for c in get_args(ann):
            if get_origin(c) is Literal:
                return [a for a in get_args(c) if isinstance(a, str)]
    return []


def _is_str_field(ann) -> bool:
    """True for `str` or `Optional[str]` (Union containing str)."""
    if ann is str:
        return True
    if get_origin(ann) is Union and str in get_args(ann):
        return True
    return False


def _list_element_type(ann):
    """Return the element type of a `List[...]` annotation, else None."""
    if get_origin(ann) is list:
        args = get_args(ann)
        if args:
            return args[0]
    return None


def _describe_literal(v: str, ann) -> Optional[str]:
    """Case-insensitive canonical match for a Literal[str] value, e.g. 'match' -> 'MATCH'."""
    if get_origin(ann) is Literal and all(isinstance(a, str) for a in get_args(ann)):
        up = v.strip().upper()
        for tok in get_args(ann):
            if tok.upper() == up:
                return tok
    return None


def _coerce_str_list(val: list) -> list:
    """risk_flags: List[str] whose elements may arrive as dicts/numbers -> list of strings."""
    out = []
    for item in val:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # Validation agent returned structured flags; keep the meaningful bit as a string.
            out.append(str(item.get("flag") or item.get("detail") or item.get("reason")
                             or json.dumps(item, ensure_ascii=False)))
        else:
            out.append(str(item))
    return out


def _normalize_subfields(item: dict, sub_model: type[BaseModel]) -> None:
    """Coerce nested scalar string / Literal-string fields a model returned as the wrong type
    (e.g. FieldCheck.draft_value: 5000 -> '5000'). Only str-typed sub-fields are touched."""
    for sub_name, sub_field in sub_model.model_fields.items():
        if sub_name not in item or item[sub_name] is None:
            continue
        v = item[sub_name]
        sub_ann = sub_field.annotation
        if _is_str_field(sub_ann) and not isinstance(v, str):
            item[sub_name] = str(v)
        elif isinstance(v, str):
            canonical = _describe_literal(v, sub_ann)
            if canonical is not None:
                item[sub_name] = canonical


def _normalize_obj(obj: dict, model_cls: type[BaseModel]) -> dict:
    """Tolerant pre-validation for live-model replies only. Absorbs harmless spelling variance:
    full language names, status/currency synonyms, whitespace, numeric-string amounts, nested
    FieldCheck scalar coercion, structured risk_flags -> strings, and null lists. Keeps the strict
    schema for the deterministic control chain; mock replies never pass through here."""
    for name, field in model_cls.model_fields.items():
        if name not in obj:
            continue
        val = obj[name]
        ann = field.annotation
        # --- list fields: field_checks (List[FieldCheck]) and risk_flags (List[str]) ---
        elem = _list_element_type(ann)
        if elem is not None and isinstance(val, list):
            if isinstance(elem, type) and issubclass(elem, BaseModel):
                for item in val:
                    if isinstance(item, dict):
                        _normalize_subfields(item, elem)
            elif elem is str:
                obj[name] = _coerce_str_list(val)
            continue
        # risk_flags returned as a bare string instead of a list
        if name == "risk_flags" and isinstance(val, str):
            obj[name] = [val] if val.strip() else []
            continue
        # amount_minor returned as a numeric string ("5000" / "50 dollars") -> int
        if name == "amount_minor" and isinstance(val, str):
            digits = re.sub(r"[^0-9]", "", val)
            if digits:
                try:
                    obj[name] = int(digits)
                except ValueError:
                    pass
            continue
        if not isinstance(val, str):
            continue
        s = val.strip()
        # detected_language: full-name / code synonyms
        if name == "detected_language":
            low = s.lower()
            for canon, aliases in _LANG_ALIASES.items():
                if low == canon.lower() or low in aliases:
                    obj[name] = canon
                    break
            continue
        # status / currency: explicit synonym tables
        alias = _ENUM_ALIASES.get(name, {}).get(s.lower())
        if alias is not None:
            obj[name] = alias
            continue
        # generic Literal / Optional[Literal] case-insensitive match (e.g. 'en' -> 'EN')
        literal_tokens = _literal_tokens(ann)
        if literal_tokens:
            up = s.upper()
            for tok in literal_tokens:
                if tok.upper() == up:
                    obj[name] = tok
                    break
    # Defensive: null -> [] for any list-typed field (models sometimes emit null for an empty list).
    for name, field in model_cls.model_fields.items():
        if get_origin(field.annotation) is list and name in obj and obj[name] is None:
            obj[name] = []
    return obj


def extract_json_object(raw: str) -> dict:
    """Pull the first JSON object out of a model reply. No regex guessing at semantics."""
    if raw is None:
        _log_fallback("extract_json_object", "empty model response", "")
        raise AgentUnavailable("empty model response")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        _log_fallback("extract_json_object", "no JSON object in model response", raw or "")
        raise AgentUnavailable("no JSON object in model response")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        _log_fallback("extract_json_object", f"malformed JSON: {exc}", raw or "")
        raise AgentUnavailable(f"malformed JSON: {exc}") from exc
    if not isinstance(obj, dict):
        _log_fallback("extract_json_object", "model returned a non-object", raw or "")
        raise AgentUnavailable("model returned a non-object")
    return obj


def typed_call(raw: str, model_cls: type[BaseModel]) -> BaseModel:
    """Key presence alone is insufficient — validate enums, required fields, ranges (§8.4)."""
    obj = extract_json_object(raw)
    obj = _normalize_obj(obj, model_cls)
    try:
        return model_cls.model_validate(obj)
    except ValidationError as exc:
        _log_fallback(f"typed_call[{model_cls.__name__}]",
                      f"schema validation failed: {exc.errors()[:3]}", raw or "")
        raise AgentUnavailable(f"schema validation failed for {model_cls.__name__}: {exc.errors()[:3]}") from exc


