#!/usr/bin/env bash
# =============================================================================
# preflight.sh — check everything that can be verified BEFORE deploying.
#
#     ./deploy/preflight.sh <public-ip> [domain]
#
# Answers, from outside the instance, the questions that otherwise fail deep inside a
# deploy with an opaque error:
#   * can we even authenticate over SSH, and as which user?
#   * is the Lighthouse firewall actually letting 80/443 through?
#   * is 8078 (the backend port) correctly NOT exposed?
#   * does the domain resolve to this instance yet?
#
# Exit 0 = ready to deploy. Exit 1 = something must be fixed in the console first.
#
# WHY THIS EXISTS
#   "Permission denied (publickey)" and "port 80 times out" are two different console
#   problems with two different fixes, and both surface as a failed deploy several steps
#   in. Checking them first turns a confusing failure into a two-line instruction.
# =============================================================================
set -uo pipefail

HOST="${1:-}"
DOMAIN="${2:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/dbsjoy_lighthouse}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-6}"

if [ -z "$HOST" ]; then
  echo "usage: $0 <public-ip> [domain]" >&2
  exit 2
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 is required" >&2; exit 2; }

FIXES=()

banner() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# ------------------------------------------------------------------ SSH
banner "SSH authentication"
if [ ! -f "$SSH_KEY" ]; then
  echo "  FAIL  key not found: $SSH_KEY"
  FIXES+=("SSH key missing at $SSH_KEY — regenerate it, or set SSH_KEY=<path>")
else
  echo "  key   $SSH_KEY"
  AUTHED=0
  # Dedupe: REMOTE_USER defaults to 'ubuntu', which also appears in the fallback list.
  USERS=""
  for u in "$REMOTE_USER" ubuntu lighthouse root; do
    case " $USERS " in *" $u "*) continue ;; esac
    USERS="$USERS $u"
  done
  for USER_TRY in $USERS; do
    OUT="$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
              -o BatchMode=yes -o PreferredAuthentications=publickey \
              "$USER_TRY@$HOST" 'echo __OK__' 2>&1 | tail -1)"
    case "$OUT" in
      *__OK__*) echo "  PASS  authenticated as '$USER_TRY'"; AUTHED=1; REMOTE_USER="$USER_TRY"; break ;;
      *"Permission denied"*) echo "  ..    '$USER_TRY' rejected (key not bound to this instance)" ;;
      *) echo "  ..    '$USER_TRY': $(printf '%s' "$OUT" | tail -c 90)" ;;
    esac
  done
  if [ "$AUTHED" -ne 1 ]; then
    echo "  FAIL  no user accepted this key"
    SSH_FAILED=1
    FIXES+=("Bind the public key to the instance (see the key below), or supply the private key you used")
  fi
fi

# ------------------------------------------------------------------ ports
banner "Lighthouse firewall"
# A note on method: this probes with RAW SOCKETS on purpose. If an HTTP proxy is configured
# (http_proxy / HTTPS_PROXY), then `curl http://<ip>/` does NOT test the host at all -- it
# talks to the proxy, which returns a misleading 502 when it cannot reach upstream. A raw
# connect cannot be intercepted that way, so it is the only trustworthy signal here.
if [ -n "${http_proxy:-}${HTTP_PROXY:-}${https_proxy:-}${HTTPS_PROXY:-}" ]; then
  echo "  note  a proxy is configured in this shell, so use RAW SOCKET results below,"
  echo "        not curl, when judging whether a port is reachable."
fi
"$PY" - "$HOST" "$PROBE_TIMEOUT" <<'PYEOF'
import socket, sys, time
host, timeout = sys.argv[1], float(sys.argv[2])

# For 80/443 the question is "does the FIREWALL allow it?", not "is something listening?".
# REFUSED means the firewall lets the packet through and nothing is listening yet -- which is
# the CORRECT state before the web server is deployed. Only TIMEOUT (silently dropped) means
# the firewall is blocking. Treating REFUSED as a failure here produced a false alarm.
EXPECT = {
    22:   ("OPEN",    "firewall blocks SSH — nothing can be deployed",
                     "firewall allows SSH; sshd is not answering"),
    80:   ("ALLOWED", "firewall BLOCKS 80 — HTTP and TLS issuance will fail", ""),
    443:  ("ALLOWED", "firewall BLOCKS 443 — HTTPS will not work", ""),
    8078: ("BLOCKED", "firewall ALLOWS the backend port — close it, the app must not be reachable directly",
                     ""),
}

def verdict(state, want):
    if want == "OPEN":     return state == "OPEN"
    if want == "ALLOWED":  return state in ("OPEN", "REFUSED")
    if want == "BLOCKED":  return state in ("TIMEOUT", "REFUSED")
    return False

