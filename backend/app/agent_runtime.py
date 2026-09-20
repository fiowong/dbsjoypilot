import json
import re
from pydantic import BaseModel, ValidationError

# =====================================================================
# 2. Shared typed-agent runtime — BRD §8.4
#    All four agents go through this. Fail closed on any deviation.
# =====================================================================
class AgentUnavailable(Exception):
    """Any timeout, malformed JSON, unknown enum, incomplete field set or schema failure."""


def extract_json_object(raw: str) -> dict:
    """Pull the first JSON object out of a model reply. No regex guessing at semantics."""
    if raw is None:
        raise AgentUnavailable("empty model response")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AgentUnavailable("no JSON object in model response")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AgentUnavailable(f"malformed JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise AgentUnavailable("model returned a non-object")
    return obj


def typed_call(raw: str, model_cls: type[BaseModel]) -> BaseModel:
    """Key presence alone is insufficient — validate enums, required fields, ranges (§8.4)."""
    obj = extract_json_object(raw)
    try:
        return model_cls.model_validate(obj)
    except ValidationError as exc:
        raise AgentUnavailable(f"schema validation failed for {model_cls.__name__}: {exc.errors()[:3]}") from exc


