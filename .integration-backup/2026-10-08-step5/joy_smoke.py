"""Step 5 smoke: walk the six recorded demo commands over the HTTP surface, Voice Mode on.

Runs against WHICHEVER tree is on sys.path (cwd/backend), so the same script produces the
pre-merge and post-merge capture and the two can be diffed.

Voice Mode is not a UI nicety -- it changes the WIRE PAYLOAD. live.js:625-641 sends, for every
input while the switch is on:

    via_voice=True, asr_confidence=0.41, asr_confidence_source='engine_reported',
    voice_risk='SPEAKER_MISMATCH'

i.e. it declares the speaker unverified, below the 0.60 admission floor, which is what makes
Stage 1 hold the send behind FaceID. A smoke that sends plain text would test a different
program. So both configurations are captured:

    VOICE   the payload above            (switch on  -- the demo's "Voice Mode")
    TYPED   via_voice=False, source='typed', no voice_risk   (switch off)

Output: JSON with, per demo, per turn: the spoken INTERFACE_LINEs (key, language, text), the
session outcome/state, and the event_type set. That is what "same output as today" has to mean.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.scenarios import ALL_SCENARIOS

DEMOS = ["DEMO1", "DEMO2", "DEMO3", "DEMO4", "DEMO5", "DEMO6"]

VOICE_TURN = {"via_voice": True, "asr_confidence": 0.41,
              "asr_confidence_source": "engine_reported", "voice_risk": "SPEAKER_MISMATCH"}
TYPED_TURN = {"via_voice": False, "asr_confidence": 0.99,
              "asr_confidence_source": "typed"}


def summarise(payload: dict) -> dict:
    """The part of a TurnResponse a customer or a judge could actually observe."""
    lines = []
    for ev in payload.get("events", []):
        if ev.get("event_type") == "INTERFACE_LINE":
            p = ev.get("payload", {})
            lines.append({"key": p.get("message_key"), "lang": p.get("language"),
                          "text": p.get("text")})
    sess = payload.get("session", {}) or {}
    return {
        "spoken": lines,
        "outcome": sess.get("outcome"),
        "state": sess.get("state"),
        "events": sorted({e.get("event_type") for e in payload.get("events", [])}),
    }


def run(mode: str) -> dict:
    client = TestClient(app)
    extra = VOICE_TURN if mode == "VOICE" else TYPED_TURN
    out: dict = {}
    for demo in DEMOS:
        spec = ALL_SCENARIOS[demo]
        sid = f"smoke_{mode}_{demo}"
        rec = {"title": spec.get("title"), "turns": []}
        for turn in spec["turns"]:
            body = {"session_id": sid, "text": turn["text"], **extra}
            rec["turns"].append(summarise(client.post("/turn", json=body).json()))
        conf = spec.get("confirm")
        if conf:
            body = {"session_id": sid, "fields": conf["fields"],
                    "language": conf.get("language", "EN"),
                    "faceid_result": conf.get("faceid", "SUCCESS"), "biometric": True}
            rec["confirm"] = summarise(client.post("/confirm", json=body).json())
        out[demo] = rec
    return out


if __name__ == "__main__":
    dest = Path(sys.argv[1])
    result = {"VOICE": run("VOICE"), "TYPED": run("TYPED")}
    dest.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    n_lines = sum(len(t["spoken"]) for m in result.values() for d in m.values()
                  for t in d["turns"])
    print(f"captured {dest}  ({len(result)} modes, {len(DEMOS)} demos, {n_lines} spoken lines)")
