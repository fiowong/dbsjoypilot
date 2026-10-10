#!/usr/bin/env bash
# Deploy AuntieJoy from this machine to a Lighthouse instance.
#
#     ./deploy/deploy.sh <public-ip> [ssh-key]
#
# Copies the repository to the instance and runs provision.sh there. Re-running it is the
# normal way to ship a change: it re-syncs the files and restarts the service.
set -euo pipefail

HOST="${1:-}"
SSH_KEY="${2:-$HOME/.ssh/dbsjoy_lighthouse}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
# Empty by default: serve on the bare IP over HTTP. Set DOMAIN=... once it resolves here and
# the same command re-provisions and issues the certificate.
DOMAIN="${DOMAIN:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "$HOST" ]; then
    echo "usage: $0 <public-ip> [ssh-key]" >&2
    exit 2
fi
if [ ! -f "$SSH_KEY" ]; then
    echo "ERROR: SSH key not found: $SSH_KEY" >&2
    exit 2
fi

# BatchMode + publickey-only: fail fast instead of blocking on an interactive password
# prompt. This instance offers password auth, so without these flags ssh stops and waits
# for input forever, which looks exactly like a hang.
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o BatchMode=yes -o PreferredAuthentications=publickey)

echo "==> Checking SSH to $REMOTE_USER@$HOST"
# `|| true` matters: under `set -e` a failing command substitution would abort the script
# before the diagnostic below could explain WHY it failed.
SSH_RESULT="$(ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST" \
    'echo connected; . /etc/os-release && echo "$PRETTY_NAME"' 2>&1 || true)"
if ! printf '%s' "$SSH_RESULT" | grep -q '^connected'; then
    echo "$SSH_RESULT" | tail -3 | sed 's/^/    /' >&2
    echo "" >&2
    # Distinguish the two very different causes: they need different fixes.
    case "$SSH_RESULT" in
        *"Permission denied"*)
            echo "ERROR: reached the instance, but the key was rejected." >&2
            echo "       '$SSH_KEY' is not bound to this instance (or the login user is wrong)." >&2
            echo "       Fix: Lighthouse console -> instance -> key pair -> bind the public key," >&2
            echo "            or pass the private key you actually used: $0 $HOST <key>" >&2
            ;;
        *"Connection timed out"*|*"Operation timed out"*|*"No route to host"*)
            echo "ERROR: cannot reach $HOST:22 at all." >&2
            echo "       Fix: Lighthouse console -> Firewall -> allow TCP 22." >&2
            ;;
        *)
            echo "ERROR: SSH failed for an unexpected reason (see above)." >&2
            ;;
    esac
    echo "" >&2
    echo "Run ./deploy/preflight.sh $HOST to check SSH, the firewall and DNS in one pass." >&2
    exit 1
fi
printf '%s\n' "$SSH_RESULT" | sed 's/^/    /'

echo "==> Copying the repository (excluding secrets, venvs and databases)"
rsync -az --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude 'node_modules' \
    --exclude '*.db' --exclude '.env' --exclude '.git' \
    --exclude '*.bak' --exclude '*~*' --exclude '*.test.js' --exclude 'test-api-base.mjs' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$REPO_ROOT/" "$REMOTE_USER@$HOST:~/auntiejoy-dcta-main/"

echo "==> Provisioning on the instance"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST" \
    "cd ~/auntiejoy-dcta-main && DOMAIN='$DOMAIN' PUBLIC_IP='$HOST' ./deploy/provision.sh"

echo
if [ -n "$DOMAIN" ]; then
    echo "==> Deployed. Open http://$DOMAIN  (or https:// once certbot has run)"
else
    echo "==> Deployed. Open http://$HOST  (HTTP on the instance IP; add a domain later with"
    echo "    DOMAIN=yourdomain ./deploy/deploy.sh $HOST)"
fi
echo "    Reminder: backend/.env on the instance must hold the live credentials."
