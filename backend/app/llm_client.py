import json
import sys
from typing import Optional
import httpx
from .config import Settings
from .agent_runtime import AgentUnavailable

# =====================================================================
# 5. LLM client — OpenAI-compatible, temperature 0, ≤1 retry, fail closed
# =====================================================================
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
    for attempt in range(Settings.AGENT_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=Settings.AGENT_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{Settings.LLM_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {Settings.LLM_API_KEY}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:                      # noqa: BLE001 — fail closed on anything
            last_error = exc
            if attempt < Settings.AGENT_MAX_RETRIES:
                await _async_sleep(0.4)
    print(f"[AGENT_LIVE_FALLBACK] llm_complete: transport/HTTP failed after retries: {last_error}",
          file=sys.stderr, flush=True)
    raise AgentUnavailable(f"LLM call failed after retries: {last_error}")


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


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
    except Exception:                      # noqa: BLE001 — health must never raise
        return False


