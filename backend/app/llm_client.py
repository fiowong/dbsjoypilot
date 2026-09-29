import json
import sys
import asyncio
from typing import Optional
import httpx
from .config import Settings
from .agent_runtime import AgentUnavailable

# =====================================================================
# 5. LLM client, OpenAI-compatible, temperature 0, fail closed.
#    Resilient: exponential backoff + honours 429 Retry-After, and logs the
#    REAL cause (exception type / HTTP status / body) instead of an empty string
#    so live-mode transport failures are diagnosable (see aj-2026-09-23 transport fix).
# =====================================================================
# Light pacing so we never thunder the upstream with a burst. The selftest fires many calls
# back-to-back; the live demo is interactive, so this is invisible. It reduces the 502
# "upstream model service is abnormal or unreachable" rate when the provider is load-sensitive.
_CALL_PACE_S = 0.3

async def llm_complete(system_prompt: str, user_payload: dict) -> str:
    if not Settings.LIVE_LLM:
        raise AgentUnavailable("live LLM not configured")
    body = {
        "model": Settings.LLM_MODEL,
        "temperature": Settings.AGENT_TEMPERATURE,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    last_error: Optional[Exception] = None
    backoff = 0.5
    for attempt in range(Settings.AGENT_MAX_RETRIES + 1):
        await asyncio.sleep(_CALL_PACE_S)
        try:
            async with httpx.AsyncClient(timeout=Settings.AGENT_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{Settings.LLM_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {Settings.LLM_API_KEY}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code == 429:
                    # Rate limited, wait the server's Retry-After (or our backoff) and retry.
                    last_error = httpx.HTTPStatusError(
                        "rate limited (429)", request=resp.request, response=resp)
                    if attempt < Settings.AGENT_MAX_RETRIES:
                        retry_after = resp.headers.get("Retry-After")
                        wait = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
                        await asyncio.sleep(wait)
                        backoff = min(backoff * 2, 10.0)
                        continue
                    break
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:                      # noqa: BLE001, fail closed on anything
            last_error = exc
            if attempt < Settings.AGENT_MAX_RETRIES:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
    print(f"[AGENT_LIVE_FALLBACK] llm_complete: transport/HTTP failed after retries: {_describe(last_error)}",
          file=sys.stderr, flush=True)
    raise AgentUnavailable(f"LLM call failed after retries: {_describe(last_error)}")


def _describe(exc: Optional[Exception]) -> str:
    """Render a transport/HTTP failure with enough detail to diagnose (timeout vs 429 vs 5xx)."""
    if exc is None:
        return "(no error captured)"
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.text[:300]
        except Exception:
            body = ""
        return f"{type(exc).__name__} status={exc.response.status_code} body={body!r}"
    return f"{type(exc).__name__}: {exc}"


async def check_llm_health() -> bool:
    """Best-effort liveness probe for /health.

    Returns False on any failure (no key, network error, non-200) and never raises, so the
    health endpoint stays fast and honest: it reports "live" only when the upstream model is
    actually reachable, not merely because a key string is present.
    """
    if not Settings.LIVE_LLM:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{Settings.LLM_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {Settings.LLM_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": Settings.LLM_MODEL, "temperature": 0,
                      "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 1})
            return resp.status_code == 200
    except Exception:                      # noqa: BLE001, health must never raise
        return False


