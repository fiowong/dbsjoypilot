"""Ears, speech to text.

Provider: Tencent Cloud realtime ASR over WebSocket (product doc 1093/48982).

Two rules this module exists to protect:

1. **The engine is a provider, not a decider.** It turns sound into text. It never decides
   what AuntieJoy says or does. Everything downstream is owned by the orchestrator.

2. **Never invent evidence.** Tencent's realtime ASR does not return a confidence score,
   the result carries only per-word ``stable_flag``. So instead of reporting a number the
   engine never gave, we derive a proxy from how many words the engine itself considers
   stable, and we return that *together with* ``confidence_source`` so the audit trail can
   always tell a measured signal from an assumption. A missing confidence is reported as
   ``None``, never as a flattering placeholder.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from .config import Settings, active_proxy

log = logging.getLogger("auntiejoy.stt")

HOST = "asr.cloud.tencent.com"

# Tencent has no single engine that covers English + Chinese + Malay, so two are needed, and
# the Auto mic pill therefore has to sacrifice one language.
#   16k_zh_en     -> Chinese + English (+31 dialects), auto-detects between them
#   16k_multi_lang-> English, Malay and 13 others, auto-detects at sentence level; no Chinese
#
# Auto resolves to 16k_zh_en, deliberately. The rule it follows is that Auto must never be
# WORSE than the pill it replaces: the default pill is EN, which already opens 16k_zh_en, so
# Auto as English + Chinese is a strict superset of the default and can only add recognition.
# Auto as 16k_multi_lang would DROP Chinese for anyone who selected it -- giving up the larger
# Singapore language group, and giving it up silently, because the wrong engine still returns a
# confident transcript rather than an error. Malay keeps its own explicit pill and its own
# engine. (Verified against the live API: both engines authenticate and transcribe; the choice
# is about coverage, not about either engine being broken.)
#
# "multilingual" is kept as an alias of "auto" so a client still sending the old value keeps
# working rather than silently falling through to the EN default.
ENGINE_BY_LANGUAGE = {
    "en": "16k_zh_en",
    "zh": "16k_zh_en",
    "mixed": "16k_zh_en",
    "bm": "16k_multi_lang",
    "auto": "16k_zh_en",
    "multilingual": "16k_zh_en",
}

# ``word_info`` (which is what makes per-word stable_flag available) is only supported on
# some engines. Without it there is no stability signal at all, so confidence is unknown.
#
# 16k_multi_lang IS in this set, and that matters because it is the engine Malay runs on:
# leaving it out meant BM was the one demo language whose confidence was always "unreported",
# so the frontend sent its 0.93 stand-in and the UI read "clarity not reported" for every
# Malay turn. Verified live against the API (three Malay utterances, all returning a populated
# word_list and stable_flag data). The value is threshold-neutral -- 0.93 stand-in and 1.0
# measured both sit above the 0.60 audio-risk floor and the 0.85 ambiguity threshold -- so this
# swaps a placeholder for a real measurement without changing any decision.
#
# Caveat worth keeping in view: stable_flag measures whether the ENGINE has stopped revising a
# word, not whether the word is RIGHT. Malay is read back by the English voice (no Malay voice
# exists in the international catalogue), and the engine can be confidently wrong about it --
# measured 1.0 on a badly mangled transcription. So this is a genuine engine-produced signal,
# honestly labelled "engine_stability", and never a claim of transcription accuracy.
WORD_INFO_ENGINES = {
    "16k_zh_en", "16k_zh", "8k_zh", "8k_zh_finance", "16k_zh-TW",
    "16k_en", "8k_en", "8k_zh_large", "16k_en_large", "16k_ca", "16k_ja",
    "16k_multi_lang",
}

# ``hotword_list`` is a per-engine capability too, and it is checked BEFORE the parameter is sent
# because the failure is not a dropped feature, it is a REJECTED HANDSHAKE. 16k_multi_lang - the
# only engine that covers Malay - answers 4001 参数不合法 with "the hotword function does not
# support [engine_model_type: 16k_multi_lang]", so the BM mic died and fell back to the browser
# recogniser while English and Chinese (both 16k_zh_en) worked fine. An engine that cannot bias
# toward payee names still CONNECTS without the bias; a misheard name is then caught by the
# downstream "did you mean X?" recovery instead.
HOTWORD_ENGINES = {
    "16k_zh_en", "16k_zh", "8k_zh", "8k_zh_finance", "16k_zh-TW",
    "16k_en", "8k_en", "8k_zh_large", "16k_en_large", "16k_ca", "16k_ja",
}

SAMPLE_RATE = 16000          # Tencent accepts 8k or 16k; 16k is what we capture
VOICE_FORMAT_PCM = 1         # voice_format=1 -> raw PCM
CHUNK_MS = 200               # doc recommends ~200ms of audio per frame, sent 1:1 realtime

# Tencent realtime ASR hotword_list: a comma-joined set of "Word|weight" entries. weight 1-11
# (11 = the strongest bias toward that spelling); up to 128 entries. We bias toward the
# customer's OWN payee names and the nicknames they actually use out loud, so a misheard name
# ("me tan") arrives correctly spelled ("Mei Tan") and never needs the downstream "did you mean?"
# recovery. (Option A, this is the upstream-of-Option-B prevention layer.)
HOTWORD_WEIGHT = 11
HOTWORD_MAX_ENTRIES = 128


def build_hotword_list(names, weight: int = HOTWORD_WEIGHT,
                       max_entries: int = HOTWORD_MAX_ENTRIES) -> Optional[str]:
    """Turn a list of candidate names into a Tencent hotword_list string, or None if empty.

    De-duplicates case-insensitively, drops blank or unsafe names (a name containing a comma
    or pipe would corrupt the "Word|weight" framing), and caps at ``max_entries`` so a customer
    with many payees can never exceed the engine's 128-entry limit.
    """
    if not names:
        return None
    seen: set = set()
    entries: list = []
    for name in names:
        clean = (name or "").strip()
        # A comma or pipe would break the "Word|weight", comma-joined framing Tencent expects.
        if not clean or "," in clean or "|" in clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append(f"{clean}|{int(weight)}")
        if len(entries) >= max_entries:
            break
    return ",".join(entries) if entries else None


class ASRError(RuntimeError):
    """Raised when the engine cannot be used (bad config, auth failure, quota, ...)."""


@dataclass
class ASRResult:
    text: str
    confidence: Optional[float]          # None = the engine did not report one
    confidence_source: str               # "engine_stability" | "unavailable"
    engine: str
    is_final: bool


def engine_for(language: str) -> str:
    return ENGINE_BY_LANGUAGE.get((language or "en").lower(), "16k_zh_en")


def _signed_url(appid: str, secret_id: str, secret_key: str, params: dict) -> str:
    """Build the wss URL with the HMAC-SHA1 signature Tencent expects.

    Signature original text = "<host>/asr/v2/<appid>?<sorted params as k=v, joined by &>",
    signed with SecretKey, base64'd, then appended as ``signature``.

    Tencent signs over the RAW, un-encoded param values, but the request URL itself must carry
    PERCENT-ENCODED values. The two differ as soon as a value contains a space, pipe or comma -
    which ``hotword_list`` always does ("Mei Tan|11,John Doe|11"). So we build the signature over
    the raw values and publish an encoded URL; a single string used for both would either be
    rejected (4002) or transcribe the hotwords wrong.
    """
    original = f"{HOST}/asr/v2/{appid}?" + "&".join(
        f"{k}={params[k]}" for k in sorted(params)
    )
    digest = hmac.new(secret_key.encode(), original.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()
    # The published query string percent-encodes every value (so spaces/pipes/commas are safe in
    # the WebSocket URL); the signature above stays over the raw values Tencent expects.
    encoded = "&".join(
        f"{k}={quote(str(params[k]), safe='')}" for k in sorted(params)
    )
    return f"wss://{HOST}/asr/v2/{appid}?{encoded}&signature={quote(signature, safe='')}"


class TencentRealtimeASR:
    """One WebSocket session = one utterance stream.

    Client sends binary PCM frames; Tencent streams back interim results and a final one.
    Call ``finish()`` to tell the engine the audio ended and collect the last result.
    """

    def __init__(self, language: str = "en",
                 on_interim: Optional[Callable[[str], None]] = None,
                 on_final: Optional[Callable[[ASRResult], None]] = None,
                 hotwords: Optional[str] = None):
        self.language = (language or "en").lower()
        self.engine = engine_for(self.language)
        self.on_interim = on_interim
        self.on_final = on_final
        self.hotwords = hotwords
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._last_result: Optional[ASRResult] = None
        self._finished = asyncio.Event()

    # ---------- lifecycle ----------
    async def connect(self) -> None:
        if not Settings.LIVE_ASR:
            raise ASRError(
                "ASR is not configured, set TENCENTCLOUD_SECRET_ID, "
                "TENCENTCLOUD_SECRET_KEY and TENCENTCLOUD_APPID"
            )
        try:
            import websockets  # imported lazily so the app still boots without it
        except ImportError as exc:  # pragma: no cover
            raise ASRError("the 'websockets' package is not installed") from exc

        now = int(time.time())
        params = {
            "secretid": Settings.ASR_SECRET_ID,
            "timestamp": str(now),
            "expired": str(now + 3600),
            "nonce": str(now % 100000000),
            "engine_model_type": self.engine,
            "voice_id": uuid.uuid4().hex[:32],
            "voice_format": str(VOICE_FORMAT_PCM),
            "needvad": "1",
            # No vad_silence_time: end-of-utterance is driven by the explicit {"type":"end"}
            # the client sends (stt.js), so we don't send a per-engine silence timeout that
            # some engines (e.g. 16k_multi_lang) ignore -- keeps Malay and others consistent.
            "filter_empty_result": "1",
        }
        # Only ask for per-word detail when the engine can actually give it; that detail
        # is the sole basis for our confidence proxy.
        if self.engine in WORD_INFO_ENGINES:
            params["word_info"] = "1"

        # Bias the engine toward the customer's own payee names / nicknames (see build_hotword_list),
        # so a misheard name arrives correctly spelled. The value is percent-encoded in the URL by
        # _signed_url; the signature is computed over the raw value. Optional: absent -> no bias.
        # Sent ONLY to engines that accept it (see HOTWORD_ENGINES): on 16k_multi_lang the parameter
        # does not degrade gracefully, it rejects the handshake outright.
        sent_hotwords = (self.hotwords if self.hotwords and self.engine in HOTWORD_ENGINES
                         else None)
        if sent_hotwords:
            params["hotword_list"] = sent_hotwords
        elif self.hotwords:
            log.info("ASR engine %s does not support hotword_list; connecting without payee-name "
                     "biasing (the 'did you mean?' recovery still covers a misheard name)",
                     self.engine)

        url = _signed_url(Settings.ASR_APPID, Settings.ASR_SECRET_ID,
                          Settings.ASR_SECRET_KEY, params)
        # One line so a proxied egress is visible instead of surfacing as an unexplained
        # handshake failure. Decides nothing; the client is pinned to USE_ENV_PROXY below.
        proxy = active_proxy()
        if proxy:
            log.info("ASR connecting via proxy %s", proxy)
        try:
            # proxy=None means "connect directly". websockets defaults to proxy=True, which
            # means "use the proxy in HTTP_PROXY/HTTPS_PROXY" -- the same trap httpx has, and
            # it fails as a generic handshake error that reads like bad credentials. Opt in
            # with AUNTIEJOY_USE_ENV_PROXY=1 on a network that genuinely needs a proxy.
            self._ws = await asyncio.wait_for(
                websockets.connect(url, max_size=None, ping_interval=None,
                                   proxy=(True if Settings.USE_ENV_PROXY else None)),
                timeout=10
            )
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise ASRError("ASR handshake timed out") from exc
        except Exception as exc:
            raise ASRError(f"ASR connection failed: {exc}") from exc

        msg = _as_json(raw)
        if msg is None or msg.get("code") != 0:
            raise ASRError(f"ASR handshake rejected: {_code_meaning(msg)}")

        self._finished.clear()
        self._reader = asyncio.create_task(self._read_loop())
        log.info("ASR connected: engine=%s language=%s hotwords=%s",
                 self.engine, self.language, sent_hotwords)

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None:
            raise ASRError("ASR is not connected")
        try:
            await self._ws.send(pcm)
        except Exception as exc:
            raise ASRError(f"ASR audio send failed: {exc}") from exc

    async def finish(self, timeout: float = 6.0) -> Optional[ASRResult]:
        """Tell the engine the utterance is over and wait for its final result."""
        if self._ws is None:
            return None
        try:
            await self._ws.send(json.dumps({"type": "end"}, ensure_ascii=False))
            await asyncio.wait_for(self._finished.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("ASR did not confirm completion in %.1fs", timeout)
        return self._last_result

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._ws = None

    # ---------- inbound ----------
    async def _read_loop(self) -> None:
        try:
            while self._ws is not None:
                raw = await self._ws.recv()
                if isinstance(raw, (bytes, bytearray)):
                    continue
                msg = _as_json(raw)
                if msg is None:
                    continue
                self._handle(msg)
                if msg.get("final") == 1:
                    break
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("ASR read loop ended: %s", exc)
        finally:
            self._finished.set()

    def _handle(self, msg: dict) -> None:
        code = msg.get("code", 0)
        if code != 0:
            log.error("ASR error %s: %s", code, msg.get("message"))
            return

        result = msg.get("result") or {}
        text = (result.get("voice_text_str") or "").strip()
        if not text:
            return

        slice_type = result.get("slice_type")
        # slice_type: 0 = utterance start, 1 = interim (may still change), 2 = stable
        if slice_type == 2:
            final = ASRResult(
                text=text,
                confidence=_stability_confidence(result),
                confidence_source="engine_stability"
                if _stability_confidence(result) is not None else "unavailable",
                engine=self.engine,
                is_final=True,
            )
            self._last_result = final
            if self.on_final:
                self.on_final(final)
        elif slice_type in (0, 1):
            if self.on_interim:
                self.on_interim(text)


def _stability_confidence(result: dict) -> Optional[float]:
    """Proxy for 'how sure is the engine' from the only signal it gives us.

    Tencent marks each recognised word with ``stable_flag``: 1 means the engine has settled
    on that word and will not revise it. The share of settled words is a genuine, engine-
    produced measure of how cleanly the audio was understood, so we use it, and we label
    it as such rather than dressing it up as a vendor confidence score.
    """
    words = result.get("word_list") or []
    if not words:
        return None
    stable = sum(1 for w in words if w.get("stable_flag") == 1)
    return round(stable / len(words), 3)


def _as_json(raw) -> Optional[dict]:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _code_meaning(msg: Optional[dict]) -> str:
    if not msg:
        return "no response"
    meanings = {
        4002: "authentication failed (check SecretId/SecretKey)",
        4003: "ASR service not activated for this AppID",
        4004: "ASR resource pack exhausted",
        4005: "account is in arrears",
        4006: "concurrency limit exceeded",
        4007: "audio decode failed",
        6001: "overseas calls must use the Tencent Cloud international site",
    }
    code = msg.get("code")
    return f"{code}, {meanings.get(code, msg.get('message', 'unknown'))}"
