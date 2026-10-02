"""Stream a known WAV through the real Tencent ASR path and print what comes back.

Isolates the MODEL from the browser capture: if this transcribes the file perfectly,
the model is fine and the problem is the mic/capture code (or the person's mic).
"""
import asyncio
import sys
import time
import wave

sys.path.insert(0, ".")

from app.stt import TencentRealtimeASR, ASRError   # noqa: E402


def read_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000, w.getframerate()
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        return w.readframes(w.getnframes())


async def run(path: str, engine: str, realtime: bool = True) -> None:
    pcm = read_pcm(path)
    chunk = 3200 * 2          # 200 ms of 16-bit mono @16k
    frames = [pcm[i:i + chunk] for i in range(0, len(pcm), chunk)]

    asr = TencentRealtimeASR(language="en")
    asr.engine = engine       # force the engine under test
    interims: list[str] = []
    asr.on_interim = lambda t: interims.append(t)
    finals: list[str] = []
    asr.on_final = lambda r: finals.append(r.text)

    t0 = time.time()
    try:
        await asr.connect()
    except ASRError as exc:
        print(f"  {engine:16s} CONNECT FAILED: {exc}")
        return

    for f in frames:
        await asr.send_audio(f)
        if realtime:
            await asyncio.sleep(0.2)
    result = await asr.finish()
    await asr.close()

    print(f"  engine={engine}")
    print(f"    interim  : {interims[-1] if interims else '(none)'}")
    print(f"    final    : {result.text if result else '(none)'}")
    print(f"    confidence: {result.confidence if result else None}"
          f"  source={result.confidence_source if result else None}")
    print(f"    latency  : {time.time() - t0:.2f}s")


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/asrtest_16k.wav"
    engines = sys.argv[2:] or ["16k_zh_en", "16k_en"]
    print(f"file: {path}")
    for e in engines:
        await run(path, e)
        print()
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
