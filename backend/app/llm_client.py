import json
import sys
import asyncio
import time
from contextvars import ContextVar
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

# Per-turn budget for live model calls, held in a ContextVar so it is scoped to the request
# being served and cannot leak between two overlapping turns. The orchestrator arms it at the
# top of run_turn; when it runs out, llm_complete raises AgentUnavailable, which every agent
# already treats as "fall back to the deterministic mock". That is what stops a slow provider
# from stacking five slow calls into a frozen demo: the first few agents may go live, the rest
# go deterministic, and the turn still completes with a correct answer.
_TURN_DEADLINE: ContextVar[Optional[float]] = ContextVar("aj_turn_deadline", default=None)


class _FatalUpstreamError(AgentUnavailable):
    """An upstream failure that retrying cannot fix (rejected credentials).

    Subclasses AgentUnavailable so callers need no new handling: every agent already treats
    that as "fall back to the deterministic mock". Only the retry loop distinguishes it.
    """


def arm_turn_deadline(seconds: float) -> None:
    """Start this turn's live-model budget. Called once at the top of run_turn."""
    _TURN_DEADLINE.set(time.monotonic() + seconds if seconds and seconds > 0 else None)


def disarm_turn_deadline() -> None:
    """Clear the budget. Called in a finally so a turn never leaves one armed."""
    _TURN_DEADLINE.set(None)


def turn_budget_spent() -> bool:
    deadline = _TURN_DEADLINE.get()
    return deadline is not None and time.monotonic() >= deadline


def budget_remaining() -> Optional[float]:
    """Seconds of live-model budget left in this turn, or None when no budget is armed."""
    deadline = _TURN_DEADLINE.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


async def llm_complete(system_prompt: str, user_payload: dict,
                       attempt_timeout_s: Optional[float] = None) -> str:
    """One live model call for one agent.

    ``attempt_timeout_s`` is that agent's own ceiling for a single attempt, taken from
    Settings.AGENT_TIMEOUTS. The agents do very different amounts of work (the Validation agent's
    five field checks take ~8s against the Guardrails agent's ~2s), so one shared ceiling would
    either kill the slow agent mid-answer or let the fast one hang far too long. Falls back to
    Settings.AGENT_TIMEOUT_S when the caller does not name one.
    """
    if not Settings.LIVE_LLM:
        raise AgentUnavailable("live LLM not configured")
    if turn_budget_spent():
        # Not a transport failure: this turn already spent its live-model budget on earlier
        # agents, so the remaining ones go deterministic. Fails closed, never silently live.
        raise AgentUnavailable(
            "turn live-model budget spent, using the deterministic mock for this agent")
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
        # Checked before EVERY attempt, not just the first: otherwise one agent's four
        # 8s attempts could overrun the whole turn's budget on its own and still leave the
        # customer waiting long after the remaining agents should have gone deterministic.
        if turn_budget_spent():
            raise AgentUnavailable(
                "turn live-model budget spent, using the deterministic mock for this agent")
        await asyncio.sleep(_CALL_PACE_S)
        # A single attempt may not outlive the budget either, so the ceiling is whichever
        # comes first: this agent's per-attempt timeout, or what this turn has left to spend.
        remaining = budget_remaining()
        attempt_timeout = attempt_timeout_s or Settings.AGENT_TIMEOUT_S
        if remaining is not None:
            attempt_timeout = max(1.0, min(attempt_timeout, remaining))
        try:
            async with httpx.AsyncClient(timeout=attempt_timeout) as client:
                resp = await client.post(
                    f"{Settings.LLM_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {Settings.LLM_API_KEY}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code in (401, 403):
                    # Rejected credentials are permanent. Retrying four times only bought four
                    # pointless round-trips, which is how a bad key cost ~28s of a turn.
                    raise _FatalUpstreamError(
                        f"upstream rejected the credentials (HTTP {resp.status_code}), "
                        "check the API key; retrying cannot fix this")
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
        except _FatalUpstreamError:
            raise                                     # a rejected key will never succeed
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


