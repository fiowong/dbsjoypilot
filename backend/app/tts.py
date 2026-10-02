"""Mouth, text to speech.

Provider: Cartesia Sonic, streamed over its WebSocket API. The backend forwards Cartesia's
audio chunks to the browser as soon as they arrive (no waiting for the full clip), so the
customer hears Auntie's first words within ~half a second of the interface bubble appearing
instead of after the whole sentence has been synthesised. The Cartesia key stays server-side
(zero-trust: the browser never sees it); the frontend only ever receives audio bytes from our
own /tts endpoint.

Design rule (same as the ears): the engine is a *provider*, not a decider. It turns the
backend's INTERFACE_LINE text into sound. It never chooses what AuntieJoy says, the
orchestrator / messages.py catalogue does.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import uuid
import wave
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

import websockets

from .config import Settings

log = logging.getLogger("auntiejoy.tts")

# Cartesia streaming TTS WebSocket. The apiKey query param is the universally-supported
# auth path; we ALSO send the X-API-Key header (the documented server auth) so either form
# is accepted. cartesia_version is a required connection parameter.
CARTESIA_WS = "wss://api.cartesia.ai/tts/websocket"
CARTESIA_VERSION = "2026-08-14"
SAMPLE_RATE = 24000

# Our UI language (EN/ZH/BM) -> Cartesia language code.
LANG_TO_CARTESIA = {"en": "en", "zh": "zh", "mixed": "en", "bm": "ms"}


class TTSError(RuntimeError):
    """Raised when speech cannot be produced (no config, auth failure, bad model/voice)."""


@dataclass
class SpeechResult:
    wav_bytes: bytes
    language: str
    engine: str = "cartesia"


def language_for(ui_lang: str) -> str:
    u = (ui_lang or "en").lower()
    # Already a Cartesia code? (en / zh / ms), pass through untouched.
    if u in ("en", "zh", "ms"):
        return u
    # Otherwise treat as our UI language (EN / ZH / BM / mixed) and map it.
    return LANG_TO_CARTESIA.get(u, "en")


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw mono 16-bit PCM in a minimal WAV container for the browser to decode."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def _cartesia_pcm_chunks(text: str, ui_lang: str) -> AsyncIterator[bytes]:
    """Connect to Cartesia and yield raw mono 16-bit PCM chunks as they arrive.

    Raises TTSError on any failure (no config, bad auth, connection error, Cartesia error).
    On a clean shutdown the connection is closed and the iterator simply ends.
    """
    if not Settings.LIVE_TTS:
        raise TTSError(
            "TTS is not configured, set CARTESIA_API_KEY (and CARTESIA_VOICE_ID)"
        )
    # The voice follows the language Auntie is answering in, so the same conversation can
    # switch tongue without her voice staying stuck in the first one. Falls back to the
    # single shared voice when no per-language id is configured (see Settings.tts_voice_for).
    voice_id = Settings.tts_voice_for(ui_lang)
    if not voice_id:
        raise TTSError("TTS voice not configured, set CARTESIA_VOICE_ID (or one "
                       "CARTESIA_VOICE_ID_EN / _ZH / _BM per language)")

    lang = language_for(ui_lang)
    model = Settings.TTS_MODEL or "sonic-latest"
    # Log the voice + model + language on every synthesis so the operator can confirm which
    # Cartesia voice is actually speaking (e.g. after a credit refill, or a per-lang swap).
    # /health only exposes the model and a bool per language, never the voice id itself.
    log.info("tts cart: lang=%s voice_id=%s model=%s text_len=%d",
             ui_lang, voice_id, model, len(text))
    params = {"cartesia_version": CARTESIA_VERSION, "apiKey": Settings.TTS_API_KEY}
    url = f"{CARTESIA_WS}?{urlencode(params)}"
    headers = {"X-API-Key": Settings.TTS_API_KEY}

    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers, max_size=None,
                               ping_interval=None),
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - surface any connection problem to the caller
        raise TTSError(f"Cartesia connection failed: {exc}") from exc

    try:
        await ws.send(json.dumps({
            "model_id": model,
            "transcript": text,
            "voice": voice_id,
            "language": lang,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
            },
            "context_id": uuid.uuid4().hex,
        }))

        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                # Cartesia speaks in JSON text frames; ignore any stray binary.
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "chunk":
                data = msg.get("data")
                if data:
                    pcm = base64.b64decode(data)
                    if pcm:
                        yield pcm
                if msg.get("done"):
                    break
            elif mtype == "done":
                break
            elif mtype == "error":
                raise TTSError(
                    f"Cartesia error: {msg.get('message')} "
                    f"({msg.get('error_code')})"
                )
            # timestamps / flush_done are ignored
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def synthesize_stream(text: str, ui_lang: str = "en") -> AsyncIterator[bytes]:
    """Stream raw mono 16-bit PCM from Cartesia. The browser builds audio on the fly.

    Each yielded chunk is a complete slice of 16-bit signed little-endian PCM at
    SAMPLE_RATE Hz, mono. No WAV header, the frontend knows the format out-of-band
    (it always matches Cartesia's output_format we asked for).
    """
    async for chunk in _cartesia_pcm_chunks(text, ui_lang):
        yield chunk


async def synthesize(text: str, ui_lang: str = "en") -> SpeechResult:
    """Legacy helper: wait for the full clip and return it as a single WAV file.

    Kept for callers that want a self-contained audio payload (e.g. tests, or any
    consumer that can't stream). The browser path uses synthesize_stream() instead
    so the customer hears the first chunk long before Cartesia finishes the sentence.
    """
    pcm = bytearray()
    async for chunk in _cartesia_pcm_chunks(text, ui_lang):
        pcm += chunk

    if not pcm:
        raise TTSError("Cartesia returned no audio")

    return SpeechResult(wav_bytes=_pcm_to_wav(bytes(pcm), SAMPLE_RATE), language=lang)
