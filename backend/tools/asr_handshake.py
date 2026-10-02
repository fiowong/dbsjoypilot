"""Probe the Tencent ASR handshake directly, bypassing any HTTP proxy, and try a
range of engine_model_type values so we can see which the account accepts."""
import asyncio
import os
import sys
import time
import uuid

sys.path.insert(0, ".")

# websockets 15+ reads HTTPS_PROXY/HTTP_PROXY from the env on its own. Clear them so
# this probe goes out over the real connection, exactly like the browser would.
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

import websockets  # noqa: E402
from app.stt import _signed_url, HOST, VOICE_FORMAT_PCM  # noqa: E402
from app.config import Settings  # noqa: E402

CANDIDATES = [
    "16k_zh_en",        # what the code uses for English + Chinese
    "16k_multi_lang",   # what the code uses for Malay
    "16k_en",
    "16k_zh_large",
    "16k_zh",
    "16k_ms",
    "16k_id",
]


async def probe(engine: str, proxy=None) -> str:
    now = int(time.time())
    params = {
        "secretid": Settings.ASR_SECRET_ID,
        "timestamp": str(now),
        "expired": str(now + 3600),
        "nonce": str(now % 100000000),
        "engine_model_type": engine,
        "voice_id": uuid.uuid4().hex[:32],
        "voice_format": str(VOICE_FORMAT_PCM),
        "needvad": "1",
    }
    url = _signed_url(Settings.ASR_APPID, Settings.ASR_SECRET_ID,
                      Settings.ASR_SECRET_KEY, params)
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, max_size=None, ping_interval=None,
                               proxy=proxy, open_timeout=10), timeout=12)
    except Exception as exc:
        return f"CONNECT EXC: {type(exc).__name__}: {exc}"
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
    except Exception as exc:
        await ws.close()
        return f"RECV EXC: {type(exc).__name__}: {exc}"
    await ws.close()
    import json
    m = json.loads(raw)
    return f"code={m.get('code')} message={m.get('message')!r}"


async def main() -> None:
    print(f"host={HOST}  appid={Settings.ASR_APPID[:4]}...  "
          f"region={Settings.ASR_REGION}  secretid={Settings.ASR_SECRET_ID[:6]}...")
    print(f"proxy env cleared. egress check below is from this process.\n")
    print("--- no proxy, engine sweep ---")
    for e in CANDIDATES:
        print(f"  {e:16s} -> {await probe(e, proxy=None)}")
        await asyncio.sleep(0.6)


if __name__ == "__main__":
    asyncio.run(main())
