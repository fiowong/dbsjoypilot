import os
from datetime import datetime, timezone

try:
    # Loads a .env file (if present) in the current working directory into
    # os.environ, so `export`-ing variables by hand is a one-time setup, not
    # a per-terminal-session chore. Safe if python-dotenv isn't installed,
    # falls back to whatever's already in the real environment.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================
# 0. Configuration, every threshold is configuration, never prompt text (§8.6.4)
# =====================================================================
class Settings:
    PROMPT_VERSION = "aj-2026-09-22e"
    SCHEMA_VERSION = "aj-schema-1.0"

    INTENT_CONFIDENCE_THRESHOLD = 0.75      # §8.6.4
    AGENT_TEMPERATURE = 0.0                 # §8.1
    # 8s, not 30s. This is the ceiling for ONE upstream attempt, so a stalled provider now
    # costs 8s before that agent fails closed to its deterministic mock instead of 30s. On
    # stage the old 30s ceiling froze the customer view for over two minutes on one hung call.
    AGENT_TIMEOUT_S = 8.0
    AGENT_MAX_RETRIES = 3                    # §8.1, 4 attempts total with exponential backoff + 429 Retry-After (upstream is flaky)
    # The whole turn's live-model budget. Once it is spent, every REMAINING agent in this
    # turn skips the upstream entirely and uses its deterministic mock, so a slow provider
    # can never stack five slow calls into a frozen demo. The customer still gets an answer,
    # just a deterministic one, and the audit records that the budget ran out.
    # 15s, deliberately WELL UNDER the frontend's 25s request timeout, so the backend is the
    # thing that gives up first and the browser is only ever a backstop, never the decider.
    TURN_DEADLINE_S = 15.0
    ASR_AMBIGUITY_THRESHOLD = 0.85

    # Personalization Agent (§ observed-friction, not speech-pattern inference).
    # Each weight is individually justified, see agents.py PERSONALIZATION_SYSTEM_PROMPT
    # and the developer_reason on PersonalizationDecision for the audit-trail rationale.
    PERSONALIZATION_THRESHOLD = 2
    PERSONALIZATION_WEIGHTS = {
        "repeat_field_rejection": 2,   # the SAME field rejected twice, the first "no" is normal use
        "repeat_clarification": 2,     # the SAME clarification point raised twice
        "stalled_intent": 1,           # two consecutive turns that failed to resolve at all
    }

    # §8.10, synthetic demonstration limits, never real DBS limits
    DEMO_MAX_TRANSFER_MINOR = 1_000_000     # SGD 10,000.00
    STEP_UP_HIGH_VALUE_MINOR = 500_000      # SGD 5,000.00, transfers above this (and at/below
                                           # the synthetic reject limit) require extra verification
                                           # via STEP_UP; the reject ceiling is DEMO_MAX_TRANSFER_MINOR
                                           # (SGD 10,000). Repurposed from COOLING_OFF_HIGH_VALUE_MINOR,
                                           # which was defined but never referenced.
    VELOCITY_MAX_DRAFTS_PER_SESSION = 5

    SIGNING_SECRET = os.getenv("AUNTIEJOY_SIGNING_SECRET", "demo-only-not-a-production-hsm")
    PAYLOAD_TTL_SECONDS = 120
    DB_PATH = os.getenv("AUNTIEJOY_DB", "auntiejoy_audit.db")
    # The trusted-repository database: customers, their accounts, saved payees, billers,
    # cards and their own accounts. Deliberately a SEPARATE file from the audit log, for the
    # same reason nicknames.json is: the audit is hash-chained and append-only, while this is
    # the authoritative master data the resolver reads on every turn. Override in tests so a
    # run never touches the real file.
    REPO_DB_PATH = os.getenv("AUNTIEJOY_REPO_DB", "auntiejoy_repo.db")
    # Nicknames the customer taught us ("ah girl" -> a beneficiary_id). Deliberately a SEPARATE
    # file from the audit log: the audit is hash-chained and append-only, while this is mutable
    # customer preference. Override in tests so a run never touches the real file.
    NICKNAMES_PATH = os.getenv("AUNTIEJOY_NICKNAMES", "nicknames.json")

    LLM_API_KEY = os.getenv("HUNYUAN_API_KEY") or os.getenv("SILICONFLOW_API_KEY") or ""
    LLM_BASE_URL = os.getenv("AUNTIEJOY_LLM_BASE", "https://api.siliconflow.cn/v1")
    LLM_MODEL = os.getenv("AUNTIEJOY_LLM_MODEL", "tencent/Hunyuan-A13B-Instruct")
    LIVE_LLM = bool(LLM_API_KEY)

    # ---- voice pipeline: the ears (ASR) and the mouth (TTS) ----
    # Both are PROVIDERS. Neither decides anything, AuntieJoy's orchestrator does.
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
    # One voice per conversation language, so Auntie never reads Mandarin in an English
    # timbre. Every one of these is OPTIONAL: a language with no voice of its own falls
    # back to the shared CARTESIA_VOICE_ID, so a single-voice setup keeps working exactly
    # as it did before and an operator can adopt the feature one language at a time.
    TTS_VOICE_ID_EN = os.getenv("CARTESIA_VOICE_ID_EN", "")
    TTS_VOICE_ID_ZH = os.getenv("CARTESIA_VOICE_ID_ZH", "")
    TTS_VOICE_ID_BM = os.getenv("CARTESIA_VOICE_ID_BM", "")
    LIVE_TTS = bool(TTS_API_KEY)

    @classmethod
    def tts_voice_for(cls, ui_lang: str) -> str:
        """The Cartesia voice id to speak `ui_lang` (EN / ZH / BM) in.

        Per-language when one is configured, otherwise the single shared voice. Never
        raises and never invents an id: an unset language degrades to the shared voice,
        and "" means genuinely unconfigured, which the caller reports as unavailable
        rather than silently speaking in the wrong tongue.
        """
        per_language = {"EN": cls.TTS_VOICE_ID_EN,
                        "ZH": cls.TTS_VOICE_ID_ZH,
                        "BM": cls.TTS_VOICE_ID_BM}
        return per_language.get((ui_lang or "EN").strip().upper()) or cls.TTS_VOICE_ID

    @classmethod
    def tts_voice_configured(cls) -> bool:
        """True when at least one voice id exists, so /health and /tts can gate on it."""
        return bool(cls.TTS_VOICE_ID or cls.TTS_VOICE_ID_EN
                    or cls.TTS_VOICE_ID_ZH or cls.TTS_VOICE_ID_BM)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Spoken currency symbols. SGD and USD are the only two the demo uses; the rest are here so a
# new one is a one-line addition rather than a silent fall-through to SGD.
CURRENCY_SYMBOLS = {"SGD": "S$", "USD": "US$", "MYR": "RM", "CNY": "¥", "HKD": "HK$"}


def money(minor: int, currency: str = "SGD") -> str:
    """Amount as the customer hears it.

    "SGD 50.00" is a system identifier; nobody says it out loud. The customer hears
    "S$50.00", so the ISO code is mapped to its spoken symbol here, the one place an
    amount becomes text. Thousands are separated and the minor units are always shown
    ("S$1,000.00"), because a bare "$1000" is easy to misread on a phone.

    An unrecognised currency degrades to the code plus a space rather than silently
    rendering as Singapore dollars, so a future currency can never be mislabelled as SGD.
    """
    code = (currency or "SGD").upper()
    symbol = CURRENCY_SYMBOLS.get(code)
    prefix = symbol if symbol else f"{code} "
    return f"{prefix}{minor / 100:,.2f}"


