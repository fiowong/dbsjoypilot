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

def _clamped_float(raw, low, high, default):
    """Parse `raw` as a float and keep it inside [low, high], else return `default`.

    Used for provider knobs that are tuned by ear (speaking rate). A typo in .env must
    degrade to the safe default, not raise at import time and take the whole API down.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:                       # NaN
        return default
    return max(low, min(high, value))


# =====================================================================
# 0. Configuration, every threshold is configuration, never prompt text (§8.6.4)
# =====================================================================
class Settings:
    PROMPT_VERSION = "aj-2026-09-22e"
    SCHEMA_VERSION = "aj-schema-1.0"

    INTENT_CONFIDENCE_THRESHOLD = 0.75      # §8.6.4
    AGENT_TEMPERATURE = 0.0                 # §8.1
    # Ceiling for ONE upstream attempt, so a stalled provider costs this much before that agent
    # fails closed to its deterministic mock. On stage the old 30s ceiling froze the customer view
    # for over two minutes on one hung call, which is why it is a ceiling and not "just wait".
    #
    # It is NOT one number for all five agents. Measured against deepseek-v4-flash-202605 the
    # agents differ by an order of magnitude, and the single 8s ceiling sat BELOW the Validation
    # agent's own normal latency, so that agent was killed mid-answer, retried, and fell back to
    # its mock while the retries ate the rest of the turn. Per-agent ceilings, each measured:
    #   guardrails      2.0-2.5s    intent  1.9-3.0s    payment  5.0-7.3s
    #   personalization 1.6-1.8s    validation 7.8-17.0s  <- five field checks, heaviest reply
    # AGENT_TIMEOUT_S remains the fallback for any agent not named here.
    AGENT_TIMEOUT_S = 8.0
    # payment and validation get real headroom because their replies are the largest. A
    # multi-instruction breath ("move a grand to my joint account, then send fifty to Ah Girl")
    # measured 16-18s on the Payment agent alone, so a 12s ceiling killed it, retried, and left
    # the rest of the turn with no budget. Both ceilings still sit under TURN_DEADLINE_S, so a
    # genuinely hung call is bounded by the turn and the remaining agents fail closed as before.
    AGENT_TIMEOUTS = {
        "guardrails": 10.0,
        "intent": 10.0,
        "payment": 20.0,
        "validation": 20.0,
        "personalization": 8.0,
    }
    AGENT_MAX_RETRIES = 3                    # §8.1, 4 attempts total with exponential backoff + 429 Retry-After (upstream is flaky)
    # The whole turn's live-model budget. Once it is spent, every REMAINING agent in this
    # turn skips the upstream entirely and uses its deterministic mock, so a slow provider
    # can never stack five slow calls into a frozen demo. The customer still gets an answer,
    # just a deterministic one, and the audit records that the budget ran out.
    # Measured, not guessed. deepseek-v4-flash-202605 answers an agent prompt in ~3-4s, and a
    # full happy-path turn makes FIVE calls (personalization, guardrails, intent, payment,
    # validation). 15s fit the first three and then starved validation, which showed up in the
    # demo as "the last agent says mock". 22s covers all five with headroom; it stays under the
    # frontend's REQUEST_TIMEOUT_MS so the backend is still the thing that gives up first and
    # the browser is only ever a backstop, never the decider.
    TURN_DEADLINE_S = 30.0
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

    # ---- outbound proxy handling for the voice providers ----
    # Both the ASR WebSocket client and the TTS HTTP client read HTTP(S)_PROXY from the
    # environment by default. That default is RIGHT on a corporate network where egress must
    # go through a proxy, and harmless on a plain cloud VM where nothing is set. It is only
    # wrong on a dev machine that happens to have a stray proxy exported, where the symptom
    # is a confusing connect failure rather than an obvious one.
    #
    # So the default is to RESPECT the environment (never silently override an operator's
    # network policy), and AUNTIEJOY_NO_PROXY=1 is the explicit escape hatch for that dev
    # case. tools/asr_probe.py clears the proxy vars unconditionally instead, because a
    # diagnostic should always test the direct path.
    NO_PROXY = os.getenv("AUNTIEJOY_NO_PROXY", "").strip().lower() in ("1", "true", "yes", "on")

    # ---- outbound network policy for the Tencent endpoints ----
    # Both HTTP clients in this app default to *inheriting the ambient proxy*, and both defaults
    # are a trap here:
    #   * httpx.AsyncClient(trust_env=True) reads HTTP_PROXY / HTTPS_PROXY / ALL_PROXY.
    #   * websockets 17 defaults to proxy=True, which means exactly the same thing.
    # On a corporate network that is correct. For this demo it is not: a stray or local proxy
    # (a sandbox sidecar, a container, a forgotten `export http_proxy=...` in a shell profile)
    # swallows the call, and the symptom is a generic connect error that reads like bad
    # credentials or a wrong region. The Tencent ASR and TTS endpoints are fixed public hosts,
    # so we go DIRECT by default and make the standard behaviour opt-in:
    #   AUNTIEJOY_USE_ENV_PROXY=1   -> honour HTTP_PROXY / HTTPS_PROXY / NO_PROXY as usual
    # Reported in /health so a proxied demo is never a silent surprise.
    USE_ENV_PROXY = os.getenv("AUNTIEJOY_USE_ENV_PROXY", "").strip().lower() in (
        "1", "true", "yes", "on")

    # ---- payment rail: what "execute" actually means (see rails.py) ----
    # mock = the deterministic in-memory ledger (default; the 28-scenario suite runs on it).
    # dbs  = the DBS Fund Transfer API contract: builds and returns the documented request and
    #        response shapes. An unknown value fails CLOSED to mock, never to a live rail.
    PAYMENT_RAIL = (os.getenv("AUNTIEJOY_PAYMENT_RAIL", "mock") or "mock").strip().lower()
    RAIL_DB_PATH = os.getenv("AUNTIEJOY_RAIL_DB", "auntiejoy_rail.db")
    # DBS Fund Transfer API. The sandbox host is published; the production host is not, so the
    # base URL is configuration rather than a constant.
    DBS_BASE_URL = os.getenv("DBS_BASE_URL", "https://www.dbs.com/sandbox/api/sg/v1")
    DBS_CLIENT_ID = os.getenv("DBS_CLIENT_ID", "")
    # Obtained through the OAuth2 accessCode (consent) flow, which is NOT publicly documented.
    # Held here only; never logged, never written to the audit, never sent to the browser.
    DBS_ACCESS_TOKEN = os.getenv("DBS_ACCESS_TOKEN", "")
    DBS_PARTY_ID = os.getenv("DBS_PARTY_ID", "")
    # Pinned, not omitted: unset means "latest", and Version History lists only 1.0 even though
    # an example header shows 1.2. Pinning makes a version change a deliberate act.
    DBS_ACCEPT_VERSION = os.getenv("DBS_ACCEPT_VERSION", "1.0")
    # FAQ Q1: transfers run from the SGD wallet of a multi-currency account. USD is
    # undocumented there, so it is excluded by default rather than assumed to work.
    DBS_ALLOWED_CURRENCIES = {
        c.strip().upper() for c in os.getenv("DBS_ALLOWED_CURRENCIES", "SGD").split(",") if c.strip()
    } or {"SGD"}
    # transferType applies to the payee/adhoc operations only (never to toOwnAccounts).
    DBS_TRANSFER_TYPE = os.getenv("DBS_TRANSFER_TYPE", "INSTANT")
    # purpose is the intra/inter-bank discriminator and is "not applicable for intra-bank
    # transfers", so it is attached only when the destination is another bank.
    DBS_PURPOSE_INTERBANK = os.getenv("DBS_PURPOSE_INTERBANK", "PAYMENT")
    # DBS publishes NO throttling numbers (the Throttling section is empty), so this is a
    # deliberately conservative client-side ceiling rather than a documented limit.
    DBS_RATE_LIMIT_PER_SEC = _clamped_float(
        os.getenv("DBS_RATE_LIMIT_PER_SEC", "5"), 0.1, 100.0, 5.0)
    # FAQ Q3 (max scheduling period) is blank in the DBS docs; 0 means "not enforced", and any
    # positive value is a deliberate local policy rather than a quoted DBS rule.
    DBS_MAX_SCHEDULE_DAYS = int(_clamped_float(
        os.getenv("DBS_MAX_SCHEDULE_DAYS", "0"), 0, 3650, 0))

    # ---- TTS provider: Tencent Cloud Text To Speech (TextToVoice, international site) ----
    # Same Tencent Cloud account as the ears, so the credentials are shared rather than
    # duplicated. TextToVoice is a signed HTTPS JSON API (TC3-HMAC-SHA256), NOT a
    # WebSocket: the international site exposes no streaming TTS endpoint, so a line is
    # synthesised in full and then relayed to the browser (see tts.py).
    TTS_SERVICE = "tts"
    TTS_API_VERSION = "2019-08-23"
    TTS_ACTION = "TextToVoice"
    # Singapore by requirement: the demo runs from Singapore and this is the lowest-latency
    # endpoint. The region also travels in the X-TC-Region header.
    TTS_REGION = os.getenv("TENCENTCLOUD_TTS_REGION") or ASR_REGION
    TTS_ENDPOINT = os.getenv("TENCENTCLOUD_TTS_ENDPOINT") or f"tts.{TTS_REGION}.tencentcloudapi.com"
    TTS_MODEL_TYPE = 1                       # 1 = the default (and only) model
    # ---- One voice per conversation language: the "Chinese premium, southern" read ----
    #
    # The brief is a warm, soft, female voice with a southern-Chinese character, applied
    # consistently across Chinese, English and Malay. Two provider facts decide the ids, and
    # both were established by probing the live API rather than from a catalogue page:
    #
    #   1. A Chinese voice CAN read English (verified: every premium Chinese voice returns
    #      audio for an English sentence, and an English ASR transcribes it back at the same
    #      accuracy as the native English voice). This is how English gets a genuine Chinese
    #      accent -- it is a Chinese voice model, not a filter applied to an English one.
    #      The reverse does NOT hold: the English voices (101050/101051) fail on Chinese text
    #      with InvalidParameter.InvalidText. That asymmetry is what _resolve_voice_and_model
    #      in tts.py is built around.
    #
    #   2. A dialect voice can read Mandarin correctly, but only SOME of them can read Malay.
    #      Verified by speaking a line and transcribing it back with our own ASR:
    #        * 101019 (zhitong, Cantonese) reads Mandarin correctly (84% char match, the only
    #          difference being a dropped polite 您). It is the one premium voice the
    #          international catalogue documents as southern, and it is female at ~246 Hz.
    #        * 101019 FAILS Malay badly -- an 82-85% match for every other voice collapses to
    #          15% ("Battle."). 101012 (zhidan) fails the same way. So BM must NOT use it.
    #        * The Mandarin voices read Malay at 82-87%, i.e. as well as the English voice
    #          does. BM therefore uses a Mandarin voice deliberately, not as a fallback.
    #
    # Result: ZH and EN share the southern voice so the accent is consistent; BM uses a
    # Mandarin voice because the southern one cannot pronounce it. Values are Tencent
    # VoiceType integers carried as strings (that is what env vars are); tts.py parses them
    # and reports an unparseable one rather than guessing.
    #   101019 = zhitong (Cantonese, premium, female)  <- southern character
    #   101003 = zhimei  (Mandarin,  premium, female)  <- reads Malay acceptably
    TTS_VOICE_ID = os.getenv("AUNTIEJOY_TTS_VOICE", "")
    TTS_VOICE_ID_EN = os.getenv("AUNTIEJOY_TTS_VOICE_EN", "101019")
    TTS_VOICE_ID_ZH = os.getenv("AUNTIEJOY_TTS_VOICE_ZH", "101019")
    TTS_VOICE_ID_BM = os.getenv("AUNTIEJOY_TTS_VOICE_BM", "101003")
    # ---- Prosody: the "soft" half of the brief ----
    # EmotionCategory is documented as taking effect only on "multi-emotion" voices. Probed:
    # the premium voices accept every value, and -- the part that matters -- the parameter
    # CHANGES THE AUDIO. Control: three identical requests are byte-identical, three requests
    # with emotion=neutral are byte-identical to each other but differ from a request with no
    # emotion at all, and peaceful/sajiao each differ from both. So it is a real prosody
    # control, not a silently ignored field. Values come from the documented set
    # (neutral, peaceful, sajiao, story, news, ...). "peaceful" is the calm, unhurried read.
    # Blank = send nothing, which is NOT the same as "neutral" (see the control above).
    TTS_EMOTION = (os.getenv("AUNTIEJOY_TTS_EMOTION", "peaceful") or "").strip().lower()
    # [50, 200], default 100. Only meaningful when an emotion is set.
    TTS_EMOTION_INTENSITY = int(_clamped_float(
        os.getenv("AUNTIEJOY_TTS_EMOTION_INTENSITY", "100"), 50, 200, 100))
    # Speaking rate, in Tencent's OWN units: [-2, 6] where 0 = 1.0x natural pace.
    #   -2 = 0.6x   -1 = 0.8x   0 = 1.0x   1 = 1.2x   2 = 1.5x   6 = 2.5x
    # One decimal place is allowed for finer steps. An out-of-range or non-numeric value
    # falls back to 0 (natural) rather than breaking speech.
    TTS_SPEED = _clamped_float(os.getenv("AUNTIEJOY_TTS_SPEED", "0"), -2.0, 6.0, 0.0)
    # Volume, [0, 10] with 0 = normal. There is no mute option.
    TTS_VOLUME = _clamped_float(os.getenv("AUNTIEJOY_TTS_VOLUME", "0"), 0.0, 10.0, 0.0)
    # 16000 or 8000 only. 16k matches what the ears capture and what the browser decodes.
    TTS_SAMPLE_RATE = int(_clamped_float(
        os.getenv("AUNTIEJOY_TTS_SAMPLE_RATE", "16000"), 8000, 16000, 16000))
    # The silence inserted BETWEEN the pieces of a long line that had to be split. It exists
    # because concatenating two clips with zero gap makes the second sentence start on top of
    # the first one's tail, which reads as a stumble rather than a pause. Tuned short so the
    # line still sounds like one utterance: too long and she sounds like she has finished.
    TTS_PIECE_GAP_MS = int(_clamped_float(
        os.getenv("AUNTIEJOY_TTS_PIECE_GAP_MS", "120"), 0, 1000, 120))
    # TextToVoice caps ONE request at 150 Chinese characters or 500 letters; tts.py splits
    # longer lines. Kept here so the ceiling is configuration, not a magic number.
    TTS_MAX_CJK_CHARS = 150
    TTS_MAX_LATIN_CHARS = 500

    # =====================================================================
    # ---- The mouth, provider 1 of 2: CARTESIA (the default) ----
    # =====================================================================
    # Cartesia Sonic is the DEFAULT mouth. The Tencent mouths above/below are still fully
    # wired and are what the UI's "TRTC Mode" toggle switches to at runtime, so the demo can
    # show either engine without a restart. See tts_provider() / set_tts_provider().
    #
    # Contract, verified against the live API before this was written (a 1-character
    # transcript returns 200 with real PCM; a deliberately wrong key returns 401):
    #   * POST https://api.cartesia.ai/tts/bytes
    #   * Headers: X-API-Key, Cartesia-Version, Content-Type: application/json
    #   * Body:    {model_id, transcript, voice:{mode:"id",id:<uuid>}, language,
    #               output_format:{container:"raw", encoding:"pcm_s16le", sample_rate},
    #               generation_config:{speed}}
    #   * Response: RAW PCM bytes, no container. Same shape the browser already decodes, so
    #     no WAV stripping is needed and the /tts relay is unchanged.
    #
    # WHY THIS PROVIDER IS A REAL UPGRADE, not just a different vendor: Sonic speaks 44
    # languages INCLUDING MALAY (ms). Tencent's international catalogue has no Malay voice at
    # all, which is why the Tencent path has to read Malay with a borrowed Mandarin voice and
    # reports tts_voice_is_fallback("BM") == true. Here BM has a genuine Malay voice, so that
    # fallback flag is false. The three voices below were each verified to return audio:
    #   EN 6eb8965c-... (Sheryl)   ZH cfce9402-... (Jing)   BM 83604597-... (Aisyah)
    CARTESIA_ENDPOINT = os.getenv("CARTESIA_ENDPOINT", "api.cartesia.ai")
    CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
    # The API VERSION header. Deliberately separate from the model id: a model id is a
    # snapshot of the voice, a version is a snapshot of the HTTP contract, and confusing the
    # two is an easy way to get a 400 that reads like a bad voice.
    CARTESIA_API_VERSION = os.getenv("CARTESIA_API_VERSION", "2025-04-16")
    CARTESIA_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3-2026-01-12")
    # SPEED IS A FLOAT MULTIPLIER HERE, NOT TENCENT'S INTEGER. Tencent uses [-2, 6] where 0
    # means 1.0x; Cartesia uses a plain ratio and sonic accepts [0.6, 1.5]. Reusing the
    # Tencent value verbatim would be silently wrong (a Tencent "0" would mean 0.6x, i.e.
    # badly slurred), so the two are separate settings with separate ranges and defaults.
    CARTESIA_SPEED = _clamped_float(os.getenv("CARTESIA_SPEED", "1.0"), 0.6, 1.5, 1.0)
    CARTESIA_VOICE_ID_EN = os.getenv("CARTESIA_VOICE_ID_EN", "")
    CARTESIA_VOICE_ID_ZH = os.getenv("CARTESIA_VOICE_ID_ZH", "")
    CARTESIA_VOICE_ID_BM = os.getenv("CARTESIA_VOICE_ID_BM", "")
    # Sonic has no 150-character ceiling of the kind TextToVoice imposes, so a whole
    # interface line goes in ONE request and keeps its natural prosody across the sentence.
    # The cap below is a sanity bound, not a provider limit.
    CARTESIA_MAX_CHARS = 3000
    CARTESIA_SAMPLE_RATE = 16000

    # ---- TRTC streaming TTS (TextToSpeechSSE) ----
    # A SECOND, OPTIONAL mouth alongside the one above. It does not replace TextToVoice, it
    # fixes the one thing that provider structurally cannot do: stream. Verified against
    # /document/api/647/80009 (TextToSpeechSSE) and /document/api/647/36760 (data types):
    #   * Service "trtc", action "TextToSpeechSSE", API version 2019-07-22.
    #   * Region is ap-singapore ONLY.
    #   * The documented examples mandate host trtc.ai.tencentcloudapi.com and warn the call
    #     FAILS against any other domain -- the endpoint is therefore its own setting rather
    #     than being derived from TTS_REGION, because the "intl" convention used elsewhere in
    #     this file would produce the wrong host here.
    #   * Response is SSE: `data:` lines carrying {Type:"chunk", Audio:<base64>, Seq, IsEnd}
    #     and terminated by the chunk whose IsEnd is true; mid-stream failures arrive as
    #     {Type:"error", Error:{Code,Message}}, while a parameter error BEFORE synthesis is a
    #     plain JSON {Response:{Error:{...}}} body detectable via the Content-Type header.
    #   * One request is capped at Text length [1, 255] — much tighter than TextToVoice's
    #     150-CJK/500-letter budgets, hence its own cap below.
    #   * 20 requests/second.
    # It needs the TRTC SdkAppId and a TRTC VoiceId STRING ("v-female-..." style), which is a
    # different identifier space from the integer VoiceType used above. It reuses the same
    # Tencent Cloud SecretId/SecretKey and the same TC3 signature.
    TRTC_SERVICE = "trtc"
    TRTC_TTS_API_VERSION = "2019-07-22"
    TRTC_TTS_ACTION = "TextToSpeechSSE"
    TRTC_TTS_MODEL = "flow_01_turbo"         # the only supported value today
    TRTC_SDK_APP_ID = os.getenv("TRTC_SDK_APP_ID", "")
    TRTC_TTS_ENDPOINT = os.getenv("TRTC_TTS_ENDPOINT", "trtc.ai.tencentcloudapi.com")
    # ap-singapore is the ONLY region this action accepts, so the value is pinned rather than
    # inherited from TENCENTCLOUD_REGION: inheriting would let an operator move their other
    # Tencent calls elsewhere and silently break this one.
    TRTC_TTS_REGION = "ap-singapore"
    # One SSE request carries [1, 255] characters. The catalogue's read-backs fit comfortably;
    # the cap is still enforced per piece so a pathological line cannot 400 mid-line.
    TRTC_TTS_MAX_CHARS = 255
    # Voice ids are TRTC tintinnabuli ids, e.g. v-female-R2s4N9qJ. None are shipped as
    # defaults because they differ per application and guessing one yields a
    # InvalidParameter.VoiceId error at runtime rather than silence.
    TRTC_TTS_VOICE_EN = os.getenv("TRTC_TTS_VOICE_EN", "")
    TRTC_TTS_VOICE_ZH = os.getenv("TRTC_TTS_VOICE_ZH", "")
    # No Malay voice exists here either (the model supports zh/en/yue/ja/ko), so Bahasa
    # Melayu keeps riding the English voice and tts.py reports the substitution exactly as
    # it does for TextToVoice.
    TRTC_TTS_VOICE_BM = os.getenv("TRTC_TTS_VOICE_BM", "")
    # ---- WHICH MOUTH SPEAKS: the runtime provider selector ----
    # Two providers, one job, switchable WITHOUT a restart. The UI's "TRTC Mode" toggle calls
    # POST /tts/provider, which calls set_tts_provider() below; nothing else has to change
    # because every reader (tts.py, main.py, /health) reads Settings.TTS_PROVIDER at CALL
    # time rather than caching it at import.
    #
    # Canonical names, and the only three accepted:
    #   "cartesia"          -> Cartesia Sonic, POST api.cartesia.ai/tts/bytes. THE DEFAULT.
    #   "texttovoice"       -> Tencent TextToVoice, synchronous. TRTC Mode's target.
    #   "texttospeechsse"   -> Tencent TRTC streaming. TRTC Mode's target when configured.
    # The two Tencent names are both "TRTC Mode" from the customer's point of view: the toggle
    # means "use Tencent", and which Tencent mouth answers is an internal detail (streaming
    # when it is configured, synchronous otherwise).
    TTS_PROVIDER_DEFAULT = (os.getenv("AUNTIEJOY_TTS_PROVIDER", "cartesia")
                            or "cartesia").strip().lower()
    # The live value. Mutated at runtime by set_tts_provider(); starts at the configured
    # default so behaviour with no UI interaction is exactly what the env asks for.
    TTS_PROVIDER = TTS_PROVIDER_DEFAULT

    # Every provider this build understands. Anything else is rejected by set_tts_provider()
    # rather than being stored and failing later somewhere less obvious.
    TTS_PROVIDERS = ("cartesia", "texttovoice", "texttospeechsse")
    # The Tencent names, so "is this the Tencent mouth?" is asked in ONE place instead of a
    # literal repeated in three files.
    TENCENT_PROVIDERS = ("texttovoice", "texttospeechsse")

    # ---- The reported fallback (never a silent one) ----
    # Cartesia is the default, so if its key is missing, the account is out of credit, or the
    # service is down, the mouth would simply go quiet -- the worst possible failure in a
    # voice-first demo. Instead the request falls back to the Tencent mouth and RECORDS that
    # it did, and /health plus the UI's voice-engine row say so in plain language. A fallback
    # the presenter cannot see is worse than an error they can.
    TTS_FALLBACK_ENABLED = (os.getenv("AUNTIEJOY_TTS_FALLBACK", "1") or "1").strip().lower() \
        not in ("0", "false", "no", "off")
    # Set by tts.py when a fallback actually happened; read by /health.
    TTS_FALLBACK_ACTIVE = False
    TTS_FALLBACK_REASON = ""
    # WHO failed and WHO answered instead. Recorded rather than inferred: the two providers
    # are not the only pair in play (a Tencent selection can itself fall back from the TRTC
    # streaming mouth to TextToVoice), so "the other one" is not a safe deduction and a UI
    # that guessed would confidently name the engine that just failed as the one speaking.
    TTS_FALLBACK_FROM = ""
    TTS_FALLBACK_TO = ""

    # Runtime truth for /health: True once a line has actually been spoken by the streaming
    # mouth, False once it fell back. Class-level so every module reads the same value, and
    # set from tts.py where the decision is actually made.
    TTS_STREAMING_ACTIVE = False

    @classmethod
    def set_tts_provider(cls, name: str) -> str:
        """Switch the active mouth. Returns the name now in force.

        Raises ValueError on an unknown name rather than silently keeping the old one: a
        toggle that appears to work but changes nothing is the failure mode worth avoiding
        most here. Clearing the fallback state on every switch is deliberate -- the reason
        belongs to the provider that failed, and carrying it across a switch would report a
        stale cause for a provider that has not been tried yet.
        """
        want = (name or "").strip().lower()
        # Accept the UI's vocabulary: the toggle sends "tencent" and the backend maps it to
        # whichever Tencent mouth is actually usable, so the frontend never has to know
        # whether streaming is configured.
        if want == "tencent":
            want = "texttospeechsse" if cls.trtc_tts_configured() else "texttovoice"
        if want not in cls.TTS_PROVIDERS:
            raise ValueError(
                f"unknown TTS provider {name!r}; expected one of {cls.TTS_PROVIDERS} "
                f"or 'tencent'")
        cls.TTS_PROVIDER = want
        cls.clear_tts_fallback()
        cls.TTS_STREAMING_ACTIVE = False
        return want

    @classmethod
    def note_tts_fallback(cls, reason: str, failed: str = "", served: str = "") -> None:
        """Record that the active provider failed and another mouth answered instead.

        `failed` and `served` are the canonical provider names. They are passed in from
        tts.py, which is the only place that knows which engine actually produced the
        bytes, so the UI can name the speaker instead of assuming it is "the other one".
        """
        cls.TTS_FALLBACK_ACTIVE = True
        cls.TTS_FALLBACK_REASON = (reason or "")[:400]
        cls.TTS_FALLBACK_FROM = (failed or "").strip().lower()
        cls.TTS_FALLBACK_TO = (served or "").strip().lower()

    @classmethod
    def clear_tts_fallback(cls) -> None:
        cls.TTS_FALLBACK_ACTIVE = False
        cls.TTS_FALLBACK_REASON = ""
        cls.TTS_FALLBACK_FROM = ""
        cls.TTS_FALLBACK_TO = ""

    @classmethod
    def cartesia_voice_for(cls, ui_lang: str) -> str:
        """The Cartesia voice UUID to speak `ui_lang` with, or "".

        NO fallback chain here, unlike the Tencent voices, because Cartesia does not need
        one: it speaks Malay natively, so BM has a voice of its own and never borrows.
        """
        key = (ui_lang or "EN").strip().upper()
        return {"EN": cls.CARTESIA_VOICE_ID_EN,
                "ZH": cls.CARTESIA_VOICE_ID_ZH,
                "BM": cls.CARTESIA_VOICE_ID_BM}.get(key) or ""

    @classmethod
    def cartesia_voice_is_fallback(cls, ui_lang: str) -> bool:
        """Always False: Cartesia has a real voice for every language this app speaks."""
        return False

    @classmethod
    def cartesia_configured(cls) -> bool:
        """True when Cartesia could actually be used: a key AND at least one voice."""
        return bool(cls.CARTESIA_API_KEY
                    and (cls.CARTESIA_VOICE_ID_EN or cls.CARTESIA_VOICE_ID_ZH
                         or cls.CARTESIA_VOICE_ID_BM))

    @classmethod
    def tencent_configured(cls) -> bool:
        """True when either Tencent mouth could actually be used."""
        return bool(cls.ASR_SECRET_ID and cls.ASR_SECRET_KEY
                    and (cls.TTS_VOICE_ID or cls.TTS_VOICE_ID_EN or cls.TTS_VOICE_ID_ZH
                         or cls.TTS_VOICE_ID_BM
                         or (cls.TRTC_SDK_APP_ID and (cls.TRTC_TTS_VOICE_EN
                                                      or cls.TRTC_TTS_VOICE_ZH
                                                      or cls.TRTC_TTS_VOICE_BM))))

    @classmethod
    def trtc_tts_voice_for(cls, ui_lang: str) -> str:
        """The TRTC VoiceId to speak `ui_lang` with, or "".

        Same degradation rule as tts_voice_for(): never invent an id, and let Bahasa Melayu
        borrow the English voice because no Malay voice exists.
        """
        key = (ui_lang or "EN").strip().upper()
        own = {"EN": cls.TRTC_TTS_VOICE_EN, "ZH": cls.TRTC_TTS_VOICE_ZH,
               "BM": cls.TRTC_TTS_VOICE_BM}.get(key)
        return own or (cls.TRTC_TTS_VOICE_EN if key == "BM" else "") or ""

    @classmethod
    def trtc_tts_voice_is_fallback(cls, ui_lang: str) -> bool:
        """True when `ui_lang` has no TRTC voice of its own and borrows another."""
        key = (ui_lang or "EN").strip().upper()
        own = {"EN": cls.TRTC_TTS_VOICE_EN, "ZH": cls.TRTC_TTS_VOICE_ZH,
               "BM": cls.TRTC_TTS_VOICE_BM}.get(key)
        return bool(not own and cls.trtc_tts_voice_for(key))

    @classmethod
    def trtc_tts_configured(cls) -> bool:
        """True when the streaming mouth COULD be used.

        Credentials (shared with the ears), the SdkAppId, and at least one voice id.

        Note what is NOT tested here: whether this provider is currently SELECTED. That
        distinction is load-bearing. set_tts_provider("tencent") asks this question to decide
        which Tencent mouth to switch to, so if the answer depended on already being on TRTC
        it would always be False and the toggle could never reach the streaming path. The
        "is it selected" question is answered by tts_provider(), separately.
        """
        return bool(cls.ASR_SECRET_ID and cls.ASR_SECRET_KEY and cls.TRTC_SDK_APP_ID
                    and (cls.TRTC_TTS_VOICE_EN or cls.TRTC_TTS_VOICE_ZH
                         or cls.TRTC_TTS_VOICE_BM))

    @classmethod
    def tts_provider(cls) -> str:
        """The mouth currently in force (runtime value, not the configured default)."""
        return cls.TTS_PROVIDER

    @classmethod
    def tts_provider_is_tencent(cls) -> bool:
        """True when the active mouth is one of the two Tencent engines."""
        return cls.TTS_PROVIDER in cls.TENCENT_PROVIDERS

    @classmethod
    def tts_provider_is_configured(cls, name: str = "") -> bool:
        """Could the named provider speak right now? Defaults to the active one."""
        want = (name or cls.TTS_PROVIDER).strip().lower()
        if want == "cartesia":
            return cls.cartesia_configured()
        if want in cls.TENCENT_PROVIDERS:
            return cls.tencent_configured()
        return False

    @classmethod
    def tts_served_provider(cls) -> str:
        """The mouth that produced the most recent line of audio.

        Equal to the selected provider unless the last attempt fell back, in which case it
        is the engine that actually answered. Kept SEPARATE from tts_provider() on purpose:
        the selection did not change, only who spoke, and collapsing the two would make the
        UI either lie about the toggle or lie about the voice.
        """
        if cls.TTS_FALLBACK_ACTIVE and cls.TTS_FALLBACK_TO:
            return cls.TTS_FALLBACK_TO
        return cls.TTS_PROVIDER

    # Credentials AND at least one voice are needed before the mouth can speak. Inlined
    # rather than calling tts_voice_configured() because this runs in the class body,
    # before that classmethod exists.
    # The streaming mouth counts too: with only TRTC voices configured (and provider set to
    # it), the app must report a working mouth rather than pretending there is none.
    # "Is ANY mouth able to speak?" -- not "is the selected one able to speak?".
    # This drives the /tts 503 gate and the UI's mute decision, so it must stay true when
    # Cartesia is configured even though the Tencent credentials below may be absent, and
    # vice versa. Which provider actually answers is resolved per request in tts.py, and the
    # per-provider question is answered by tts_provider_is_configured().
    LIVE_TTS = bool(
        # Cartesia: its own key and its own voices. Needs NOTHING from Tencent, which is why
        # it cannot be folded into the ASR_SECRET_ID test below.
        (CARTESIA_API_KEY and (CARTESIA_VOICE_ID_EN or CARTESIA_VOICE_ID_ZH
                               or CARTESIA_VOICE_ID_BM))
        # Tencent TextToVoice: the shared Tencent credentials plus a voice.
        or (ASR_SECRET_ID and ASR_SECRET_KEY
            and (TTS_VOICE_ID or TTS_VOICE_ID_EN or TTS_VOICE_ID_ZH or TTS_VOICE_ID_BM))
        # Tencent TRTC streaming: the same credentials plus the SdkAppId and a TRTC voice.
        # NOTE: no TTS_PROVIDER test here. That test used to be here and made the mouth look
        # unconfigured whenever TRTC was installed but not selected, which is precisely the
        # state the UI toggle needs to be able to leave.
        or (ASR_SECRET_ID and ASR_SECRET_KEY and TRTC_SDK_APP_ID
            and (TRTC_TTS_VOICE_EN or TRTC_TTS_VOICE_ZH or TRTC_TTS_VOICE_BM)))

    @classmethod
    def _voice_chain(cls, ui_lang: str) -> tuple:
        """(language, ordered candidate voice ids) for `ui_lang`.

        The order is: the language's OWN voice, then the single shared voice, then — for
        Malay only — the English voice. That last step exists because the international
        Tencent TTS catalogue ships no Malay voice at all: without it, a Malay conversation
        would get a 503 and no speech whatsoever. Reading Malay with an English voice is a
        compromise, but silence is not an option, and tts_voice_is_fallback() reports the
        substitution so nothing pretends a Malay voice spoke.
        """
        key = (ui_lang or "EN").strip().upper()
        own = {"EN": cls.TTS_VOICE_ID_EN,
               "ZH": cls.TTS_VOICE_ID_ZH,
               "BM": cls.TTS_VOICE_ID_BM}.get(key)
        chain = [own, cls.TTS_VOICE_ID]
        if key == "BM":
            chain.append(cls.TTS_VOICE_ID_EN)
        return key, [v for v in chain if v]

    @classmethod
    def tts_voice_for(cls, ui_lang: str) -> str:
        """The Tencent VoiceType to speak `ui_lang` (EN / ZH / BM) in, or "".

        Never raises and never invents an id: an unset language degrades along the chain in
        _voice_chain, and "" means genuinely unconfigured, which the caller reports as
        unavailable rather than silently speaking in the wrong tongue.
        """
        return (cls._voice_chain(ui_lang)[1] or [""])[0]

    @classmethod
    def tts_voice_configured(cls) -> bool:
        """True when at least one voice id exists, so /health and /tts can gate on it."""
        return bool(cls.TTS_VOICE_ID or cls.TTS_VOICE_ID_EN
                    or cls.TTS_VOICE_ID_ZH or cls.TTS_VOICE_ID_BM)

    @classmethod
    def tts_voice_is_fallback(cls, ui_lang: str) -> bool:
        """True when `ui_lang` is NOT being spoken by a voice of its own.

        Today that is Malay: the international Tencent catalogue ships no Malay voice, so BM
        borrows the English voice. Exposed so the log and /health can say so out loud instead
        of silently mislabelling the substitution.
        """
        key, chain = cls._voice_chain(ui_lang)
        if not chain:
            return False
        own = {"EN": cls.TTS_VOICE_ID_EN,
               "ZH": cls.TTS_VOICE_ID_ZH,
               "BM": cls.TTS_VOICE_ID_BM}.get(key)
        return chain[0] != own


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def active_proxy():
    """The outbound proxy the voice clients will actually use, or None when they go direct.

    Diagnostics only — it decides nothing. Both clients PIN their behaviour to
    Settings.USE_ENV_PROXY (default: direct), so this must agree with that flag or it would
    report a proxy the clients are not using. Used to log ONE line at connect time, so a
    proxied egress shows up as "going through <proxy>" in the terminal instead of as an
    unexplained connect failure.
    """
    if not Settings.USE_ENV_PROXY:
        return None
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        value = os.getenv(name)
        if value:
            return value
    return None


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


