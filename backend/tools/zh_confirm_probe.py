"""Drive the ZH turn -> FaceID /confirm path over HTTP against a running backend.

The customer reported: "faceid doesn't work when my input is in chinese too. it said
'抱歉，我刚刚联系不上支付系统，请再试一次。'" - that is the frontend's ZH `unreachable` line.

This exercises the real API surface (not an in-process call) so the 409 / 422 / transport
branches are all reachable exactly as the browser would hit them.

Run:  .venv/bin/python -B tools/zh_confirm_probe.py [port]
"""
import json
import sys
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8085"
BASE = f"http://127.0.0.1:{PORT}"


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw[:300]}


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def lines(events):
    return [e.get("payload", {}).get("message_key") for e in events
            if e.get("event_type") == "INTERFACE_LINE"]


def main():
    ok = True
    sid = "zh_confirm_probe_1"
    # The customer's own ZH wording, taken from the audit trail (session zh1):
    # "从我的储蓄账户转五十新元给阿美。" - 阿美 is a seeded nickname for Mei Tan.
    zh = "从我的储蓄账户转五十新元给阿美。"

    print(f"--- ZH turn against {BASE} ---")
    st, r = post("/turn", {"session_id": sid, "text": zh, "via_voice": False})
    sess = r.get("session", {})
    print(f"    HTTP {st} state={sess.get('state')} outcome={sess.get('outcome')}")
    print(f"    draft={sess.get('draft', {}).get('draft_id') if sess.get('draft') else None} "
          f"amount={sess.get('draft', {}).get('amount_minor') if sess.get('draft') else None}")
    print(f"    interface_language={sess.get('interface_language')} lines={lines(r.get('events', []))}")
    ok &= check("ZH turn answered 200", st == 200, str(st))
    ok &= check("ZH turn produced a draft awaiting confirmation",
                sess.get("state") == "CONFIRMATION_READY" and bool(sess.get("draft")),
                f"state={sess.get('state')} draft={bool(sess.get('draft'))}")

    print("--- the FaceID /confirm call, exactly as the frontend sends it ---")
    st2, r2 = post("/confirm", {"session_id": sid,
                                "fields": ["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
                                "language": "ZH", "biometric": True, "faceid_result": "SUCCESS"})
    s2 = r2.get("session", {})
    print(f"    HTTP {st2} state={s2.get('state')} outcome={s2.get('outcome')}")
    print(f"    lines={lines(r2.get('events', []))}")
    ok &= check("ZH /confirm is ACCEPTED (not 409, not 422)", st2 == 200, f"HTTP {st2} {r2.get('detail','')}")
    ok &= check("ZH /confirm executes the payment", s2.get("state") == "EXECUTED", str(s2.get("state")))

    print("--- a /confirm for a session the server never saw must be a 409 with a reason ---")
    st3, r3 = post("/confirm", {"session_id": "no_such_session_zzz",
                                "fields": ["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
                                "language": "ZH", "biometric": True, "faceid_result": "SUCCESS"})
    print(f"    HTTP {st3} detail={str(r3.get('detail'))[:120]}")
    ok &= check("unknown session -> 409 (the frontend's nothingToConfirm branch)", st3 == 409, str(st3))

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


main()
