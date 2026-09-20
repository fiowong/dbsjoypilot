import json
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
    raise AgentUnavailable(f"LLM call failed after retries: {last_error}")


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


