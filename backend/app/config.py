import os
from datetime import datetime, timezone

try:
    # Loads a .env file (if present) in the current working directory into
    # os.environ, so `export`-ing variables by hand is a one-time setup, not
    # a per-terminal-session chore. Safe if python-dotenv isn't installed —
    # falls back to whatever's already in the real environment.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================
# 0. Configuration — every threshold is configuration, never prompt text (§8.6.4)
# =====================================================================
class Settings:
    PROMPT_VERSION = "aj-2026-09-22e"
    SCHEMA_VERSION = "aj-schema-1.0"

    INTENT_CONFIDENCE_THRESHOLD = 0.75      # §8.6.4
    AGENT_TEMPERATURE = 0.0                 # §8.1
    AGENT_TIMEOUT_S = 30.0                  # raised: deepseek endpoint occasionally slow; 30s ceiling, not a typical latency
    AGENT_MAX_RETRIES = 3                    # §8.1 — 4 attempts total with exponential backoff + 429 Retry-After (upstream is flaky)
    ASR_AMBIGUITY_THRESHOLD = 0.85

    # Personalization Agent (§ observed-friction, not speech-pattern inference).
    # Each weight is individually justified — see agents.py PERSONALIZATION_SYSTEM_PROMPT
    # and the developer_reason on PersonalizationDecision for the audit-trail rationale.
    PERSONALIZATION_THRESHOLD = 2
    PERSONALIZATION_WEIGHTS = {
        "repeat_field_rejection": 2,   # the SAME field rejected twice — the first "no" is normal use
        "repeat_clarification": 2,     # the SAME clarification point raised twice
        "stalled_intent": 1,           # two consecutive turns that failed to resolve at all
    }

    # §8.10 — synthetic demonstration limits, never real DBS limits
    DEMO_MAX_TRANSFER_MINOR = 1_000_000     # SGD 10,000.00
    STEP_UP_HIGH_VALUE_MINOR = 500_000      # SGD 5,000.00 — transfers above this (and at/below
                                           # the synthetic reject limit) require extra verification
                                           # via STEP_UP; the reject ceiling is DEMO_MAX_TRANSFER_MINOR
                                           # (SGD 10,000). Repurposed from COOLING_OFF_HIGH_VALUE_MINOR,
                                           # which was defined but never referenced.
    VELOCITY_MAX_DRAFTS_PER_SESSION = 5

    SIGNING_SECRET = os.getenv("AUNTIEJOY_SIGNING_SECRET", "demo-only-not-a-production-hsm")
    PAYLOAD_TTL_SECONDS = 120
    DB_PATH = os.getenv("AUNTIEJOY_DB", "auntiejoy_audit.db")

    LLM_API_KEY = os.getenv("HUNYUAN_API_KEY") or os.getenv("SILICONFLOW_API_KEY") or ""
    LLM_BASE_URL = os.getenv("AUNTIEJOY_LLM_BASE", "https://api.siliconflow.cn/v1")
    LLM_MODEL = os.getenv("AUNTIEJOY_LLM_MODEL", "tencent/Hunyuan-A13B-Instruct")
    LIVE_LLM = bool(LLM_API_KEY)

    # ---- voice pipeline: the ears (ASR) and the mouth (TTS) ----
    # Both are PROVIDERS. Neither decides anything — AuntieJoy's orchestrator does.
    ASR_SECRET_ID = os.getenv("TENCENTCLOUD_SECRET_ID", "")
    ASR_SECRET_KEY = os.getenv("TENCENTCLOUD_SECRET_KEY", "")
    # Required: the realtime ASR URL puts the AppID in its path.
    ASR_APPID = os.getenv("TENCENTCLOUD_APPID", "")
    ASR_REGION = os.getenv("TENCENTCLOUD_REGION", "ap-singapore")
    ASR_LANGUAGE = os.getenv("AUNTIEJOY_ASR_LANGUAGE", "en")   # en | zh | bm
    LIVE_ASR = bool(ASR_SECRET_ID and ASR_SECRET_KEY and ASR_APPID)

    TTS_API_KEY = os.getenv("CARTESIA_API_KEY", "")
    TTS_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3-2026-01-12")
    TTS_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "")
    LIVE_TTS = bool(TTS_API_KEY)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(minor: int) -> str:
    return f"SGD {minor / 100:,.2f}"


