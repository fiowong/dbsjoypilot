"""Option A verification: the hotword_list wiring and the signed-URL encoding.

The live Tencent ASR cannot be reached from this sandbox (it returns 6001 - overseas caller on a
mainland-site account), so this probe verifies the two things that DO NOT need the network:

  1. build_hotword_list() - shape, de-dup, cap, unsafe-name skip.
  2. _signed_url()        - the value is PERCENT-ENCODED in the URL but the HMAC is computed over
                            the RAW value (the exact gotcha that made hotwords 4002 without the fix).

Run:  HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/x.db PYTHONPATH="$PWD" .venv/bin/python -B tools/hotword_probe.py
"""
import sys

sys.path.insert(0, ".")

from app.stt import _signed_url, build_hotword_list, HOST  # noqa: E402
import hmac, hashlib, base64  # noqa: E402
from urllib.parse import parse_qs, urlparse, unquote  # noqa: E402

APPID, SID, SKEY = "appid123", "sid-xyz", "secret-key"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return bool(cond)


ok = True

# --- build_hotword_list ---------------------------------------------------------------
hl = build_hotword_list(["Mei Tan", "John Doe", "John Smith", "Alice"])
ok &= check("four payees -> four 'Name|11' entries",
            hl == "Mei Tan|11,John Doe|11,John Smith|11,Alice|11")
ok &= check("empty list -> None", build_hotword_list([]) is None)
ok &= check("None -> None", build_hotword_list(None) is None)
ok &= check("blank entries skipped",
            build_hotword_list(["  ", "Mei Tan", ""]) == "Mei Tan|11")
ok &= check("dedup is case-insensitive",
            build_hotword_list(["Mei Tan", "mei tan", "MEI TAN"]) == "Mei Tan|11")
ok &= check("names with comma/pipe are dropped",
            build_hotword_list(["Ok Name", "Bad,Name", "Worse|Name"]) == "Ok Name|11")

many = [f"Payee {i}" for i in range(200)]
capped = build_hotword_list(many)
ok &= check("capped at 128 entries", len(capped.split(",")) == 128)

# --- _signed_url: encode in URL, sign over RAW ----------------------------------------
params = {
    "secretid": SID,
    "timestamp": "1700000000",
    "expired": "1700003600",
    "nonce": "12345",
    "engine_model_type": "16k_zh_en",
    "voice_id": "abc123",
    "voice_format": "1",
    "needvad": "1",
    "hotword_list": "Mei Tan|11,John Doe|11",
}
url = _signed_url(APPID, SID, SKEY, params)

ok &= check("URL percent-encodes the hotword space", "Mei%20Tan%7C11" in url)
ok &= check("URL percent-encodes the hotword comma", "John%20Doe%7C11" in url)

# The signature must equal HMAC-SHA1 over the RAW (un-encoded) original text.
qs = urlparse(url).query
got_sig = parse_qs(qs)["signature"][0]
raw_sig = unquote(got_sig)
original = f"{HOST}/asr/v2/{APPID}?" + "&".join(
    f"{k}={params[k]}" for k in sorted(params)
)
expected = base64.b64encode(
    hmac.new(SKEY.encode(), original.encode(), hashlib.sha1).digest()
).decode()
ok &= check("signature is HMAC-SHA1 over the RAW (un-encoded) values", raw_sig == expected)

# Backward compatibility: ordinary params (no special chars) are NOT percent-encoded.
url2 = _signed_url(APPID, SID, SKEY,
                   {k: v for k, v in params.items() if k != "hotword_list"})
ok &= check("plain params: engine value stays unencoded",
            "engine_model_type=16k_zh_en" in url2)
ok &= check("plain params: signature present", "signature=" in url2)

print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
