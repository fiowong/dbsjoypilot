#!/usr/bin/env bash
# =============================================================================
# AuntieJoy -- pre-deployment readiness gate.
#
#   ./deploy/verify.sh
#
# Runs every check that must be green BEFORE anything is deployed. Safe to run
# anywhere: it touches no cloud resource and no network, and it writes only to
# /tmp. Exit code 0 means "safe to deploy".
#
# WHY THIS FILE DIFFERS FROM THE HANDOVER VERSION
#   The handover repo pushed stage 15 through a switchable payment rail
#   (app/rails.py, AUNTIEJOY_PAYMENT_RAIL=mock|dbs), so it tested that rail three
#   ways: configured, fail-closed with no credentials, and idempotent on replay.
#   THIS repo has no rails.py -- stage 15 executes through the deterministic
#   in-memory ledger directly -- so those three sections are DELETED here rather
#   than left failing. The assertions that still apply are kept in section 3.
#   The handover's section 15 (the append-only JSONL export, which needs
#   tools/export_turns.py and tools/cos_sync.py) is absent for the same reason:
#   those tools are not shipped in this repo.
#
# THE EXPECTED NUMBERS LIVE IN ONE PLACE
#   The suite's shape changes whenever a scenario or case is added, so the exact
#   strings are declared once below and every assertion reads from there. Update
#   the variable, not the individual greps.
# =============================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
BACKEND="$ROOT/backend"
VENV_PY="$BACKEND/.venv/bin/python"

# 89 scenarios = 28 acceptance (the customer-facing BRD/demo set) + 61 regression pins.
# B4 is deliberately NOT in the acceptance set: it has no node on the MECE failure map, so
# it lives in selftest.py as the dedicated over-trigger guard rather than in the UI rail.
# 55 -> 59 on 2026-10-09: the four IN-CORRECT-* rows pinning the correction relaxer (see
# scenarios.py). 59 -> 61 on 2026-10-09: IN-SRC-CPF (a CPF account is refused, not frozen as a
# scam) and IN-COERCE-ATTRIB (an agency that WANTS a transfer still freezes - the cue the SCAM
# relaxer reads). This line and EXPECT_ALL are the only place the shape is declared; update them
# together, never the individual greps.
EXPECT_ALL='89/89 scenarios passed (28/28 acceptance, 61/61 regression pins)'
# The lines worth echoing on every run, whatever their values. The schedule count moved 8 -> 9
# with the "next friday" row that pins the DATE, not only the weekday.
SUMMARY_LINES='^(89/89|6/6|9/9|11/11|1/1|62/62)'

