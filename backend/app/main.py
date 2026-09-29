import asyncio
import json as _json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .config import Settings
from .stt import ASRError, TencentRealtimeASR
from .tts import TTSError, SAMPLE_RATE, synthesize_stream
from .llm_client import check_llm_health
from .session import STAGES, get_session
from .data import POLICY
from .audit import AUDIT
from .orchestrator import (TurnRequest, TurnResponse, ConfirmRequest, run_turn, run_confirmation,
                            FieldSignalRequest, run_field_signal)
from .scenarios import SCENARIOS, ScenarioResponse, run_scenario

# 13. FastAPI surface
# =====================================================================
app = FastAPI(title="AuntieJoy", version="1.0",
              description="DBS DCTA Scenario 1, voice-first conversational payments. "
                          "Four bounded agents, sixteen deterministic controls.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health() -> dict:
    configured = Settings.LIVE_LLM
    reachable = await check_llm_health() if configured else False
    return {
        "status": "ok",
        # engine reflects ACTUAL reachability, not mere key presence; a key can be set while
        # the upstream service is down, and /health must not claim "live" in that case.
        "engine": "live" if reachable else "mock",
        "llm_configured": configured,
        "llm_reachable": reachable,
        "llm_base_url": Settings.LLM_BASE_URL if configured else None,
        "model": Settings.LLM_MODEL if configured else "deterministic-mock",
        "prompt_version": Settings.PROMPT_VERSION,
        "schema_version": Settings.SCHEMA_VERSION,
        # Ears and mouth report mere CONFIGURATION here. A configured key does not mean the
        # upstream is reachable, so /health must never claim "live" on key presence alone.
        "asr_configured": Settings.LIVE_ASR,
        "asr_engines": {"en": "16k_zh_en", "zh": "16k_zh_en", "bm": "16k_multi_lang"},
        "asr_reports_confidence": False,   # Tencent realtime ASR has no confidence field
        "tts_configured": Settings.LIVE_TTS,
        "tts_model": Settings.TTS_MODEL or "sonic-latest",
        # True when ANY voice id exists; the per-language ones are optional extras, so a
        # single-voice setup must still report a working mouth.
        "tts_voice_set": Settings.tts_voice_configured(),
        "tts_voice_per_language": {
            "EN": bool(Settings.tts_voice_for("EN")),
            "ZH": bool(Settings.tts_voice_for("ZH")),
            "BM": bool(Settings.tts_voice_for("BM")),
        },
        "agents": ["GUARDRAILS AGENT", "INTENT CLASSIFIER", "PAYMENT AGENT",
                   "INDEPENDENT VALIDATION AGENT", "PERSONALIZATION AGENT"],
        "stages": len(STAGES),
        # The MECE failure map is branches A-H. Counted from SCENARIOS so it can never drift
        # out of sync with the UI rail (which labels the map "A-H / 20 NODES"). The HAPPY
        # path is recorded under branch "-", so it is excluded from the A-H total.
        "branches": f"A-H / {sum(1 for v in SCENARIOS.values() if v.get('branch') in 'ABCDEFGH')} scenarios",
        "disclosure": POLICY["disclosure"],
        "audit_db": Settings.DB_PATH,
    }


@app.get("/stages")
async def stages() -> dict:
    return {"stages": [{"stage": n, "control": c, "type": t} for n, c, t in STAGES]}


@app.get("/scenarios")
async def scenarios() -> dict:
    branch_labels = {
        "A": "Wrong person or untrusted input",
        "B": "Auntie is misunderstood",
        "C": "Malicious instructions affect the workflow",
        "D": "Auntie is being manipulated",
        "E": "Auntie does not understand the payment",
        "F": "Wrong, forged or repeated payment executed",
        "G": "Multiple or out-of-shape request",
    }
    return {
        "scenarios": [{"id": k, "branch": v["branch"], "title": v["title"],
                       "turns": len(v["turns"]), "confirmation": bool(v.get("confirm"))}
                      for k, v in SCENARIOS.items()],
        "branches": [{"branch": b, "label": label,
                      "scenarios": [k for k, v in SCENARIOS.items() if v["branch"] == b]}
                     for b, label in branch_labels.items()],
    }


@app.post("/turn", response_model=TurnResponse)
async def turn(req: TurnRequest) -> TurnResponse:
    return await run_turn(req)


@app.post("/confirm", response_model=TurnResponse)
async def confirm(req: ConfirmRequest) -> TurnResponse:
    return await run_confirmation(req)


@app.post("/field_signal", response_model=TurnResponse)
async def field_signal(req: FieldSignalRequest) -> TurnResponse:
    return await run_field_signal(req)


@app.post("/scenario/{scenario_id}", response_model=ScenarioResponse)
async def scenario(scenario_id: str) -> ScenarioResponse:
    return await run_scenario(scenario_id.upper())


@app.get("/audit/{session_id}")
async def audit(session_id: str) -> dict:
    return {"session_id": session_id, "events": AUDIT.events(session_id),
            "chain": AUDIT.verify_chain(session_id)}


@app.get("/session/{session_id}")
async def session_detail(session_id: str) -> dict:
    return get_session(session_id).to_public()


# =====================================================================
# 14. Ears, streaming speech to text (Tencent Cloud realtime ASR)
#
# Protocol (browser -> server):
#   {"type":"start","language":"en"}   open one ASR session for one utterance
#   <binary frames>                    16 kHz, 16-bit, mono PCM, ~200 ms per frame
#   {"type":"end"}                     utterance finished; server replies with the final
# Server -> browser:
#   {"type":"ready","live":bool}       on connect, tells the client whether ASR can run
#   {"type":"started","engine":...}    session open
#   {"type":"interim","text":...}      partial result
#   {"type":"final","text":...,"confidence":..|null,"confidence_source":...}
#   {"type":"error","message":...}     the browser should fall back to the Web Speech API
# =====================================================================
@app.websocket("/ws/stt")
async def ws_stt(ws: WebSocket) -> None:
    await ws.accept()
    asr: TencentRealtimeASR | None = None
    try:
        await ws.send_json({"type": "ready", "live": Settings.LIVE_ASR})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            chunk = msg.get("bytes")
            if chunk:
                if asr is None:
                    await ws.send_json({"type": "error",
                                        "message": "send {\"type\":\"start\"} before audio"})
                    continue
                try:
                    await asr.send_audio(chunk)
                except ASRError as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    break
                continue

            raw = msg.get("text")
            if not raw:
                continue
            try:
                data = _json.loads(raw)
            except ValueError:
                continue

            kind = data.get("type")
            if kind == "start":
                language = data.get("language") or Settings.ASR_LANGUAGE

                def _send(payload: dict) -> None:
                    # fire-and-forget: results must not block audio upload
                    asyncio.create_task(ws.send_json(payload))

                def on_interim(text: str) -> None:
                    _send({"type": "interim", "text": text})

                def on_final(result) -> None:
                    _send({"type": "final", "text": result.text,
                           "confidence": result.confidence,
                           "confidence_source": result.confidence_source,
                           "engine": result.engine})

                asr = TencentRealtimeASR(language=language,
                                         on_interim=on_interim, on_final=on_final)
                try:
                    await asr.connect()
                except ASRError as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    asr = None
                    continue
                await ws.send_json({"type": "started", "engine": asr.engine,
                                    "language": language})
            elif kind == "end":
                if asr is not None:
                    result = await asr.finish()
                    if result is not None:
                        await ws.send_json({"type": "final", "text": result.text,
                                            "confidence": result.confidence,
                                            "confidence_source": result.confidence_source,
                                            "engine": result.engine})
                    else:
                        await ws.send_json({"type": "final", "text": "",
                                            "confidence": None,
                                            "confidence_source": "unavailable",
                                            "engine": asr.engine})
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # never let a probe take the server down
        try:
            await ws.send_json({"type": "error", "message": f"stt failed: {exc}"})
        except Exception:
            pass
    finally:
        if asr is not None:
            await asr.close()


# =====================================================================
# 15. Mouth, text to speech (Cartesia Sonic), proxied so the key stays server-side.
#
# The browser POSTs the INTERFACE_LINE text (and language) here; we stream it from
# Cartesia and return one WAV. The frontend never sees the Cartesia key.
# =====================================================================
class TTSRequest(BaseModel):
    text: str
    language: str = "en"   # EN | ZH | BM  (mapped to a Cartesia code in tts.py)


@app.post("/tts")
async def tts(req: TTSRequest) -> StreamingResponse:
    """Stream Cartesia audio to the browser as it is synthesised.

    The body is raw mono 16-bit signed little-endian PCM at SAMPLE_RATE Hz, no WAV
    container, so the first byte the browser receives is the first byte of audio it
    can play. The matching audio parameters are echoed in response headers so the
    frontend knows what to feed into Web Audio without an out-of-band handshake.
    """
    # Gated on the voice for THIS language: a language with no id of its own falls back to
    # the shared voice, so the mouth only goes quiet when there is no voice at all.
    if not Settings.LIVE_TTS or not Settings.tts_voice_for(req.language):
        # Same contract as before, the frontend disables its mouth permanently on 503.
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="tts not configured")

    async def _pcm_stream():
        try:
            async for chunk in synthesize_stream(req.text, req.language):
                yield chunk
        except TTSError as exc:
            # Once the response body has started, headers are already committed; we can't
            # flip the status code. The browser logs the broken pipe and falls back; logging
            # the upstream reason here keeps the audit trail honest.
            import logging
            logging.getLogger("auntiejoy.tts").warning("stream aborted: %s", exc)
            return

    return StreamingResponse(
        _pcm_stream(),
        media_type="application/octet-stream",
        headers={
            # Tell the browser exactly what format to expect, Cartesia's sonic-latest is
            # always mono 16-bit little-endian PCM at the sample rate we asked for.
            "X-Audio-Sample-Rate": str(SAMPLE_RATE),
            "X-Audio-Channels": "1",
            "X-Audio-Bit-Depth": "16",
            "X-Audio-Encoding": "pcm_s16le",
            # Disable proxy buffering so the first chunk reaches the browser ASAP.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# =====================================================================