problems = []
for port, (want, on_timeout, on_refused) in EXPECT.items():
    s = socket.socket(); s.settimeout(timeout); t0 = time.time()
    try:
        s.connect((host, port)); state, note = "OPEN", ""
    except socket.timeout:
        state, note = "TIMEOUT", on_timeout
    except ConnectionRefusedError:
        state, note = "REFUSED", on_refused
    except Exception as exc:
        state, note = "ERROR", str(exc)
    finally:
        s.close()

    ok = verdict(state, want)
    detail = ""
    if state == "OPEN":
        detail = "listening"
    elif state == "REFUSED":
        detail = "firewall allows it; nothing listening yet" if want != "BLOCKED" else "allowed but nothing listening (acceptable)"
    elif state == "TIMEOUT":
        detail = "filtered by the firewall" if want != "BLOCKED" else "not reachable (correct)"
    print(f"  {'PASS' if ok else 'FAIL'}  {port:<5} {state:<9} {detail if ok else note}")
    if not ok:
        problems.append(port)
sys.exit(3 if problems else 0)
PYEOF
PORT_RC=$?
if [ "$PORT_RC" -eq 3 ]; then
  FIXES+=("Lighthouse console -> Firewall: add Allow rules for TCP 22, 80 and 443 (source 0.0.0.0/0). Do NOT open 8078.")
fi

# ------------------------------------------------------------------ DNS
if [ -n "$DOMAIN" ]; then
  banner "DNS for $DOMAIN"
  RESOLVED="$(dig +short A "$DOMAIN" 2>/dev/null | tail -1)"
  WWW="$(dig +short A "www.$DOMAIN" 2>/dev/null | tail -1)"
  [ -z "$RESOLVED" ] && RESOLVED="$(nslookup "$DOMAIN" 2>/dev/null | awk '/^Address: /{print $2}' | tail -1)"
  # Distinguish "registered but no A record" from "not registered at all" -- the second means
  # the domain must be BOUGHT before any DNS work makes sense, which is a very different task.
  # Trailing comma matters: dig prints "status: NXDOMAIN, id: 12345", so the field is
  # "NXDOMAIN," and a bare string compare against "NXDOMAIN" silently never matches.
  STATUS="$(dig "$DOMAIN" 2>/dev/null | awk -F'status: ' '/status:/{print $2}' | awk '{print $1}' | tr -d ',')"
  if [ -z "$RESOLVED" ] && [ "$STATUS" = "NXDOMAIN" ]; then
    echo "  $DOMAIN is NOT REGISTERED (DNS status: NXDOMAIN)."
    echo ""
    echo "  A DNS record cannot be added to a domain that does not exist yet."
    echo "  Register it first, then point it here:"
    echo "    1. buy the domain (any registrar)"
    echo "    2. add an A record:  @    -> $HOST"
    echo "    3. add an A record:  www  -> $HOST"
    echo "    4. re-run:  DOMAIN=$DOMAIN ./deploy/deploy.sh $HOST"
    echo ""
    echo "  The app is already reachable on http://$HOST in the meantime."
    FIXES+=("Register $DOMAIN, then add A records for @ and www pointing at $HOST")
  elif [ "$RESOLVED" = "$HOST" ]; then
    echo "  $DOMAIN      -> $RESOLVED"
    echo "  PASS  the apex already points here; TLS can be issued"
  else
    echo "  $DOMAIN      -> ${RESOLVED:-<no A record>}"
    echo "  www.$DOMAIN  -> ${WWW:-<no A record>}"
    echo "  ..    not pointing here yet"
    echo "        -> deploy over HTTP on the IP now, then re-run once DNS propagates:"
    echo "           DOMAIN=$DOMAIN ./deploy/deploy.sh $HOST"
    FIXES+=("Point $DOMAIN (and www) at $HOST — currently ${RESOLVED:-unresolved}")
  fi
else
  banner "DNS"
  echo "  no domain supplied — deploy will serve over HTTP on the IP"
fi

# ------------------------------------------------------------------ verdict
banner "Verdict"
if [ "${#FIXES[@]}" -eq 0 ]; then
  echo "  Ready to deploy:  ./deploy/deploy.sh $HOST${DOMAIN:+   (then DOMAIN=$DOMAIN ...)}"
  echo
  exit 0
fi
echo "  Fix these in the console first:"
echo
for f in "${FIXES[@]}"; do echo "    * $f"; done
echo
# Only show the key when SSH is actually the problem -- printing it for a DNS-only failure
# just adds noise to the one instruction that matters.
if [ "${SSH_FAILED:-0}" -eq 1 ] && [ -f "$SSH_KEY.pub" ]; then
  echo "  Public key to bind to the instance (Lighthouse -> instance -> key pair -> bind):"
  echo
  sed 's/^/    /' "$SSH_KEY.pub"
  echo
fi
exit 1
