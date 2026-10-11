"""Mouth, text to speech.

TWO PROVIDERS, ONE JOB, SWITCHABLE AT RUNTIME.

  * **Cartesia Sonic** -- ``POST api.cartesia.ai/tts/bytes``, raw PCM back. **THE DEFAULT.**
    Its own API key; it needs nothing from Tencent. Speaks Malay natively.
  * **Tencent Cloud** -- either ``TextToVoice`` (synchronous JSON, TC3-HMAC-SHA256) or the
    TRTC ``TextToSpeechSSE`` stream. This is what the UI's "TRTC Mode" toggle selects.

``Settings.tts_provider()`` names the active one; ``Settings.set_tts_provider()`` changes it
(``POST /tts/provider`` does exactly that). ``_speak_pcm`` is the single place that decides,
and it also owns the reported fallback -- if the active mouth cannot speak, the other one
does, and ``/health`` plus the UI say so rather than letting the voice change silently.

Design rule (unchanged): the engine is a *provider*, not a decider. It turns the backend's
INTERFACE_LINE text into sound. It never chooses what AuntieJoy says, the orchestrator /
messages.py catalogue does.

Five provider facts worth knowing before reading further:

1. **TextToVoice is synchronous.** The international site exposes no streaming TTS
   endpoint (the WebSocket streaming API is not part of the international catalogue), so a
   line is synthesised IN FULL and only then relayed. The frontend still receives it as a
   chunked stream and schedules it on the audio clock, so playback is sample-accurate; what
   is lost is a sub-second *first* chunk. For one interface sentence (the only
   thing this file is ever asked to speak) that is a fraction of a second, not a pause the
   customer notices.

2. **Cartesia is the only provider here with a real Malay voice.** Tencent's international
   catalogue carries Chinese, Cantonese, English and two Chinese dialects -- no Malay at all.
   So on the Tencent path a Malay conversation is read by a Chinese voice, which is a
   deliberate choice rather than a fallback (``AUNTIEJOY_TTS_VOICE_BM`` is set), and the
   *right* Chinese voice had to be chosen by measurement: the southern voice used for ZH/EN
   (101019, Cantonese) collapses on Malay -- an 82-85% transcription match for every other
   voice drops to 15% for it. On the Cartesia path BM has a genuine Malay voice (``ms``), so
   ``tts_voice_is_fallback("BM")`` is true for Tencent and FALSE for Cartesia.

3. **A Chinese voice can read English; an English voice cannot read Chinese.** This asymmetry
   is why ``_resolve_voice_and_model`` exists, and it is also what gives the Tencent English
   lines their Chinese accent: they are spoken by the Chinese voice model itself, not by an
   English voice with a filter over it. Verified both directions against the live API -- the
   English voices (101050/101051) fail Chinese text with `InvalidParameter.InvalidText`, while
   every premium Chinese voice returns audio for English at native-level transcription
   accuracy. This constraint is Tencent-only; Cartesia's model is multilingual.

4. **One Tencent request is capped at 150 Chinese characters or 500 letters.** The read-back
   for a payment sits right at that ceiling, so long lines are split on sentence boundaries
   and the resulting clips are concatenated. See ``split_for_tts``. Cartesia has no comparable
   ceiling, so a Cartesia line goes in ONE request and keeps its prosody across the sentence.

5. **The two providers measure speed differently, and the units are not interchangeable.**
   Tencent uses an integer offset in [-2, 6] where 0 means 1.0x; Cartesia uses a plain float
   multiplier in [0.6, 1.5]. ``AUNTIEJOY_TTS_SPEED`` and ``CARTESIA_SPEED`` are therefore
   separate settings. Copying one into the other is silently wrong rather than an error.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import re
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional, Tuple

import httpx

from .config import Settings, active_proxy

log = logging.getLogger("auntiejoy.tts")

# Raw PCM, mono, 16-bit signed little-endian, the same shape the browser already builds
# AudioBuffers from. Asking for PCM rather than WAV means no container to strip.
SAMPLE_RATE = Settings.TTS_SAMPLE_RATE

# Our UI language (EN/ZH/BM) -> Tencent PrimaryLanguage: 1 = Chinese, 2 = English.
# Malay has no Tencent voice, so BM rides the English model (see the module docstring).
LANG_TO_PRIMARY = {"EN": 2, "ZH": 1, "MIXED": 2, "BM": 2}
# Our UI language -> the code we log and echo. These are the SAME codes the previous
# provider used (en/zh/ms), kept deliberately so the logged language of a turn never changes
# just because the voice provider did: existing audit rows and dashboards keep reading the
# same values, and a Malay turn is still logged "ms" even though it is spoken by the EN voice.
LANG_TO_CODE = {"EN": "en", "ZH": "zh", "MIXED": "en", "BM": "ms"}

# How much PCM to hand the browser per chunk. The frontend batches into ~200 ms frames
# anyway; this only controls how soon the FIRST byte arrives.
_RELAY_CHUNK_BYTES = 4096

# Transient Tencent errors worth one more try. Everything else is a real answer.
_RETRYABLE_CODES = {
    "InternalError", "InternalError.InternalError", "InternalError.NoResource",
    "InternalError.ErrorGetRoute", "InternalError.ExceedMaxLimit",
    "LimitExceeded.AccessLimit",
}

# Error codes that mean "the account cannot use TTS at all", rendered as a sentence an
# operator can act on rather than a bare code.
_CODE_MEANINGS = {
    "AuthFailure.InvalidAuthorization": "invalid authorization (check SecretId/SecretKey)",
    "AuthFailure.SignatureExpire": "the request signature expired (check the server clock)",
    "AuthFailure.SignatureFailure": "signature mismatch (check SecretKey)",
    "UnsupportedOperation.AuthorizationFailed": "authorization failed for this account",
    "UnsupportedOperation.AuthorizationExpired": "authorization expired",
    "InvalidParameterValue.AppIdNotRegistered": "the APPID is not registered, activate TTS "
                                                 "in the Tencent Cloud TTS console first",
    "UnsupportedOperation.ServerNotOpen": "the TTS service is not activated for this account",
    "UnsupportedOperation.ServerStopped": "the TTS service is stopped",
    "UnsupportedOperation.NoFreeAccount": "the TTS free tier is used up",
    "UnsupportedOperation.PkgExhausted": "the TTS resource package is used up",
    "UnsupportedOperation.AccountArrears": "the account is in arrears",
    "UnsupportedOperation.ForbiddenUse": "TTS use is prohibited for this account",
    "LimitExceeded.AccessLimit": "TTS rate limit exceeded (20 requests/sec)",
    "InvalidParameterValue.VoiceType": "unknown VoiceType (see the TTS voice catalogue)",
    "InvalidParameterValue.TextTooLong": "the text exceeded the per-request limit",
    "InvalidParameterValue.TextEmpty": "empty text",
    "InvalidParameterValue.Speed": "Speed out of range [-2, 6]",
    "InvalidParameterValue.Volume": "Volume out of range [0, 10]",
    "InvalidParameterValue.SampleRate": "SampleRate must be 8000 or 16000",
}


class TTSError(RuntimeError):
    """Raised when speech cannot be produced (no config, auth failure, bad voice, ...)."""


@dataclass
class SpeechResult:
    wav_bytes: bytes
    language: str
    # The provider that actually answered, not a hardcoded name: with two mouths and a
    # runtime switch, a constant here would misdescribe half the calls.
    engine: str = ""


def language_for(ui_lang: str) -> str:
    """Our UI language (EN / ZH / BM / mixed) as the code we log and echo."""
    return LANG_TO_CODE.get((ui_lang or "EN").strip().upper(), "en")


def _primary_language(ui_lang: str) -> int:
    """Tencent PrimaryLanguage for `ui_lang`: 1 = Chinese, 2 = English."""
    return LANG_TO_PRIMARY.get((ui_lang or "EN").strip().upper(), 2)


def _resolve_voice_and_model(piece: str, ui_lang: str) -> Tuple[str, int]:
    """The (VoiceType, PrimaryLanguage) pair that can actually read `piece`.

    Tencent needs the VOICE and the MODEL to agree with the text, not just the model. An
    English voice handed Chinese characters comes back as
    `InvalidParameter.InvalidText` with NO audio at all, verified against the live API,
    including with PrimaryLanguage forced to Chinese. So:

      * a piece containing any CJK is spoken by the CHINESE voice on the Chinese model
        (the Chinese model also reads embedded English, so a code-mixed sentence is fine);
      * everything else uses the session language's own voice on the English model, which
        also carries Malay, the international catalogue has no Malay voice.

    Deciding per piece (not per line) is what keeps a code-mixed line working after
    split_for_tts() has cut it at sentence boundaries.
    """
    if _CJK.search(piece):
        zh_voice = Settings.tts_voice_for("ZH")
        if not zh_voice:
            raise TTSError("text contains Chinese characters but no Chinese voice is "
                           "configured (set AUNTIEJOY_TTS_VOICE_ZH)")
        return zh_voice, 1
    return Settings.tts_voice_for(ui_lang), _primary_language(ui_lang)


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw mono 16-bit PCM in a minimal WAV container for callers that want a file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


# =====================================================================
# Text splitting, one request is capped at 150 CJK chars or 500 letters
# =====================================================================
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Sentence boundaries in every language this app speaks, plus the newline.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？…])\s*|\n+")
# A last-resort break when one "sentence" is itself over the cap: after a comma or a space.
_SOFT_SPLIT = re.compile(r"(?<=[,;:，、；：])\s*|\s+")


def _weight(text: str) -> Tuple[int, int]:
    """(CJK character count, non-CJK character count), the two budgets Tencent caps."""
    cjk = len(_CJK.findall(text))
    return cjk, len(text) - cjk


def _fits(text: str) -> bool:
    cjk, other = _weight(text)
    return cjk <= Settings.TTS_MAX_CJK_CHARS and other <= Settings.TTS_MAX_LATIN_CHARS


def _hard_split(piece: str) -> List[str]:
    """Break a single over-long run of text at the last boundary that still fits."""
    parts: List[str] = []
    current = ""
    for token in _SOFT_SPLIT.split(piece):
        candidate = (current + " " + token).strip() if current else token
        if current and not _fits(candidate):
            parts.append(current)
            current = token
        else:
            current = candidate
        # A single token longer than the whole budget (a 600-character URL, say): cut it.
        while not _fits(current):
            limit = (Settings.TTS_MAX_CJK_CHARS if _CJK.search(current[:50])
                     else Settings.TTS_MAX_LATIN_CHARS)
            parts.append(current[:limit])
            current = current[limit:]
    if current.strip():
        parts.append(current)
    return parts


def split_for_tts(text: str) -> List[str]:
    """Split `text` into pieces that each fit one TextToVoice request.

    Splits on sentence boundaries first so a clip never breaks mid-sentence unless a single
    sentence is itself over the cap. Short lines (the normal case) come back as one piece.
    """
    clean = (text or "").strip()
    if not clean:
        return []
    if _fits(clean):
        return [clean]

    chunks: List[str] = []
    current = ""
    for sentence in (s.strip() for s in _SENTENCE_SPLIT.split(clean)):
        if not sentence:
            continue
        if not _fits(sentence):
            # Flush what we have, then break the over-long sentence itself.
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(sentence))
            continue
        candidate = (current + " " + sentence).strip() if current else sentence
        if _fits(candidate):
            current = candidate
        else:
            chunks.append(current)
            current = sentence
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if c.strip()]


# =====================================================================
# Generic TC3-HMAC-SHA256 signing.
#
# Originally written for TextToVoice and now shared with the TRTC streaming mouth, which is
# a different service ("trtc") with a different action and API version but the identical
# signing algorithm. The service/action/version therefore come in as ARGUMENTS rather than
# being read from Settings inside, so one signer cannot accidentally sign a request for the
# wrong service.
# =====================================================================
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def tc3_headers(payload: dict, region: str, endpoint: str, service: str = Settings.TTS_SERVICE,
                action: str = Settings.TTS_ACTION,
                api_version: str = Settings.TTS_API_VERSION) -> Tuple[dict, str]:
    """Build the signed headers and JSON body for one Tencent Cloud API call.

    The algorithm is the documented TC3-HMAC-SHA256: a canonical request hashed into a
    string-to-sign, then a date/service/request key ladder derived from the SecretKey. The
    SecretKey never leaves this function.
    """
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    timestamp = int(datetime.now(timezone.utc).timestamp())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

    canonical_headers = (f"content-type:application/json; charset=utf-8\n"
                         f"host:{endpoint}\n"
                         f"x-tc-action:{action.lower()}\n")
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST", "/", "",
        canonical_headers, signed_headers, _sha256_hex(body.encode("utf-8")),
    ])

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(timestamp), credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    secret_date = _hmac_sha256(("TC3" + Settings.ASR_SECRET_KEY).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    headers = {
        "Authorization": (f"TC3-HMAC-SHA256 "
                          f"Credential={Settings.ASR_SECRET_ID}/{credential_scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
        "Content-Type": "application/json; charset=utf-8",
        "Host": endpoint,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": api_version,
        "X-TC-Region": region,
    }
    return headers, body


def _describe_error(code: Optional[str], message: Optional[str]) -> str:
    meaning = _CODE_MEANINGS.get(code or "")
    if meaning:
        return f"{code}: {meaning}"
    return f"{code}: {message or 'unknown error'}"


# =====================================================================
# Synthesis
# =====================================================================
async def _synth_piece(client: httpx.AsyncClient, piece: str, voice_type: str,
                       primary_language: int) -> bytes:
    """One TextToVoice call -> raw mono 16-bit PCM. Retries only the transient failures."""
    payload = {
        "Text": piece,
        "SessionId": uuid.uuid4().hex,
        "ModelType": Settings.TTS_MODEL_TYPE,
        "VoiceType": int(voice_type),
        "PrimaryLanguage": primary_language,
        "SampleRate": SAMPLE_RATE,
        "Codec": "pcm",
        "Volume": Settings.TTS_VOLUME,
        "Speed": Settings.TTS_SPEED,
    }
    # The soft, unhurried read. Sent only when configured: an ABSENT EmotionCategory and
    # EmotionCategory="neutral" are different requests to this API and produce different
    # audio (verified -- three identical calls are byte-identical, so the difference is real
    # and not synthesis jitter). Intensity rides along only when an emotion is set, because
    # it is documented to be ignored otherwise.
    if Settings.TTS_EMOTION:
        payload["EmotionCategory"] = Settings.TTS_EMOTION
        payload["EmotionIntensity"] = Settings.TTS_EMOTION_INTENSITY
    headers, body = tc3_headers(payload, Settings.TTS_REGION, Settings.TTS_ENDPOINT)
    url = f"https://{Settings.TTS_ENDPOINT}/"

    backoff = 0.4
    last: Optional[str] = None
    for attempt in range(3):
        try:
            resp = await client.post(url, headers=headers, content=body.encode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - transport, fail closed
            last = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise TTSError(f"Tencent TTS transport failed: {last}") from exc

        if resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            if attempt < 2:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise TTSError(f"Tencent TTS upstream error: {last}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise TTSError(f"Tencent TTS returned a non-JSON body (HTTP {resp.status_code})") from exc

        r = (data or {}).get("Response") or {}
        err = r.get("Error")
        if err:
            code = err.get("Code")
            if code in _RETRYABLE_CODES and attempt < 2:
                last = _describe_error(code, err.get("Message"))
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise TTSError(f"Tencent TTS error: {_describe_error(code, err.get('Message'))}")

        audio_b64 = r.get("Audio") or ""
        if not audio_b64:
            raise TTSError("Tencent TTS returned no audio")
        pcm = base64.b64decode(audio_b64)
        if not pcm:
            raise TTSError("Tencent TTS returned empty audio")
        return pcm

    raise TTSError(f"Tencent TTS failed after retries: {last}")


# =====================================================================
# Streaming mouth: TRTC TextToSpeechSSE
#
# Same job as _synth_piece (one piece of text -> mono 16-bit PCM at SAMPLE_RATE), but the
# audio arrives as a Server-Sent Events stream instead of one complete response, so the
# browser can start playing before the sentence has finished being generated.
#
# Verified against the TRTC docs (TextToSpeechSSE API + data types):
#   * service "trtc", action "TextToSpeechSSE", version 2019-07-22, region ap-singapore ONLY.
#   * request: {Text, Voice:{VoiceId}, SdkAppId, Model, AudioFormat:{Format, SampleRate}}
#   * response: repeated `data: {...}` lines, each a chunk {Type:"chunk", Audio:<base64>,
#     Seq, IsEnd}, terminated by the chunk with IsEnd true. A mid-stream failure arrives as
#     {Type:"error", Error:{Code, Message}}. A failure BEFORE synthesis is not SSE at all but
#     a plain JSON {Response:{Error:{...}}} body, detectable from the Content-Type header.
#   * Text length [1, 255]; AudioFormat.Format supports "pcm" (the default) and SampleRate
#     accepts 16000 (default is 24000, so it must be sent explicitly or the browser would be
#     handed audio at a rate its AudioContext was not told about).
# =====================================================================
_TRTC_MAX_CHARS = Settings.TRTC_TTS_MAX_CHARS


def _trtc_split(text: str) -> List[str]:
    """Split `text` into pieces that each fit one TextToSpeechSSE request ([1, 255] chars).

    The SSE budget is CHARACTERS, not the CJK/latin split budgets TextToVoice uses, so this
    reuses split_for_tts's sentence-aware behaviour and then subdivides anything still over
    the SSE ceiling. Sentence boundaries first, so a clip never breaks mid-sentence.
    """
    pieces: List[str] = []
    for piece in split_for_tts(text):
        while len(piece) > _TRTC_MAX_CHARS:
            cut = piece.rfind(" ", 0, _TRTC_MAX_CHARS)
            if cut <= 0:
                cut = _TRTC_MAX_CHARS
            pieces.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            pieces.append(piece)
    return [p for p in pieces if p]


def _trtc_language(piece: str, ui_lang: str) -> str:
    """The `Language` code to send for this piece: zh | en.

    The SSE model takes an explicit ISO 639-1 language and supports zh, en, yue, ja, ko.
    There is NO Malay option, exactly like TextToVoice, so Bahasa Melayu is spoken by the
    English voice and Settings.trtc_tts_voice_is_fallback("BM") reports the substitution.
    Chinese is detected from the script rather than assumed from the session language, which
    is what keeps a Mandarin sentence inside a code-mixed line readable.
    """
    return "zh" if _CJK.search(piece) else "en"


async def _trtc_synth_piece(client: httpx.AsyncClient, piece: str, voice_id: str,
                            language: str) -> AsyncIterator[bytes]:
    """Yield PCM for ONE piece as it arrives, rather than waiting for the whole clip."""
    payload = {
        "Text": piece,
        "SdkAppId": int(Settings.TRTC_SDK_APP_ID),
        "Model": Settings.TRTC_TTS_MODEL,
        "Voice": {"VoiceId": voice_id},
        "Language": language,
        # PCM at the SAME rate the ears capture and the browser decodes. Explicit because the
        # service default is 24000 and a mismatch would play at the wrong speed.
        "AudioFormat": {"Format": "pcm", "SampleRate": SAMPLE_RATE},
    }
    headers, body = tc3_headers(payload, Settings.TRTC_TTS_REGION, Settings.TRTC_TTS_ENDPOINT,
                                service=Settings.TRTC_SERVICE,
                                action=Settings.TRTC_TTS_ACTION,
                                api_version=Settings.TRTC_TTS_API_VERSION)
    url = f"https://{Settings.TRTC_TTS_ENDPOINT}/"

    async with client.stream("POST", url, headers=headers,
                             content=body.encode("utf-8")) as resp:
        # A failure before synthesis is JSON, not SSE. Detect it here rather than trying to
        # parse it as an event stream, where it would surface as an opaque "no audio".
        content_type = (resp.headers.get("content-type") or "").lower()
        if resp.status_code >= 400 or "text/event-stream" not in content_type:
            raw = await resp.aread()
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
                err = ((data or {}).get("Response") or {}).get("Error") or {}
                raise TTSError("TRTC TextToSpeechSSE error: "
                               f"{_describe_error(err.get('Code'), err.get('Message'))}")
            except ValueError:
                raise TTSError(
                    f"TRTC TextToSpeechSSE returned HTTP {resp.status_code} with a "
                    f"non-streaming body: {raw[:200]!r}")

        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue                        # a heartbeat or blank frame, not a chunk
            kind = event.get("Type")
            if kind == "error":
                err = event.get("Error") or {}
                raise TTSError("TRTC TextToSpeechSSE stream error: "
                               f"{_describe_error(err.get('Code'), err.get('Message'))}")
            if kind != "chunk":
                continue
            audio_b64 = event.get("Audio") or ""
            if audio_b64:
                try:
                    yield base64.b64decode(audio_b64)
                except Exception as exc:                    # noqa: BLE001 - never crash mid-stream
                    raise TTSError(f"TRTC TextToSpeechSSE sent undecodable audio: {exc}") from exc
            if event.get("IsEnd"):
                return


async def _trtc_pcm(text: str, ui_lang: str) -> AsyncIterator[bytes]:
    """Yield raw mono 16-bit PCM for `text` from the TRTC streaming endpoint."""
    if not (Settings.ASR_SECRET_ID and Settings.ASR_SECRET_KEY and Settings.TRTC_SDK_APP_ID):
        raise TTSError("TRTC streaming TTS is not configured, set TENCENTCLOUD_SECRET_ID, "
                       "TENCENTCLOUD_SECRET_KEY and TRTC_SDK_APP_ID")

    pieces = _trtc_split(text)
    if not pieces:
        raise TTSError("nothing to speak")

    fallback = Settings.trtc_tts_voice_is_fallback(ui_lang)
    log.info("tts trtc-sse: lang=%s voices=%s/%s region=%s endpoint=%s pieces=%d text_len=%d%s",
             ui_lang, Settings.trtc_tts_voice_for("ZH"), Settings.trtc_tts_voice_for(ui_lang),
             Settings.TRTC_TTS_REGION, Settings.TRTC_TTS_ENDPOINT, len(pieces), len(text),
             " (FALLBACK: no Malay voice exists, using the English voice)"
             if fallback else "")

    proxy = active_proxy()
    if proxy:
        log.info("TRTC TTS connecting via proxy %s", proxy)

    async with httpx.AsyncClient(timeout=20.0, trust_env=Settings.USE_ENV_PROXY) as client:
        for index, piece in enumerate(pieces):
            piece_voice = Settings.trtc_tts_voice_for(ui_lang)
            if _CJK.search(piece):
                piece_voice = Settings.trtc_tts_voice_for("ZH") or piece_voice
            if not piece_voice:
                raise TTSError("TRTC voice not configured, set TRTC_TTS_VOICE_EN / _ZH")
            language = _trtc_language(piece, ui_lang)
            async for chunk in _trtc_synth_piece(client, piece, piece_voice, language):
                yield chunk
            if index < len(pieces) - 1:
                yield b"\x00\x00" * int(SAMPLE_RATE * Settings.TTS_PIECE_GAP_MS / 1000)


# =====================================================================
# The default mouth: CARTESIA (Sonic)
#
# One synchronous POST returns the whole clip as raw PCM, so this path is structurally the
# simplest of the three: no container to strip, no SSE frames to reassemble, no per-request
# character ceiling worth worrying about. It is the DEFAULT provider, and the Tencent mouths
# below are what the UI's "TRTC Mode" toggle switches to.
#
# Verified against the live API before this was written:
#   * POST https://api.cartesia.ai/tts/bytes with X-API-Key + Cartesia-Version.
#   * A 1-character transcript returns 200 with real PCM; a deliberately wrong key returns
#     401 "Invalid API key", so a failure here is never ambiguous about which of the two it is.
#   * All three configured voice ids return audio (EN/ZH/BM).
#
# The one genuine capability upgrade over Tencent: Cartesia speaks MALAY NATIVELY (ms is in
# Sonic's 44 languages). Tencent's international catalogue has no Malay voice at all, so the
# Tencent path reads Malay with a borrowed Mandarin voice and flags it. Here BM has a voice of
# its own, so no language borrows another and there is nothing to flag.
# =====================================================================

# Our UI language -> Cartesia's ISO 639-1 code. Unlike the Tencent map this is NOT a
# degradation: every entry is a language the model actually speaks, so BM -> "ms" is a real
# Malay voice rather than a stand-in.
LANG_TO_CARTESIA = {"EN": "en", "ZH": "zh", "MIXED": "en", "BM": "ms"}

# Cartesia error codes rendered as something an operator can act on. The quota one is here
# because it is the failure that actually happens in practice: a valid key on an account with
# no credit left authenticates fine and then refuses every synthesis, which reads like a code
# bug unless it is named.
_CARTESIA_CODE_MEANINGS = {
    "unauthorized": "the API key was rejected (check CARTESIA_API_KEY)",
    "quota_exceeded": "the Cartesia account is out of credits -- top up at "
                      "play.cartesia.ai/subscription, or switch to TRTC Mode",
    "model_not_found": "unknown model id (check CARTESIA_MODEL)",
    "voice_not_found": "unknown voice id (check CARTESIA_VOICE_ID_EN / _ZH / _BM)",
    "invalid_request": "the request was rejected (check the voice id and language code)",
    "language_not_supported": "the model does not speak this language",
    "unsupported_language": "the model does not speak this language",
    "text_too_long": "the transcript exceeded the model's limit",
}


def _cartesia_language(piece: str, ui_lang: str) -> str:
    """The `language` code to send for this piece.

    Chinese is detected from the SCRIPT rather than assumed from the session language, so a
    Mandarin sentence inside an otherwise-English line is still normalised as Chinese. This
    matters more here than for Tencent: the wrong language code does not fail, it silently
    changes how numbers and dates are read out.
    """
    if _CJK.search(piece):
        return "zh"
    return LANG_TO_CARTESIA.get((ui_lang or "EN").strip().upper(), "en")


def _cartesia_split(text: str) -> List[str]:
    """Split only if the line is absurdly long; Sonic has no 150-character ceiling.

    Sentence-aware so a split, if it ever happens, still breaks at a sentence boundary. In
    practice an interface line is one piece, which is the point: one request keeps the
    prosody continuous across the whole sentence instead of stitching clips together.
    """
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= Settings.CARTESIA_MAX_CHARS:
        return [clean]
    pieces: List[str] = []
    for piece in split_for_tts(clean):
        while len(piece) > Settings.CARTESIA_MAX_CHARS:
            cut = piece.rfind(" ", 0, Settings.CARTESIA_MAX_CHARS)
            if cut <= 0:
                cut = Settings.CARTESIA_MAX_CHARS
            pieces.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            pieces.append(piece)
    return [p for p in pieces if p]


async def _cartesia_synth(client: httpx.AsyncClient, text: str, voice_id: str,
                          language: str) -> bytes:
    """One Cartesia request -> the complete clip as raw mono 16-bit PCM."""
    payload = {
        "model_id": Settings.CARTESIA_MODEL,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "language": language,
        # "raw" means NO container: the bytes are exactly the PCM the browser already knows
        # how to decode, and the same X-Audio-* headers describe them.
        "output_format": {"container": "raw", "encoding": "pcm_s16le",
                          "sample_rate": SAMPLE_RATE},
        # A plain multiplier, NOT Tencent's [-2, 6] offset scale. See config.py.
        "generation_config": {"speed": Settings.CARTESIA_SPEED},
    }
    headers = {
        "X-API-Key": Settings.CARTESIA_API_KEY,
        "Cartesia-Version": Settings.CARTESIA_API_VERSION,
        "Content-Type": "application/json",
    }
    url = f"https://{Settings.CARTESIA_ENDPOINT}/tts/bytes"

    backoff = 0.4
    last: Optional[str] = None
    for attempt in range(3):
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except Exception as exc:                      # noqa: BLE001 - transport, fail closed
            last = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise TTSError(f"Cartesia transport failed: {last}") from exc

        # 429 and 5xx are worth another try; everything else is a real answer.
        if resp.status_code == 429 or resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            if attempt < 2:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise TTSError(f"Cartesia upstream error: {last}")

        if resp.status_code >= 400:
            # Cartesia reports failures as a JSON body on some API versions and as PLAIN TEXT
            # on others -- the 402 is `{"error_code":"quota_exceeded",...}` on one version and
            # `Insufficient credits: ...` on another. Both are normalised to the same code
            # here, so the reason an operator reads does not depend on which version answered.
            code, message = "unknown", ""
            try:
                err = resp.json() or {}
                code = err.get("error_code") or err.get("code") or "unknown"
                message = err.get("message") or err.get("title") or ""
            except ValueError:
                message = (resp.text or "")[:200]
            if code == "unknown":
                low = f"{message}".lower()
                if "insufficient credit" in low or "quota" in low or "credit" in low:
                    code = "quota_exceeded"
                elif "invalid api key" in low or "unauthorized" in low:
                    code = "unauthorized"
            meaning = _CARTESIA_CODE_MEANINGS.get(code)
            detail = f"{code}: {meaning}" if meaning else f"{code}: {message}"
            raise TTSError(f"Cartesia error (HTTP {resp.status_code}): {detail}")

        audio = resp.content or b""
        if not audio:
            raise TTSError("Cartesia returned no audio")
        return audio

    raise TTSError(f"Cartesia failed after retries: {last}")


async def _cartesia_pcm(text: str, ui_lang: str) -> AsyncIterator[bytes]:
    """Yield raw mono 16-bit PCM for `text` from Cartesia."""
    if not Settings.CARTESIA_API_KEY:
        raise TTSError("Cartesia is not configured, set CARTESIA_API_KEY")

    pieces = _cartesia_split(text)
    if not pieces:
        raise TTSError("nothing to speak")

    log.info("tts cartesia: lang=%s model=%s speed=%s endpoint=%s pieces=%d text_len=%d",
             ui_lang, Settings.CARTESIA_MODEL, Settings.CARTESIA_SPEED,
             Settings.CARTESIA_ENDPOINT, len(pieces), len(text))

    proxy = active_proxy()
    if proxy:
        log.info("Cartesia TTS connecting via proxy %s", proxy)

    async with httpx.AsyncClient(timeout=30.0, trust_env=Settings.USE_ENV_PROXY) as client:
        for index, piece in enumerate(pieces):
            # A piece containing CJK is spoken by the Chinese voice; otherwise the session
            # language's own voice. Cartesia needs the voice and the language to agree, the
            # same way Tencent does.
            if _CJK.search(piece):
                piece_voice = Settings.cartesia_voice_for("ZH") or Settings.cartesia_voice_for(ui_lang)
            else:
                piece_voice = Settings.cartesia_voice_for(ui_lang)
            if not piece_voice:
                raise TTSError(f"no Cartesia voice configured for {ui_lang}, "
                               "set CARTESIA_VOICE_ID_EN / _ZH / _BM")
            pcm = await _cartesia_synth(client, piece, piece_voice,
                                        _cartesia_language(piece, ui_lang))
            for start in range(0, len(pcm), _RELAY_CHUNK_BYTES):
                yield pcm[start:start + _RELAY_CHUNK_BYTES]
            if index < len(pieces) - 1:
                yield b"\x00\x00" * int(SAMPLE_RATE * Settings.TTS_PIECE_GAP_MS / 1000)


async def _tencent_pcm(text: str, ui_lang: str) -> AsyncIterator[bytes]:
    """Yield raw mono 16-bit PCM at SAMPLE_RATE Hz for `text`, piece by piece.

    Raises TTSError on any failure (no config, bad auth, bad voice, upstream error).
    """
    if not Settings.LIVE_TTS:
        raise TTSError(
            "TTS is not configured, set CARTESIA_API_KEY and a Cartesia voice "
            "(CARTESIA_VOICE_ID_EN / _ZH / _BM), or the Tencent credentials plus "
            "AUNTIEJOY_TTS_VOICE_EN / _ZH"
        )

    # NOTE: provider selection used to live here. It now belongs to _speak_pcm(), which owns
    # the choice AND the fallback, so this function is only ever the TextToVoice mouth. Having
    # two places decide which provider speaks is how a fallback ends up fighting a setting.

    voice_type = Settings.tts_voice_for(ui_lang)
    if not voice_type:
        raise TTSError("TTS voice not configured, set AUNTIEJOY_TTS_VOICE_EN / _ZH "
                       "(or a shared AUNTIEJOY_TTS_VOICE)")
    try:
        int(voice_type)
    except (TypeError, ValueError) as exc:
        raise TTSError(f"TTS voice must be a Tencent VoiceType integer, got {voice_type!r}") from exc

    pieces = split_for_tts(text)
    if not pieces:
        raise TTSError("nothing to speak")

    # Log the voice + region + language on every synthesis so the operator can confirm which
    # voice is actually speaking. /health exposes only booleans, never the voice id itself.
    log.info("tts tencent: lang=%s voice=%s region=%s endpoint=%s speed=%s pieces=%d text_len=%d%s",
             ui_lang, voice_type, Settings.TTS_REGION, Settings.TTS_ENDPOINT,
             Settings.TTS_SPEED, len(pieces), len(text),
             " (FALLBACK: no voice for this language, using another language's voice)"
             if Settings.tts_voice_is_fallback(ui_lang) else "")

    # One line so a proxied egress is visible instead of surfacing as an opaque connect
    # failure. Decides nothing; the client is pinned to USE_ENV_PROXY below.
    proxy = active_proxy()
    if proxy:
        log.info("TTS connecting via proxy %s", proxy)

    # One connection reused across the pieces of a line; TTS is ~20 req/s so a burst of
    # pieces for one sentence is well inside the limit.
    # trust_env is pinned to Settings.USE_ENV_PROXY: httpx would otherwise read HTTP_PROXY /
    # HTTPS_PROXY from the environment and route a call to a fixed public Tencent host through
    # whatever proxy the shell happens to export. See config.py for why that is opt-in here.
    async with httpx.AsyncClient(timeout=20.0, trust_env=Settings.USE_ENV_PROXY) as client:
        for index, piece in enumerate(pieces):
            # Resolve the voice AND the model from the piece itself: Tencent returns no
            # audio when the voice cannot read the script (see _resolve_voice_and_model).
            piece_voice, piece_model = _resolve_voice_and_model(piece, ui_lang)
            try:
                int(piece_voice)
            except (TypeError, ValueError) as exc:
                raise TTSError(
                    f"TTS voice must be a Tencent VoiceType integer, got {piece_voice!r}"
                ) from exc
            pcm = await _synth_piece(client, piece, piece_voice, piece_model)
            for start in range(0, len(pcm), _RELAY_CHUNK_BYTES):
                yield pcm[start:start + _RELAY_CHUNK_BYTES]
            # A small pause between pieces so concatenated sentences are not run together.
            if index < len(pieces) - 1:
                yield b"\x00\x00" * int(SAMPLE_RATE * Settings.TTS_PIECE_GAP_MS / 1000)


# =====================================================================
# The dispatcher: pick the mouth, and never go silent
#
# One place decides which provider speaks. Cartesia is the default; the UI's "TRTC Mode"
# toggle switches to Tencent at runtime; and if the chosen provider fails for a reason that
# is not the text's fault, the other mouth answers instead.
#
# The fallback is REPORTED, never silent. Settings.TTS_FALLBACK_ACTIVE / _REASON carry it to
# /health and the UI, so a presenter can see "Cartesia is out of credit, Tencent is speaking"
# instead of wondering why the voice changed. A hidden fallback would be worse than an error.
#
# The partially-emitted rule is the subtle part, and it is the same rule the old TRTC path
# used: once a byte of audio has reached the browser, a failure is a FAILURE, not a retry.
# Replaying the sentence from the top would dub over the audio already being played.
# =====================================================================
def _pcm_for_provider(name: str, text: str, ui_lang: str) -> AsyncIterator[bytes]:
    """The synthesis coroutine for a named provider. No fallback logic here."""
    if name == "cartesia":
        return _cartesia_pcm(text, ui_lang)
    if name == "texttospeechsse":
        return _trtc_pcm(text, ui_lang)
    return _tencent_pcm(text, ui_lang)


def provider_order() -> List[str]:
    """The providers to try, in order: the active one first, then one fallback if enabled.

    Public because the HTTP layer needs the same answer to decide whether the mouth is
    usable at all -- asking "is the SELECTED provider configured?" would 503 a request that
    the fallback could have served.
    """
    active = Settings.tts_provider()
    order = [active]
    if not Settings.TTS_FALLBACK_ENABLED:
        return order
    # The other vendor. Switching to Tencent prefers the streaming mouth when it is
    # configured, matching what the TRTC toggle itself would pick.
    if active == "cartesia":
        alt = "texttospeechsse" if Settings.trtc_tts_configured() else "texttovoice"
    else:
        alt = "cartesia"
    if alt != active and Settings.tts_provider_is_configured(alt):
        order.append(alt)
    return order


async def _speak_pcm(text: str, ui_lang: str) -> AsyncIterator[bytes]:
    """Speak `text` with the active provider, falling back once if it cannot."""
    order = provider_order()
    last_error: Optional[Exception] = None
    reason = ""

    for index, name in enumerate(order):
        emitted = False
        try:
            async for chunk in _pcm_for_provider(name, text, ui_lang):
                emitted = True
                yield chunk
            Settings.TTS_STREAMING_ACTIVE = (name == "texttospeechse")
            if index == 0:
                # The selected provider answered, so clear any stale fallback note --
                # otherwise a failure from ten minutes ago would still be displayed.
                Settings.clear_tts_fallback()
            else:
                # A fallback did happen. Pin the note to the engine that ACTUALLY produced
                # the audio: order[1] was only a prediction, and it is a lie if the first
                # successor failed too and a later one is the voice the user just heard.
                Settings.note_tts_fallback(reason or f"{order[0]}: unavailable",
                                           failed=order[0], served=name)
            return
        except Exception as exc:                            # noqa: BLE001 - deliberate fallback
            if emitted:
                # Audio is already on the way to the browser. Replaying would dub over it.
                log.warning("tts %s failed AFTER audio had started; not retrying: %s", name, exc)
                raise
            last_error = exc
            if index == 0 and len(order) > 1:
                reason = f"{name}: {exc}"
                log.warning("tts %s failed (%s), falling back to %s", name, exc, order[1])
                # Note it immediately so a long line still shows the amber row while it
                # synthesises. `served` is corrected to the real engine on success above.
                Settings.note_tts_fallback(reason, failed=name, served=order[1])
                continue
            raise

    raise TTSError(f"no TTS provider could speak: {last_error}")


async def synthesize_stream(text: str, ui_lang: str = "en") -> AsyncIterator[bytes]:
    """Stream raw mono 16-bit PCM. The browser builds audio on the fly.

    Each yielded chunk is a slice of 16-bit signed little-endian PCM at SAMPLE_RATE Hz,
    mono. No WAV header: the frontend reads the format from the response headers.
    """
    async for chunk in _speak_pcm(text, ui_lang):
        yield chunk


async def synthesize_pcm(text: str, ui_lang: str = "en") -> bytes:
    """Synthesise the whole line and return one PCM buffer, raising TTSError on failure.

    The HTTP layer calls this rather than synthesize_stream() on purpose. Every provider here
    is effectively synchronous from our side, so doing the work BEFORE the response starts
    means a failure can still be reported as a real HTTP status (502) instead of a 200 with
    an empty body that the browser cannot tell from silence.
    """
    pcm = bytearray()
    async for chunk in _speak_pcm(text, ui_lang):
        pcm += chunk
    if not pcm:
        raise TTSError("TTS returned no audio")
    return bytes(pcm)


async def synthesize(text: str, ui_lang: str = "en") -> SpeechResult:
    """Wait for the full clip and return it as a single WAV file.

    Kept for callers that want a self-contained audio payload (e.g. tests, or any consumer
    that can't stream). The browser path uses synthesize_stream() instead.
    """
    pcm = bytearray()
    async for chunk in _speak_pcm(text, ui_lang):
        pcm += chunk

    if not pcm:
        raise TTSError("TTS returned no audio")

    return SpeechResult(wav_bytes=_pcm_to_wav(bytes(pcm), SAMPLE_RATE),
                        language=language_for(ui_lang),
                        engine=f"{Settings.tts_provider()}")
