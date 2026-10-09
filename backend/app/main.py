import asyncio
import json as _json

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .config import Settings
from .stt import ASRError, TencentRealtimeASR, build_hotword_list, engine_for
from .tts import TTSError, SAMPLE_RATE, synthesize_pcm, provider_order
from .repository import get_repository
from . import nicknames as customer_nicknames
from .llm_client import check_llm_health
from .session import STAGES, get_session
from .data import POLICY
from .audit import AUDIT
from .orchestrator import (TurnRequest, TurnResponse, ConfirmRequest, run_turn, run_confirmation,
                            FieldSignalRequest, run_field_signal)
from .scenarios import ALL_SCENARIOS, SCENARIOS, ScenarioResponse, run_scenario

# ---- logging ----------------------------------------------------------------
# Uvicorn only wires up its own `uvicorn*` loggers, so every `auntiejoy.*` logger
# (tts, stt, audit) ends up with no handler at all. Python's built-in "lastResort"
# handler is WARNING-only, which silently drops everything we emit at INFO - that is
# why `tts cart: lang=... voice_id=...` never reached the terminal. Attach one console
# handler to the shared "auntiejoy" parent; child loggers propagate up to it.
import logging

_auntiejoy_log = logging.getLogger("auntiejoy")
_auntiejoy_log.setLevel(logging.INFO)
if not _auntiejoy_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
    _auntiejoy_log.addHandler(_handler)

# How much PCM to hand the browser per chunk on /tts. Small enough that playback can start
# on the first slice, large enough not to flood the socket with 1-2 kB writes.
_TTS_RELAY_BYTES = 4096

# 13. FastAPI surface
# =====================================================================
app = FastAPI(title="AuntieJoy", version="1.0",
              description="DBS DCTA Scenario 1, voice-first conversational payments. "
                          "Four bounded agents, sixteen deterministic controls.")