# Managed node first, then whatever is on PATH.
#
# The managed version directory is GLOBBED rather than pinned to one build. It is reinstalled
# under a new suffix from time to time (this machine moved from 22.22.2-3 to 22.22.2-6), and a
# pinned path then matches nothing while looking perfectly correct -- so the gate silently falls
# back to a PATH node that may be far too old for jsdom, and the frontend suites fail for a
# reason that has nothing to do with the frontend.
NODE_BIN="${NODE_BIN:-}"
if [ -z "$NODE_BIN" ]; then
  for candidate in \
    "$HOME"/.workbuddy-ai/binaries/node/versions/*/bin/node \
    "$(command -v node || true)"
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then NODE_BIN="$candidate"; break; fi
  done
fi

PASS=0
FAIL=0
note() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }

if [ ! -x "$VENV_PY" ]; then
  echo "No venv at $VENV_PY"
  echo "Create it first:  cd backend && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
  exit 1
fi

# -----------------------------------------------------------------------------
# EVERY SUITE RUN BELOW BLANKS THE LLM KEYS ON PURPOSE. Do not "tidy" them away.
#
# Two independent reasons:
#
# 1. The expected numbers (EXPECT_ALL / EXPECT_FAILCLOSED) were established against the
#    DETERMINISTIC MOCK agents, and they are only valid there. The live LLM is a different
#    implementation of stages 3/4/5/8: its guardrail and validation decisions are its own,
#    so a scenario the mock resolves one way can legitimately resolve differently. Asserting
#    an exact count against a non-deterministic engine is not a test, it is a coin flip.
#
# 2. Without this, the gate silently goes LIVE whenever backend/.env holds a key. That makes
#    a pre-deploy gate slow, non-deterministic (it then depends on a network service and a
#    model's mood) and billable. A readiness gate must be none of those.
#
# WHY BLANKED VALUES AND NOT `env -u`:
#   config.py calls load_dotenv(), and python-dotenv SETS any key that is absent from the
#   environment -- so `env -u HUNYUAN_API_KEY` just lets .env put it straight back. An empty
#   string is present in the environment, so dotenv leaves it alone. Verified both ways:
#   `env -u` gives LIVE_LLM=True, `HUNYUAN_API_KEY=` gives LIVE_LLM=False.
#
# This gate tests the DETERMINISTIC half. The live agents are exercised separately, by
# running the app and watching /health report engine=live.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
note "1. Shell syntax of the deploy scripts"
# -----------------------------------------------------------------------------
for script in deploy/provision.sh deploy/deploy.sh deploy/verify.sh deploy/preflight.sh; do
  if [ -f "$script" ]; then
    if bash -n "$script" 2>/dev/null; then ok "$script"; else bad "$script (syntax error)"; fi
  fi
done
for script in deploy/check_routes.py deploy/check_route_shadowing.py deploy/create_lighthouse.py \
              backend/tools/reseed_repository.py; do
  if [ -f "$script" ]; then
    # compile() IN MEMORY -- deliberately NOT `python -m py_compile`.
    #
    # py_compile writes __pycache__ into the repo as a SIDE EFFECT of a check that is supposed
    # to be read-only. Two things go wrong with that: it litters the tree every time the gate
    # runs, and on a read-only or sandboxed checkout the WRITE is what fails -- so a syntax
    # check reports a permission error that has nothing to do with the code. Measured: it
    # aborted this script outright.
    # compile() catches the same SyntaxErrors and touches no file.
    if SYNTAX_ERR="$("$VENV_PY" -c 'import sys, pathlib
compile(pathlib.Path(sys.argv[1]).read_bytes(), sys.argv[1], "exec")' "$script" 2>&1)"; then
      ok "$script"
    else
      printf '%s\n' "$SYNTAX_ERR" | tail -3 | sed 's/^/  /'
      bad "$script (syntax error)"
    fi
  fi
done

# -----------------------------------------------------------------------------
note "2. Python dependencies import"
# -----------------------------------------------------------------------------
if "$VENV_PY" -c "import fastapi, pydantic, uvicorn, httpx" 2>/dev/null; then
  ok "fastapi / pydantic / uvicorn / httpx"
else
  bad "backend dependencies missing -- run: backend/.venv/bin/pip install -r backend/requirements.txt"
fi

# -----------------------------------------------------------------------------
note "3. Backend suite (deterministic mock agents)"
# -----------------------------------------------------------------------------
# AUNTIEJOY_DB IS REDIRECTED TO A THROWAWAY FILE. Do not remove that override.
# app/audit.py builds its store from Settings.DB_PATH at IMPORT time, so without it this
# suite appends its ~1100 synthetic scenario events to the REAL hash-chained audit
# database -- the one artefact this project exists to demonstrate.
# (The handover version redirected AUNTIEJOY_RAIL_DB here instead, because its suite ran
# against a separate rail database. This repo has a single store, AUNTIEJOY_DB.)
MOCK_OUT="$(cd "$BACKEND" && AUNTIEJOY_DB=/tmp/aj_verify_mock.db \
  HUNYUAN_API_KEY= SILICONFLOW_API_KEY= \
  "$VENV_PY" -m app.selftest --selftest 2>&1)"
echo "$MOCK_OUT" | grep -E "$SUMMARY_LINES" | sed 's/^/  /'
if echo "$MOCK_OUT" | grep -qF "$EXPECT_ALL"; then ok "suite: all green"; else bad "suite: expected -- $EXPECT_ALL"; fi
if echo "$MOCK_OUT" | grep -q '3 expected, 3 observed'; then ok "suite: ledger 3/3"; else bad "suite: ledger not 3/3"; fi
# Assert the engine DIRECTLY, so this gate cannot quietly become a live one. Without the key
# blanking at the top of this file the suite reports engine=live: slower, billable, and no
# longer deterministic -- while still printing a passing scenario count.
if echo "$MOCK_OUT" | grep -q 'engine=mock'; then
  ok "the suite ran against the deterministic mock agents, not the live LLM"
else
  bad "the suite ran LIVE -- the LLM key blanking at the top of verify.sh is not in effect"
fi

# -----------------------------------------------------------------------------
note "4. Frontend API-base resolver (all six serving modes)"
# -----------------------------------------------------------------------------
if [ -n "$NODE_BIN" ]; then
  if RESOLVER_OUT="$("$NODE_BIN" frontend/test-api-base.mjs 2>&1)"; then
    echo "$RESOLVER_OUT" | sed 's/^/  /'
    ok "resolver: 6/6"
  else
    echo "$RESOLVER_OUT" | sed 's/^/  /'
    bad "resolver: at least one serving mode resolves to the wrong backend"
  fi
else
  bad "no usable node found (set NODE_BIN=/path/to/node)"
fi

# -----------------------------------------------------------------------------
note "5. Every backend route is reachable through the web server"
# -----------------------------------------------------------------------------
# Guards a real bug that was found here: /session/{id} existed in main.py but was absent
# from the Nginx proxy list, so it fell through to `location /` and returned index.html
# with a 200 instead of JSON.
if ROUTE_OUT="$("$VENV_PY" deploy/check_routes.py 2>&1)"; then
  echo "$ROUTE_OUT" | sed 's/^/  /'
  ok "route coverage: complete"
else
  echo "$ROUTE_OUT" | sed 's/^/  /'
  bad "a backend route is not proxied by Nginx (it would return HTML, not JSON)"
fi

# -----------------------------------------------------------------------------
note "6. Frontend has no build step (the deploy relies on this)"
# -----------------------------------------------------------------------------
if [ -f frontend/index.html ] && [ ! -f frontend/package.json ]; then
  ok "frontend is plain HTML/CSS/JS -- nothing to build"
else
  printf '  \033[33mNOTE\033[0m frontend/package.json exists; check whether the deploy must run a build\n'
fi

# -----------------------------------------------------------------------------
note "7. No proxy matcher shadows a static file (the /tts.js bug)"
# -----------------------------------------------------------------------------
# A PREFIX matcher such as Caddy's `handle /tts*` also captures `/tts.js`, which is a real
# frontend file. The browser asked for JavaScript, got the API's 404 JSON, refused to run
# it, so `stopSpeaking` was never defined -- and liveFreeTurn() calls stopSpeaking() as its
# FIRST statement, so every send threw ReferenceError. The chat looked completely dead
# while /turn stayed perfectly healthy on the wire. Asserted here so it cannot come back.
if SHADOW_OUT="$("$VENV_PY" deploy/check_route_shadowing.py \
      --config deploy/provision.sh --frontend frontend 2>&1)"; then
  ok "provision.sh: no proxy route shadows a frontend file"
else
  echo "$SHADOW_OUT" | sed 's/^/  /'
  bad "a proxy route would swallow a frontend file (see above)"
fi

# On the server the LIVE config is the one that matters, so check it too when present.
if [ -r /etc/caddy/Caddyfile ]; then
  if SHADOW_LIVE="$(sudo python3 deploy/check_route_shadowing.py \
        --config /etc/caddy/Caddyfile --frontend "${APP_DIR:-/opt/auntiejoy}/frontend" 2>&1)"; then
    ok "/etc/caddy/Caddyfile: no proxy route shadows a frontend file"
  else
    echo "$SHADOW_LIVE" | sed 's/^/  /'
    bad "/etc/caddy/Caddyfile would swallow a frontend file (see above)"
  fi
fi

# -----------------------------------------------------------------------------
note "8. Repository seed drift can be detected (the INSERT OR IGNORE trap)"
# -----------------------------------------------------------------------------
# Repository.seed() uses INSERT OR IGNORE, so it never updates an existing row: a deploy
# that ships a new seed leaves the instance reading the old rows, and the only symptom is
# one feature quietly not working. tools/reseed_repository.py is what makes that visible,
# so it is tested here BOTH ways -- it must pass a database that is in step, and it must
# FAIL a database that has drifted. A checker that cannot fail is not a checker.
REPO_TOOL="$BACKEND/tools/reseed_repository.py"
if [ ! -f "$REPO_TOOL" ]; then
  bad "backend/tools/reseed_repository.py is missing"
else
  rm -f /tmp/aj_verify_repo_fresh.db /tmp/aj_verify_repo_drift.db

  # (a) a freshly seeded database must be reported as in step
  if (cd "$BACKEND" && PYTHONPATH="$BACKEND" "$VENV_PY" -B -c \
        "from app.repository import Repository; Repository('/tmp/aj_verify_repo_fresh.db')" \
        >/dev/null 2>&1) \
     && (cd "$BACKEND" && PYTHONPATH="$BACKEND" "$VENV_PY" -B \
        tools/reseed_repository.py --check --db /tmp/aj_verify_repo_fresh.db \
        >/tmp/aj_verify_repo_a.log 2>&1); then
    ok "fresh repository reported IN STEP"
  else
    sed 's/^/  /' /tmp/aj_verify_repo_a.log
    bad "a freshly seeded repository was not reported as in step"
  fi

  # (b) a deliberately staled database must be reported as drifted, and exit non-zero
  cp /tmp/aj_verify_repo_fresh.db /tmp/aj_verify_repo_drift.db
  "$VENV_PY" -c "
import sqlite3
c = sqlite3.connect('/tmp/aj_verify_repo_drift.db')
c.execute(\"UPDATE payees SET nicknames = '[]' WHERE beneficiary_id = 'payee_mei_001'\")
c.commit()
" >/dev/null 2>&1
  if (cd "$BACKEND" && PYTHONPATH="$BACKEND" "$VENV_PY" -B \
        tools/reseed_repository.py --check --db /tmp/aj_verify_repo_drift.db \
        >/tmp/aj_verify_repo_b.log 2>&1); then
    bad "a drifted repository was reported as in step -- the checker is blind"
  else
    ok "drifted repository detected (exit non-zero, as required)"
  fi

  # (c) and --apply must repair it, then verify the repair
  if (cd "$BACKEND" && PYTHONPATH="$BACKEND" "$VENV_PY" -B \
        tools/reseed_repository.py --apply --db /tmp/aj_verify_repo_drift.db \
        >/tmp/aj_verify_repo_c.log 2>&1); then
    ok "reseed repaired the drift and verified the result"
  else
    sed 's/^/  /' /tmp/aj_verify_repo_c.log
    bad "reseed did not repair the drift"
  fi

  # (d) the audit database must be refused, whatever the caller passes
  if (cd "$BACKEND" && PYTHONPATH="$BACKEND" AUNTIEJOY_DB=/tmp/aj_verify_audit_guard.db \
        "$VENV_PY" -B tools/reseed_repository.py --apply \
        --db /tmp/aj_verify_audit_guard.db >/dev/null 2>&1); then
    bad "the reseed tool agreed to operate on the AUDIT database"
  else
    ok "reseed tool refuses the audit database"
  fi
fi

# -----------------------------------------------------------------------------
note "9. Frontend behaviour suites (jsdom)"
# -----------------------------------------------------------------------------
# The repo's own frontend regression guards. They need jsdom, which is a DEV dependency and
# is not installed on a server, so an unresolvable jsdom is reported as a SKIP rather than a
# failure -- but it is never silent, because a green gate that quietly skipped the frontend
# would be worse than no gate at all.
if [ -n "$NODE_BIN" ]; then
  # jsdom may live in the managed node workspace rather than next to the repo.
  if ! "$NODE_BIN" -e "require('jsdom')" >/dev/null 2>&1; then
    JS_WS="$HOME/.workbuddy-ai/binaries/node/workspace/node_modules"
    if [ -d "$JS_WS/jsdom" ]; then
      NODE_PATH="$JS_WS${NODE_PATH:+:$NODE_PATH}"
      export NODE_PATH
    fi
  fi
fi

if [ -n "$NODE_BIN" ] && "$NODE_BIN" -e "require('jsdom')" >/dev/null 2>&1; then
  for suite in frontend/confirm.test.js frontend/popup.test.js \
               frontend/shell.test.js frontend/langpill.test.js; do
    [ -f "$suite" ] || continue
    if SUITE_OUT="$("$NODE_BIN" "$suite" 2>&1)"; then
      ok "$(basename "$suite")"
    else
      echo "$SUITE_OUT" | grep -E 'FAIL|CHECK' | head -10 | sed 's/^/  /'
      bad "$(basename "$suite")"
    fi
  done
else
  printf '  \033[33mSKIP\033[0m jsdom not available -- the frontend suites were not run\n'
  printf '       install with:  npm install jsdom    (or set NODE_PATH to a tree that has it)\n'
fi

# -----------------------------------------------------------------------------
note "10. Provisioning script cannot execute text out of its own comments"
# -----------------------------------------------------------------------------
# The Caddyfile and the nginx vhost are written with UNQUOTED heredocs, because they have to
# expand $CADDY_SITE / $BASIC_USER / $BASIC_HASH. That has a sharp edge: inside an unquoted
# heredoc bash performs command substitution on the WHOLE body, so a backtick in an
# explanatory comment is not a comment at all -- it is a command, and bash runs it.
# This actually happened: four "command not found" lines appeared during a real deploy, and
# the text inside each pair of backticks was silently REPLACED by the (empty) output before
# the Caddyfile was written. A backticked string that happened to be a real command would
# have EXECUTED during provisioning, as root, from a comment.
# `bash -n` cannot catch this -- the script is syntactically valid. So assert it directly:
# provision.sh must contain no backticks at all. Use 'single quotes' in those comments.
BACKTICK_LINES="$(grep -n '`' deploy/provision.sh 2>/dev/null || true)"
if [ -n "$BACKTICK_LINES" ]; then
  echo "$BACKTICK_LINES" | sed 's/^/  /'
  bad "deploy/provision.sh contains a backtick -- inside an unquoted heredoc it is EXECUTED"
else
  ok "deploy/provision.sh has no backticks (nothing can execute from a comment)"
fi

# The instance's post-deploy selftest must NOT write into the live audit database.
# app/audit.py constructs its store from Settings.DB_PATH at IMPORT time, so a suite run with
# the deployed .env appends ~1100 synthetic scenario events to the real audit chain. This is
# not theoretical: it was measured on the live instance, where 7976 of 8108 events (98%) were
# selftest noise from earlier deploys, against only 9 genuine chat sessions. The append-only
# chain is the artefact the project exists to demonstrate, so assert the redirect survives.
if grep -q 'AUNTIEJOY_DB=/tmp/aj_instance_selftest.db' deploy/provision.sh; then
  ok "provision.sh runs the instance selftest against a scratch DB, not the live audit chain"
else
  bad "provision.sh would write the instance selftest into the LIVE audit database"
fi

# -----------------------------------------------------------------------------
note "11. TTS voice selection (the accent brief and the Malay trap)"
# -----------------------------------------------------------------------------
# The brief is a soft, female, southern-Chinese voice applied to ZH, EN and BM. Two provider
# facts make that easy to get wrong, and both were established by probing the live API:
#
#   * An ENGLISH voice cannot read Chinese (InvalidParameter.InvalidText). Chinese is
#     therefore spoken by a Chinese voice, and English is too -- that is where its accent
#     comes from. Picking an English voice for EN silently removes the accent.
#   * A DIALECT voice can read Mandarin but some of them CANNOT read Malay. 101019 (Cantonese)
#     and 101012 both collapse to ~15% on a Malay round-trip where every other voice scores
#     82-85%. Pointing BM at either of those makes Auntie unintelligible in Malay, and the
#     only symptom is a bad accent -- no error, no log line.
#
# This section is offline: it asserts against the set actually accepted by the account when
# probed, so it needs no credentials and cannot flake. Re-probe with
# backend/tools/probe_tts_voices.py when the catalogue changes.
if (cd "$BACKEND" && PYTHONPATH="$BACKEND" "$VENV_PY" -B - >/tmp/aj_verify_tts.log 2>&1 <<'PYEOF'
import sys
from app.config import Settings

# Every id below was accepted by TextToVoice on this account (probed 2026-10-07), so a
# rejection here is a real misconfiguration and not a catalogue change we have not seen.
ACCEPTED = set(range(101001, 101035)) | {101040, 101052, 101053, 101054, 101055, 101056,
                                         1001, 1002, 1003, 1004, 1005, 1007, 1008, 1009, 1010,
                                         101050, 101051, 1050, 1051}
# Voices whose own output an ASR could not read back in Malay (measured ~15% vs 82-85%).
MALAY_HOSTILE = {101019, 101012}
# Documented EmotionCategory values. Sending an unknown one is an error, not a fallback.
EMOTIONS = {"", "neutral", "sad", "happy", "angry", "fear", "news", "story", "radio",
            "poetry", "call", "sajiao", "disgusted", "amaze", "peaceful", "exciting",
            "aojiao", "jieshuo"}

fails = []
for lang in ("EN", "ZH", "BM"):
    vid = Settings.tts_voice_for(lang)
    print(f"  {lang}: voice={vid or '(none)'}  fallback={Settings.tts_voice_is_fallback(lang)}")
    if not vid:
        fails.append(f"{lang} has no voice configured at all")
        continue
    try:
        n = int(vid)
    except ValueError:
        fails.append(f"{lang} voice {vid!r} is not an integer VoiceType")
        continue
    if n not in ACCEPTED:
        fails.append(f"{lang} voice {n} was not accepted when the catalogue was probed")

bm = Settings.tts_voice_for("BM")
try:
    if bm and int(bm) in MALAY_HOSTILE:
        fails.append(f"BM is pointed at {bm}, which is UNINTELLIGIBLE in Malay "
                     f"(measured ~15% transcription match). Use a Mandarin voice.")
except ValueError:
    pass

print(f"  emotion={Settings.TTS_EMOTION!r} intensity={Settings.TTS_EMOTION_INTENSITY}")
if Settings.TTS_EMOTION not in EMOTIONS:
    fails.append(f"EmotionCategory {Settings.TTS_EMOTION!r} is not a documented value")
if not 50 <= Settings.TTS_EMOTION_INTENSITY <= 200:
    fails.append(f"EmotionIntensity {Settings.TTS_EMOTION_INTENSITY} is outside [50, 200]")

if fails:
    for f in fails:
        print(f"  FAIL {f}")
    sys.exit(1)
print("  voice selection is consistent with the accent brief")
PYEOF
); then
  sed 's/^/  /' /tmp/aj_verify_tts.log
  ok "TTS voice selection is valid and Malay-safe"
else
  sed 's/^/  /' /tmp/aj_verify_tts.log
  bad "TTS voice selection is invalid -- see the reasons above"
fi

# The emotion must be sent ONLY when configured: an absent EmotionCategory and
# EmotionCategory="neutral" are different requests that produce different audio, so a blank
# setting has to mean "send nothing", not "send neutral".
if (cd "$BACKEND" && PYTHONPATH="$BACKEND" AUNTIEJOY_TTS_EMOTION= "$VENV_PY" -B -c '
from app.config import Settings
import sys
sys.exit(0 if Settings.TTS_EMOTION == "" else 1)'); then
  ok "an empty AUNTIEJOY_TTS_EMOTION really means 'send no EmotionCategory'"
else
  bad "a blank AUNTIEJOY_TTS_EMOTION did not resolve to empty"
fi

# -----------------------------------------------------------------------------
note "11b. The runtime mouth switch (Cartesia default, TRTC Mode, reported fallback)"
# -----------------------------------------------------------------------------
# The app has two mouths and the UI switches between them live. Four properties have to hold,
# and each one is a way this breaks without any error being raised:
#
#   * The DEFAULT must be Cartesia. It is the only mouth with a real Malay voice, so a
#     regression to a Tencent default would quietly make BM borrow an English voice.
#   * The word "tencent" must resolve to a REAL Tencent engine, never to nothing. The UI
#     sends that one word because it does not know whether TRTC streaming is configured.
#   * An unknown name must RAISE. A switch that appears to work but changes nothing is the
#     worst outcome for a control a presenter will click on stage.
#   * A fallback must be REPORTED, and must name the right engines. A UI that credits the
#     voice to the provider that just failed is worse than a silent fallback, because it is
#     confidently wrong.
#
# Offline: no network, no credentials, no cloud resource.
if (cd "$BACKEND" && PYTHONPATH="$BACKEND" AUNTIEJOY_TTS_PROVIDER= "$VENV_PY" -B - \
      >/tmp/aj_verify_prov.log 2>&1 <<'PYEOF'
import sys
from app.config import Settings
from app import tts

fails = []

# --- the default ------------------------------------------------------------------
print(f"  default provider = {Settings.TTS_PROVIDER_DEFAULT!r}")
if Settings.TTS_PROVIDER_DEFAULT != "cartesia":
    fails.append(f"the default mouth is {Settings.TTS_PROVIDER_DEFAULT!r}, not 'cartesia'")

# --- the switch -------------------------------------------------------------------
got = Settings.set_tts_provider("tencent")
print(f"  'tencent' -> {got!r}")
if got not in Settings.TENCENT_PROVIDERS:
    fails.append(f"'tencent' resolved to {got!r}, which is not a Tencent engine")
if not Settings.tts_provider_is_tencent():
    fails.append("after switching to tencent, tts_provider_is_tencent() is still False")

back = Settings.set_tts_provider("cartesia")
print(f"  'cartesia' -> {back!r}")
if back != "cartesia" or Settings.tts_provider_is_tencent():
    fails.append(f"switching back gave {back!r}")

try:
    Settings.set_tts_provider("nope")
    fails.append("an unknown provider name was ACCEPTED instead of raising")
except ValueError:
    print("  an unknown name raises ValueError (fails closed)")

# A switch must clear the previous provider's note, or /health reports a stale cause for an
# engine that has not even been tried yet.
Settings.note_tts_fallback("boom", failed="cartesia", served="texttovoice")
Settings.set_tts_provider("cartesia")
if Settings.TTS_FALLBACK_ACTIVE or Settings.TTS_FALLBACK_REASON:
    fails.append("switching providers did not clear the fallback note")
else:
    print("  switching providers clears the fallback note")

# --- who failed, and who actually answered -----------------------------------------
Settings.set_tts_provider("cartesia")
Settings.note_tts_fallback("cartesia: HTTP 402 quota_exceeded",
                           failed="cartesia", served="texttovoice")
print(f"  fallback: from={Settings.TTS_FALLBACK_FROM!r} to={Settings.TTS_FALLBACK_TO!r} "
      f"served={Settings.tts_served_provider()!r} selected={Settings.tts_provider()!r}")
if Settings.tts_served_provider() != "texttovoice":
    fails.append("tts_served_provider() does not report the engine that actually answered")
if Settings.tts_provider() != "cartesia":
    fails.append("a fallback changed the SELECTION -- the toggle would appear to move on its own")
if Settings.TTS_FALLBACK_FROM != "cartesia":
    fails.append("the engine that failed was not recorded")
Settings.clear_tts_fallback()
if Settings.tts_served_provider() != Settings.tts_provider():
    fails.append("after clearing, the served engine should equal the selected one")

# --- Cartesia language mapping ----------------------------------------------------
# A missing BM entry would NOT error: Malay would be sent to Cartesia tagged "en" and read
# aloud by an English voice. That is precisely the failure the default was chosen to avoid.
for ui, want in (("EN", "en"), ("ZH", "zh"), ("BM", "ms")):
    got_lang = tts.LANG_TO_CARTESIA.get(ui)
    print(f"  Cartesia language {ui} -> {got_lang!r}")
    if got_lang != want:
        fails.append(f"{ui} maps to {got_lang!r}, expected {want!r}")
if tts._cartesia_language("\u4f60\u597d", "EN") != "zh":
    fails.append("Chinese text inside an English session was not normalised to zh")

# --- the two speed scales are NOT interchangeable ---------------------------------
# Cartesia takes a plain multiplier in [0.6, 1.5]; Tencent takes an offset in [-2, 6].
print(f"  CARTESIA_SPEED={Settings.CARTESIA_SPEED} (a multiplier, [0.6, 1.5])")
if not 0.6 <= Settings.CARTESIA_SPEED <= 1.5:
    fails.append(f"CARTESIA_SPEED={Settings.CARTESIA_SPEED} is outside Cartesia's [0.6, 1.5]")

if fails:
    for f in fails:
        print(f"  FAIL {f}")
    sys.exit(1)
print("  the runtime switch, the default and the fallback record are all consistent")
PYEOF
); then
  sed 's/^/  /' /tmp/aj_verify_prov.log
  ok "the runtime mouth switch behaves (Cartesia default, 'tencent' maps to a Tencent engine)"
else
  sed 's/^/  /' /tmp/aj_verify_prov.log
  bad "the runtime mouth switch is broken -- see the reasons above"
fi

# A Tencent-scale speed value must NOT be forwarded to Cartesia: 6 is legal for Tencent and
# far outside Cartesia's [0.6, 1.5], where it is rejected outright. This is the one place the
# two providers' units could be silently conflated.
if (cd "$BACKEND" && PYTHONPATH="$BACKEND" CARTESIA_SPEED=6 "$VENV_PY" -B -c '
from app.config import Settings
import sys
print(f"  CARTESIA_SPEED=6 clamped to {Settings.CARTESIA_SPEED}")
sys.exit(0 if Settings.CARTESIA_SPEED == 1.5 else 1)'); then
  ok "an out-of-range CARTESIA_SPEED clamps instead of being sent to the API"
else
  bad "an out-of-range CARTESIA_SPEED was not clamped"
fi

# "Is this mouth configured?" must be answered PER PROVIDER. With Cartesia's key blanked, the
# Tencent mouth must still report itself configured -- otherwise /health either claims a mouth
# that cannot speak or hides one that can.
if (cd "$BACKEND" && PYTHONPATH="$BACKEND" CARTESIA_API_KEY= "$VENV_PY" -B -c '
from app.config import Settings
import sys
print(f"  with no Cartesia key: cartesia={Settings.cartesia_configured()} "
      f"tencent={Settings.tencent_configured()}")
sys.exit(0 if Settings.cartesia_configured() is False else 1)'); then
  ok "a blank Cartesia key reports Cartesia as unconfigured (per-provider, not global)"
else
  bad "a blank Cartesia key still reported Cartesia as configured"
fi

# -----------------------------------------------------------------------------
# 12. A re-deploy must not silently drop the domain (the HTTPS downgrade trap)
#
# provision.sh rebuilds /etc/caddy/Caddyfile from scratch and emits a site block only for
# $DOMAIN, so a deploy run with DOMAIN unset used to replace a working HTTPS domain config
# with an IP-only, HTTP-only one. Nothing in the deploy output looked wrong, because the app
# kept serving on 127.0.0.1:8078 -- the public endpoint simply stopped answering on 443 and
# the certificate stopped being presented. The guard is that provision.sh ADOPTS the domain
# the existing Caddyfile already names. This runs that exact guard against two fixtures, with
# the awk program pulled out of provision.sh so the test cannot drift away from the code.
printf '\n\033[1m== 12. A re-deploy keeps the domain (the HTTPS downgrade trap)\033[0m\n'

if grep -q 'ADOPT THE DOMAIN THE BOX IS ALREADY SERVING' deploy/provision.sh; then
  ok "provision.sh carries the domain-adoption guard"
else
  bad "provision.sh has lost the domain-adoption guard (a DOMAIN-less deploy will drop TLS)"
fi

ADOPT_AWK="$("$VENV_PY" - <<'PY'
import re
src = open('deploy/provision.sh', encoding='utf-8').read()
m = re.search(r"ADOPTED=\"\$\(awk '(.*?)' \\", src, re.S)
print(m.group(1) if m else '')
PY
)"

cat > /tmp/aj_caddy_domain_fixture <<'EOF'
(auntiejoy_handlers) {
	handle {
		file_server
	}
}

dbsjoy.xyz, www.dbsjoy.xyz {
	import auntiejoy_handlers
}
http://43.156.247.155 {
	import auntiejoy_handlers
}
EOF

cat > /tmp/aj_caddy_iponly_fixture <<'EOF'
(auntiejoy_handlers) {
	handle {
		file_server
	}
}

:80 {
	import auntiejoy_handlers
}
EOF

if [ -n "$ADOPT_AWK" ]; then
  GOT_DOMAIN="$(awk "$ADOPT_AWK" /tmp/aj_caddy_domain_fixture | tr -d ',' | head -1)"
  if [ "$GOT_DOMAIN" = "dbsjoy.xyz" ]; then
    ok "adopts the domain from an existing domain config ($GOT_DOMAIN)"
  else
    bad "expected dbsjoy.xyz from the domain fixture, got '$GOT_DOMAIN'"
  fi
  GOT_IPONLY="$(awk "$ADOPT_AWK" /tmp/aj_caddy_iponly_fixture | tr -d ',' | head -1)"
  if [ -z "$GOT_IPONLY" ]; then
    ok "adopts nothing from an IP-only config (an IP is not a domain)"
  else
    bad "expected nothing from the IP-only fixture, got '$GOT_IPONLY'"
  fi
  # The snippet label starts with "(" and the IP fallback with "http:"/":" -- neither may be
  # mistaken for a domain, or a DOMAIN-less deploy would invent one out of its own fallback.
  if awk "$ADOPT_AWK" /tmp/aj_caddy_domain_fixture | grep -qE '^\(|^http|^:'; then
    bad "the adoption pattern matched a non-domain site label"
  else
    ok "the adoption pattern ignores the handler snippet and the IP fallback"
  fi
else
  bad "could not extract the adoption awk program from provision.sh"
fi

# -----------------------------------------------------------------------------
printf '\n\033[1m== Summary ==\033[0m\n'
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32mReady to deploy.\033[0m  Next:  ./deploy/deploy.sh <public-ip>\n\n'
  exit 0
fi
printf '  \033[31mNOT ready.\033[0m Fix the failures above before deploying.\n\n'
exit 1
