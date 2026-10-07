"""The same FaceID /confirm flow in all three languages, to find the one that differs.

The customer reported FaceID working in Chinese but failing in Malay with the BM "unreachable"
line ("Maaf, saya tidak dapat menghubungi sistem pembayaran sebentar tadi. Sila cuba lagi.").
That line is the frontend's ELSE branch, so the BM call is failing with something that is NOT a
409 - a 422/500 or a transport failure. This drives the real HTTP surface per language and prints
the exact status and body, so the failing case names itself instead of being inferred.

Run:  .venv/bin/python -B tools/confirm_lang_probe.py [port]
"""
import json
import sys
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8085"
BASE = f"http://127.0.0.1:{PORT}"

CASES = [
    ("EN", "Send fifty dollars to ah girl."),
    ("ZH", "从我的储蓄账户转五十新元给阿美。"),
    ("BM", "Hantar lima puluh dolar kepada ah girl."),
]


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
            return e.code, {"detail": raw[:400]}
    except Exception as e:                                    # transport
        return 0, {"detail": f"{type(e).__name__}: {e}"}


def lines(events):
    return [e.get("payload", {}).get("message_key") for e in events
            if e.get("event_type") == "INTERFACE_LINE"]


def main():
    bad = []
    for lang, text in CASES:
        print(f"=== {lang} ===")
        sid = f"clp_{lang}"
        st, r = post("/turn", {"session_id": sid, "text": text, "via_voice": False})
        sess = r.get("session", {})
        print(f"  /turn   HTTP {st}  state={sess.get('state')}  "
              f"iface_lang={sess.get('interface_language')}  "
              f"draft={sess.get('draft', {}).get('draft_id') if sess.get('draft') else None}")
        if st != 200 or sess.get("state") != "CONFIRMATION_READY":
            print(f"          lines={lines(r.get('events', []))} detail={str(r.get('detail'))[:200]}")
            bad.append((lang, "turn", st, str(r.get("detail"))[:160]))
            continue

        st2, r2 = post("/confirm", {"session_id": sid,
                                    "fields": ["RECIPIENT", "AMOUNT", "SOURCE_ACCOUNT"],
                                    "language": lang, "biometric": True,
                                    "faceid_result": "SUCCESS"})
        s2 = r2.get("session", {})
        print(f"  /confirm HTTP {st2}  state={s2.get('state')}  lines={lines(r2.get('events', []))}")
        if st2 != 200:
            print(f"          detail={str(r2.get('detail'))[:400]}")
            bad.append((lang, "confirm", st2, str(r2.get("detail"))[:160]))

    print()
    if bad:
        print("FAILING CASES:")
        for lang, where, st, detail in bad:
            print(f"  {lang} {where}: HTTP {st} - {detail}")
    else:
        print("every language completed /turn -> /confirm -> EXECUTED")
    sys.exit(1 if bad else 0)


main()