# expose_headers is load-bearing, not cosmetic. tts.js reads X-TTS-Provider off the
# /tts response to label the SPEAKING row with the engine that actually produced THAT
# audio. Without an explicit expose_headers, a browser silently hides every custom
# response header from JS on a cross-origin fetch -- which is exactly the local dev
# shape this whole three-tier API_BASE resolution exists to support (static frontend
# on one port, backend on another). Same-origin deployment would hide the bug, so the
# omission would only ever show up on a developer's laptop.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-TTS-Provider", "X-TTS-Selected", "X-TTS-Fallback"])


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
        # Derived from stt.ENGINE_BY_LANGUAGE rather than repeated as literals here, so the
        # engine reported for a language can never drift from the engine actually opened.
        # "auto" is the Auto mic pill: Tencent has no single engine covering English +
        # Chinese + Malay, so Auto picks one of the two rather than making the customer
        # choose before speaking. See stt.ENGINE_BY_LANGUAGE for which, and why.
        "asr_engines": {lang: engine_for(lang) for lang in ("en", "zh", "bm", "auto")},
        "asr_reports_confidence": False,   # Tencent realtime ASR has no confidence field
        "tts_configured": Settings.LIVE_TTS,
        # ---- WHICH MOUTH IS SPEAKING ----
        # The provider in force RIGHT NOW. Not necessarily the configured default: the UI's
        # "TRTC Mode" toggle changes this at runtime via POST /tts/provider.
        "tts_provider": Settings.tts_provider(),
        # What the environment asked for, so a runtime switch is distinguishable from the
        # deployed configuration rather than silently replacing it in the report.
        "tts_provider_default": Settings.TTS_PROVIDER_DEFAULT,
        # Is the Tencent toggle the one currently selected? This is the field the UI's
        # "TRTC Mode" switch reflects.
        "tts_provider_is_tencent": Settings.tts_provider_is_tencent(),
        # Can each mouth speak? Reported per provider, because with a fallback in play
        # "tts_configured" alone no longer says WHICH engine will answer.
        "tts_providers": {
            "cartesia": Settings.cartesia_configured(),
            "tencent": Settings.tencent_configured(),
        },
        # ---- THE REPORTED FALLBACK ----
        # True when the selected provider failed and the other one answered instead. The
        # reason carries the provider's own error text (e.g. "quota_exceeded"). This exists so
        # a fallback is never silent: a voice that changes without explanation is far more
        # confusing than one that says why.
        "tts_fallback_enabled": Settings.TTS_FALLBACK_ENABLED,
        "tts_fallback_active": Settings.TTS_FALLBACK_ACTIVE,
        "tts_fallback_reason": Settings.TTS_FALLBACK_REASON or None,
        # WHO failed and WHO answered. Reported as names rather than left for the reader to
        # infer, because with three canonical engines "the other one" is not well defined.
        "tts_fallback_from": Settings.TTS_FALLBACK_FROM or None,
        "tts_fallback_to": Settings.TTS_FALLBACK_TO or None,
        # The engine that produced the last line of audio. Differs from tts_provider only
        # while a fallback is in force, which is exactly when the distinction matters.
        "tts_served_provider": Settings.tts_served_provider(),
        # Which mouth actually answered the last synthesis, or None before the first one.
        # Distinguishes "installed but never exercised" from "actually streaming".
        "tts_streaming_active": (Settings.TTS_STREAMING_ACTIVE
                                 if Settings.tts_provider() == "texttospeechsse" else None),
        # Whether the streaming mouth COULD run: credentials + SdkAppId + a voice id. Shown
        # even when it is not the selected provider, so an operator can see the option exists.
        "tts_trtc_sse_configured": Settings.trtc_tts_configured(),
        # The engine name for whichever provider is active, in the vocabulary dashboards
        # already match on.
        "tts_engine": {"cartesia": "cartesia-sonic",
                       "texttospeechsse": "trtc-texttospeechsse"}.get(
                           Settings.tts_provider(), "tencent-tts"),
        # Cartesia's own endpoint/model, reported alongside Tencent's so the report describes
        # where the audio ACTUALLY came from rather than only where it used to.
        "tts_cartesia_endpoint": Settings.CARTESIA_ENDPOINT,
        "tts_cartesia_model": Settings.CARTESIA_MODEL,
        "tts_cartesia_api_version": Settings.CARTESIA_API_VERSION,
        "tts_endpoint": Settings.TTS_ENDPOINT,
        "tts_region": Settings.TTS_REGION,
        "tts_api_version": Settings.TTS_API_VERSION,
        # The streaming mouth talks to a DIFFERENT endpoint and service, so reporting the
        # synchronous one alone would misdescribe where the audio actually came from.
        "tts_sse_endpoint": Settings.TRTC_TTS_ENDPOINT,
        "tts_sse_region": Settings.TRTC_TTS_REGION,
        # The sample rate the browser must decode. Echoed here AND on every /tts response
        # (X-Audio-Sample-Rate) so the two sides can never drift apart unnoticed.
        "tts_sample_rate": SAMPLE_RATE,
        # True when ANY voice id exists for the ACTIVE provider; the per-language ones are
        # optional extras, so a single-voice setup must still report a working mouth.
        "tts_voice_set": Settings.tts_provider_is_configured(),
        "tts_voice_per_language": {
            "EN": bool(Settings.cartesia_voice_for("EN") if not Settings.tts_provider_is_tencent()
                       else Settings.tts_voice_for("EN")),
            "ZH": bool(Settings.cartesia_voice_for("ZH") if not Settings.tts_provider_is_tencent()
                       else Settings.tts_voice_for("ZH")),
            "BM": bool(Settings.cartesia_voice_for("BM") if not Settings.tts_provider_is_tencent()
                       else Settings.tts_voice_for("BM")),
        },
        # True when a language has no voice of its OWN and borrows another's. This is now
        # PROVIDER-DEPENDENT, and that is the whole point of the Cartesia default: Tencent's
        # international catalogue ships no Malay voice at all, so on the Tencent path BM is
        # read by a borrowed voice and this is true. Cartesia speaks Malay natively, so on the
        # Cartesia path it is FALSE. Reported rather than hidden, so nothing claims otherwise.
        "tts_voice_is_fallback": {
            "EN": Settings.tts_voice_is_fallback("EN") if Settings.tts_provider_is_tencent()
                  else Settings.cartesia_voice_is_fallback("EN"),
            "ZH": Settings.tts_voice_is_fallback("ZH") if Settings.tts_provider_is_tencent()
                  else Settings.cartesia_voice_is_fallback("ZH"),
            "BM": Settings.tts_voice_is_fallback("BM") if Settings.tts_provider_is_tencent()
                  else Settings.cartesia_voice_is_fallback("BM"),
        },
        # False (the default) means the ASR and TTS calls go DIRECT and ignore any
        # HTTP_PROXY/HTTPS_PROXY the host shell exports. Reported because a proxied voice
        # pipeline fails as a generic connect error, which is easy to misread as bad
        # credentials; this one field says which of the two it is. See config.py.
        "use_env_proxy": Settings.USE_ENV_PROXY,
        "agents": ["GUARDRAILS AGENT", "INTENT CLASSIFIER", "PAYMENT AGENT",
                   "INDEPENDENT VALIDATION AGENT", "PERSONALIZATION AGENT"],
        "stages": len(STAGES),
        # The MECE failure map is branches A-H. Counted from SCENARIOS so it can never drift
        # out of sync with the UI rail, which labels the map "BRD §6 · 21 NODES" -- hence
        # "nodes" here and not "scenarios". The HAPPY path and the six DEMO entries are
        # recorded under branch "-", so they are excluded from this total.
        # The membership test is an explicit set, NOT `branch in "ABCDEFGH"`: that substring
        # form is true for an EMPTY branch ("") as well, so one blank branch would silently
        # inflate the count and the map would disagree with the rail again.
        "branches": (f"A-H / "
                     f"{sum(1 for v in SCENARIOS.values() if v.get('branch') in set('ABCDEFGH'))}"
                     " nodes"),
        "disclosure": POLICY["disclosure"],
        # NOTE: no "payment_rail" key. That key reported which rail executes an authorised
        # payment -- "mock" (the deterministic in-memory ledger) or "dbs" (a real DBS Fund
        # Transfer request). This build keeps the ledger call inside orchestrator.py stage 15
        # and does NOT ship rails.py, so there is no rail to report and nothing that could
        # imply a live bank call.
        "audit_db": Settings.DB_PATH,
    }


