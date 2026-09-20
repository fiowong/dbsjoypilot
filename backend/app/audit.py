import hashlib
import json
import sqlite3
from typing import List, Optional
from .config import Settings, now_iso

# 8. Append-only, hash-chained audit (§8.2 stage 2, §7.3 "implement and display throughout")
# =====================================================================
class AuditStore:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    ts         TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage      INTEGER,
                    payload    TEXT NOT NULL,
                    prev_hash  TEXT NOT NULL,
                    hash       TEXT NOT NULL
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_session ON audit_events(session_id, seq)")

    def append(self, session_id: str, event_type: str, payload: dict,
               stage: Optional[int] = None) -> dict:
        with sqlite3.connect(self.path) as con:
            row = con.execute(
                "SELECT hash FROM audit_events WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
                (session_id,)).fetchone()
            prev = row[0] if row else "GENESIS"
            ts = now_iso()
            body = json.dumps({"session_id": session_id, "ts": ts, "event_type": event_type,
                               "stage": stage, "payload": payload},
                              sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256((prev + body).encode()).hexdigest()[:32]
            con.execute(
                "INSERT INTO audit_events (session_id, ts, event_type, stage, payload, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?)",
                (session_id, ts, event_type, stage, json.dumps(payload, ensure_ascii=False),
                 prev, digest))
            con.commit()
        return {"event_type": event_type, "stage": stage, "ts": ts,
                "payload": payload, "hash": digest, "prev_hash": prev}

    def events(self, session_id: str) -> List[dict]:
        with sqlite3.connect(self.path) as con:
            rows = con.execute(
                "SELECT seq, ts, event_type, stage, payload, prev_hash, hash FROM audit_events"
                " WHERE session_id = ? ORDER BY seq", (session_id,)).fetchall()
        return [{"seq": r[0], "ts": r[1], "event_type": r[2], "stage": r[3],
                 "payload": json.loads(r[4]), "prev_hash": r[5], "hash": r[6]} for r in rows]

    def verify_chain(self, session_id: str) -> dict:
        events = self.events(session_id)
        prev = "GENESIS"
        for event in events:
            body = json.dumps({"session_id": session_id, "ts": event["ts"],
                               "event_type": event["event_type"], "stage": event["stage"],
                               "payload": event["payload"]},
                              sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256((prev + body).encode()).hexdigest()[:32]
            if expected != event["hash"]:
                return {"intact": False, "broken_at_seq": event["seq"], "events": len(events)}
            prev = event["hash"]
        return {"intact": True, "events": len(events), "head": prev}


AUDIT = AuditStore(Settings.DB_PATH)


# =====================================================================