@app.get("/stages")
async def stages() -> dict:
    return {"stages": [{"stage": n, "control": c, "type": t} for n, c, t in STAGES]}


@app.get("/scenarios")
async def scenarios() -> dict:
    # Kept in step with the frontend rail in translations.js BRANCHES. H was missing here,
    # so the API's branch list stopped at G while the UI drew eight groups.
    branch_labels = {
        "A": "Wrong person or untrusted input",
        "B": "Auntie is misunderstood",
        "C": "Malicious instructions affect the workflow",
        "D": "Auntie is being manipulated",
        "E": "Auntie does not understand the payment",
        "F": "Wrong, forged or repeated payment executed",
        "G": "Multiple or out-of-shape request",
        "H": "Auntie cannot cover the amount",
    }
    return {
        # The runnable set: the BRD/demo scenarios AND the folded-in regression pins, both
        # runnable through /scenario/{id}. `acceptance` below separates them for a caller that
        # only wants the set the spec asks for.
        "scenarios": [{"id": k, "branch": v["branch"], "title": v["title"],
                       "turns": len(v["turns"]), "confirmation": bool(v.get("confirm")),
                       "kind": "acceptance" if k in SCENARIOS else "regression"}
                      for k, v in ALL_SCENARIOS.items()],
        "branches": [{"branch": b, "label": label,
                      "scenarios": [k for k, v in ALL_SCENARIOS.items() if v["branch"] == b]}
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
# The demo customer. Mirrors session.Session's default: the prototype has a single customer and
# no real auth, so the STT socket resolves hotwords against this id unless the client passes one.
DEMO_CUSTOMER_ID = "cust_auntie_001"


def _hotwords_for(customer_id: str):
    """The hotword_list for one customer: their saved payee display names plus the nicknames
    they actually use out loud. Returns None when there is nothing useful to bias toward. A
    failure here must never stop the ears, so it degrades to None and logs instead of raising.
    """
    names = []
    try:
        names += [p.display_name for p in get_repository().get_saved_payees(customer_id)]
    except Exception as exc:  # pragma: no cover - defensive; repository is trusted + local
        _auntiejoy_log.warning("hotwords: could not read saved payees: %s", exc)
    try:
        names += list(customer_nicknames.index(customer_id).keys())
    except Exception as exc:  # pragma: no cover - defensive
        _auntiejoy_log.warning("hotwords: could not read nicknames: %s", exc)
    return build_hotword_list(names)


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
                # The client may name the customer (so hotwords match their own payees); the
                # demo has one customer, so fall back to the known id rather than dropping the bias.
                customer_id = data.get("customer_id") or DEMO_CUSTOMER_ID
                hotwords = _hotwords_for(customer_id)

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
                                         on_interim=on_interim, on_final=on_final,
                                         hotwords=hotwords)
                try:
                    await asr.connect()
                except ASRError as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    asr = None
                    continue
                await ws.send_json({"type": "started", "engine": asr.engine,
                                    "language": language, "hotwords": hotwords})
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
# 15. Mouth, text to speech (Tencent Cloud TextToVoice), proxied so the SecretKey stays
#     server-side.
#
# The browser POSTs the INTERFACE_LINE text (and language) here; we synthesise it with
# Tencent and relay the audio. The frontend never sees the Tencent SecretId/SecretKey.
# =====================================================================
class TTSRequest(BaseModel):
    text: str
    language: str = "en"   # EN | ZH | BM  (mapped to the active provider's voice in tts.py)


class TTSProviderRequest(BaseModel):
    # "cartesia" | "tencent" | "texttovoice" | "texttospeechsse"
    # "tencent" is the UI's word for the toggle; the backend resolves it to whichever
    # Tencent mouth is actually usable so the frontend never has to know whether streaming
    # is configured. An unknown value is a 400, not a silent no-op.
    provider: str


@app.post("/tts/provider")
async def set_tts_provider(req: TTSProviderRequest) -> dict:
    """Switch the speaking mouth at runtime. No restart, no redeploy.

    This is what the UI's "TRTC Mode" toggle calls. It changes ONLY which engine turns the
    already-decided INTERFACE_LINE into sound -- it cannot alter what is said, who authorises
    a payment, or any control-chain stage. Swapping the voice provider is not a security
    boundary and must never be treated as one.
    """
    try:
        active = Settings.set_tts_provider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logging.getLogger("auntiejoy.tts").info("tts provider switched to %s", active)
    return {
        "tts_provider": active,
        # Echo whether the newly selected mouth can actually speak, so the toggle can show a
        # problem immediately instead of waiting for the next spoken line to fail.
        "configured": Settings.tts_provider_is_configured(active),
        "fallback_active": Settings.TTS_FALLBACK_ACTIVE,
        "fallback_reason": Settings.TTS_FALLBACK_REASON,
    }


@app.post("/tts")
async def tts(req: TTSRequest) -> StreamingResponse:
    """Stream the active provider's TTS audio to the browser.

    The body is raw mono 16-bit signed little-endian PCM at SAMPLE_RATE Hz, no WAV
    container, so the browser can build AudioBuffers directly. The matching audio
    parameters are echoed in response headers so the frontend knows what to feed into
    Web Audio without an out-of-band handshake.

    Either provider may be the one that answers: Cartesia is the default, "TRTC Mode" selects
    Tencent, and a failure on the selected one falls back to the other (reported on /health,
    never silent). The frontend contract is identical either way.
    """
    # Gate on whether ANY mouth in the attempt order can speak, not just the selected one.
    # Asking only about the selected provider would 503 a request the fallback could have
    # served -- e.g. Cartesia selected but out of credit, with Tencent standing by.
    if not Settings.LIVE_TTS or not any(Settings.tts_provider_is_configured(p)
                                        for p in provider_order()):
        # The frontend disables its mouth permanently on 503, so this must mean "nothing can
        # ever speak", not "the first choice is unhappy".
        raise HTTPException(status_code=503, detail="tts not configured")

    # Synthesise BEFORE the response starts. Tencent's TextToVoice is synchronous, so there
    # is no streaming to preserve; doing the work first means an upstream failure can still
    # be reported as a real status code. (Previously the failure surfaced as a 200 with an
    # empty body, which the browser cannot tell apart from legitimate silence.)
    try:
        pcm = await synthesize_pcm(req.text, req.language)
    except TTSError as exc:
        logging.getLogger("auntiejoy.tts").warning("synthesis failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"tts failed: {exc}") from exc

    async def _relay():
        # Relay the finished clip in small chunks so the browser can start scheduling audio
        # immediately instead of waiting for one large body to land.
        for start in range(0, len(pcm), _TTS_RELAY_BYTES):
            yield pcm[start:start + _TTS_RELAY_BYTES]

    return StreamingResponse(
        _relay(),
        media_type="application/octet-stream",
        headers={
            # Tell the browser exactly what format to expect. We ask Tencent for Codec=pcm,
            # which is always mono 16-bit little-endian at the sample rate we requested.
            # The frontend reads this header rather than trusting a compiled-in constant.
            "X-Audio-Sample-Rate": str(SAMPLE_RATE),
            "X-Audio-Channels": "1",
            "X-Audio-Bit-Depth": "16",
            "X-Audio-Encoding": "pcm_s16le",
            # WHICH ENGINE ACTUALLY SPOKE THIS LINE. Sent on the response rather than polled
            # from /health, because /health would report the state a moment later and the UI
            # needs to attribute THIS audio. Values are ASCII by construction: the human-
            # readable fallback reason is deliberately NOT sent here, because HTTP headers are
            # latin-1 and a provider error containing a quote or an em dash would raise
            # UnicodeEncodeError and turn a working fallback into a 500. The UI fetches the
            # reason from /health (UTF-8 JSON) when the flag says there is one.
            # The engine that ACTUALLY produced this audio, which is what tts.js labels the
            # SPEAKING row with. During a fallback this is the engine that answered, not the
            # one selected -- the selection is reported separately below so a client can
            # still tell that the toggle is on Cartesia while Tencent is speaking.
            "X-TTS-Provider": Settings.tts_served_provider(),
            "X-TTS-Selected": Settings.tts_provider(),
            "X-TTS-Fallback": "1" if Settings.TTS_FALLBACK_ACTIVE else "0",
            # Disable proxy buffering so the first chunk reaches the browser ASAP.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# =====================================================================
