#!/usr/bin/env bash
# Provision a FRESH Ubuntu 22.04/24.04 Lighthouse instance to serve AuntieJoy.
#
# Run this ON THE INSTANCE, from the repository root, as a user with sudo:
#
#     scp -i ~/.ssh/dbsjoy_lighthouse -r ./dbs-auntiejoy ubuntu@<ip>:~/
#     ssh -i ~/.ssh/dbsjoy_lighthouse ubuntu@<ip>
#     cd ~/dbs-auntiejoy && ./deploy/provision.sh
#
# It installs everything and leaves the app running under systemd behind Nginx or Caddy.
# It is IDEMPOTENT: re-running it re-applies config without duplicating anything.
#
# Environment overrides:
#     APP_DIR            (default /opt/auntiejoy)
#     DOMAIN             (default EMPTY = serve on the bare IP over HTTP, no TLS)
#     ENABLE_BASIC_AUTH  (default 0 = NO login prompt. Set 1 to require user/password.)
#     BASIC_USER         (default dbsjoy)   BASIC_PASS (generated if unset)
#     ENABLE_TLS         (default: 1 when DOMAIN is set, 0 when it is not)
#     RUN_SELFTEST       (default 1)        set 0 to skip the scenario suite
#
# ABOUT ENABLE_BASIC_AUTH=0 (THE DEFAULT)
#   The site is served with NO authentication. Anyone with the URL can open it, use the chat,
#   and reach every API route the page calls -- including /turn, /tts and /ws/stt. That is a
#   deliberate choice for a demo, because the login prompt got in the way of showing the app,
#   but the consequence should be stated plainly rather than glossed over:
#     * the URL is public. It is not indexed or linked anywhere, but it is not secret either.
#     * the Tencent voice credentials are never exposed (they stay server-side), but anyone can
#       spend them by driving /tts and /ws/stt.
#     * the synthetic ledger, the trusted repository and the audit trail are all reachable.
#   None of that is real money or real customer data, which is what makes it acceptable here.
#   Turn it back on with ENABLE_BASIC_AUTH=1, or per-deploy with BASIC_PASS=... .
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/auntiejoy}"
DOMAIN="${DOMAIN:-}"
# ADOPT THE DOMAIN THE BOX IS ALREADY SERVING when the caller did not pass one.
#
# This is not a convenience, it is a guard against a silent downgrade. provision.sh rebuilds
# /etc/caddy/Caddyfile from scratch and only emits a site block for $DOMAIN, so a plain
# re-deploy with DOMAIN unset used to replace a working HTTPS domain config with an
# IP-only, HTTP-only one. Nothing in the deploy output looked wrong, because the app itself
# kept serving perfectly on 127.0.0.1:8078 - the public endpoint simply stopped answering on
# 443, and the certificate stopped being presented. Recovery needed the domain config to be
# reconstructed by hand.
#
# So: if the existing Caddyfile already names a domain, keep naming it. Only an explicit
# DOMAIN= (or no existing Caddyfile at all) changes the answer.
if [ -z "$DOMAIN" ] && [ -r /etc/caddy/Caddyfile ]; then
    # A site label looks like "dbsjoy.xyz, www.dbsjoy.xyz {". The handler snippet starts with
    # "(" and the IP fallback starts with "http:" or ":", so this pattern matches only a real
    # domain label. $1 is the first host, still carrying its trailing comma.
    ADOPTED="$(awk '/^[a-z0-9][a-z0-9.-]*[ ,].*\{[[:space:]]*$/ {print $1; exit}' \
        /etc/caddy/Caddyfile 2>/dev/null | tr -d ',' || true)"
    # Guard against adopting the IP fallback if the pattern ever loosens.
    case "$ADOPTED" in
        ''|*[!a-zA-Z0-9.-]*) : ;;
        *[a-zA-Z]*)          DOMAIN="$ADOPTED"
                             printf '\n\033[1;33m==> DOMAIN not passed; keeping the domain the box already serves: %s\033[0m\n' "$DOMAIN" ;;
        *)                   : ;;
    esac
fi
# The instance's own public IP, passed in by deploy.sh. Used to add an HTTP-only fallback
# site, so the app stays reachable even when the domain does not resolve.
PUBLIC_IP="${PUBLIC_IP:-}"
ENABLE_BASIC_AUTH="${ENABLE_BASIC_AUTH:-0}"
BASIC_USER="${BASIC_USER:-dbsjoy}"
RUN_SELFTEST="${RUN_SELFTEST:-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# TLS only makes sense once a domain resolves here, so it defaults on only when DOMAIN is set.
if [ -z "${ENABLE_TLS:-}" ]; then
    if [ -n "$DOMAIN" ]; then ENABLE_TLS=1; else ENABLE_TLS=0; fi
fi
# server_name: the domain when we have one, otherwise the catch-all so a bare IP works.
if [ -n "$DOMAIN" ]; then SERVER_NAMES="$DOMAIN www.$DOMAIN"; else SERVER_NAMES="_"; fi

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ -f "$REPO_ROOT/backend/requirements.txt" ] || die "run this from the repo root (backend/ not found at $REPO_ROOT)"
command -v sudo >/dev/null || die "sudo is required"

# ---------------------------------------------------------------- web server choice
# Prefer Caddy when it is already installed AND running. Some images ship it, it already holds
# port 80, and it gives AUTOMATIC HTTPS -- which removes certbot from the picture entirely. It
# also proxies WebSocket and streams responses with none of nginx's per-location tuning.
# NEVER run both: they would fight over port 80 and whichever lost would fail to start.
WS="nginx"
if command -v caddy >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
    WS="caddy"
fi

# ---------------------------------------------------------------- packages
log "Installing system packages (web server: $WS)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-venv python3-pip rsync curl ca-certificates
if [ "$WS" = "caddy" ]; then
    echo "  Caddy is already installed and running -- no web server package needed."
else
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx apache2-utils
    if [ "$ENABLE_TLS" = "1" ]; then
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-certbot-nginx
    fi
fi

# ---------------------------------------------------------------- app files
log "Copying the application to $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo rsync -a --delete --exclude '.venv' --exclude '__pycache__' \
     --exclude '*.db' --exclude '.env' \
     "$REPO_ROOT/backend/"  "$APP_DIR/backend/"
sudo rsync -a --delete --exclude 'node_modules' \
     "$REPO_ROOT/frontend/" "$APP_DIR/frontend/"
sudo chown -R "$USER":"$USER" "$APP_DIR"

# ---------------------------------------------------------------- venv + deps
log "Creating the virtualenv and installing dependencies"
python3 -m venv "$APP_DIR/backend/.venv"
"$APP_DIR/backend/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/backend/.venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"

# ---------------------------------------------------------------- .env
# Never overwrite an existing .env: it holds the live credentials and the operator's edits.
if [ ! -f "$APP_DIR/backend/.env" ]; then
    log "Seeding backend/.env from .env.example (FILL IN YOUR CREDENTIALS)"
    cp "$APP_DIR/backend/.env.example" "$APP_DIR/backend/.env"
    chmod 600 "$APP_DIR/backend/.env"
    echo
    echo "  !!  $APP_DIR/backend/.env was created EMPTY of secrets."
    echo "      Edit it and set the LLM + Tencent voice credentials before the demo."
    echo "      /health reports exactly which providers are configured."
    echo
else
    log "backend/.env already exists, leaving it untouched"
    chmod 600 "$APP_DIR/backend/.env"
fi

# ---------------------------------------------------------------- repository seed
# WHY THIS STEP EXISTS
#   The trusted repository seeds with INSERT OR IGNORE on the natural key, so it is
#   idempotent -- but it therefore NEVER updates a row that already exists. A deploy that
#   ships a new seed leaves the instance reading the old rows, and the only visible symptom
#   is that one feature quietly stops working. Concretely: a seed that adds the nickname
#   "ah girl" to Mei Tan does nothing on an instance whose auntiejoy_repo.db predates it,
#   so the voice pipeline appears unable to recognise names when it is only reading stale
#   rows.
#   Note the rsync above deliberately excludes '*.db', so the database on the instance
#   survives every deploy -- which is exactly why this check has to exist.
#
#   Only the TRUSTED REPOSITORY is rebuilt. The audit database is the append-only
#   hash-chained compliance record and is never opened by the tool.
log "Checking the trusted repository against data.py"
REPO_TOOL="$APP_DIR/backend/tools/reseed_repository.py"
if [ -f "$REPO_TOOL" ]; then
    if ( cd "$APP_DIR/backend" && PYTHONPATH="$APP_DIR/backend" \
         "$APP_DIR/backend/.venv/bin/python" -B tools/reseed_repository.py --check ); then
        log "  repository is in step with data.py"
    else
        echo "  !!  the repository is stale (the INSERT OR IGNORE trap). Rebuilding it now."
        echo "      The AUDIT database is NOT touched."
        ( cd "$APP_DIR/backend" && PYTHONPATH="$APP_DIR/backend" \
          "$APP_DIR/backend/.venv/bin/python" -B tools/reseed_repository.py --apply )
    fi
else
    echo "  !!  $REPO_TOOL not found -- skipping the seed check (the app will still run)."
fi

# ---------------------------------------------------------------- systemd
log "Installing the systemd service (single uvicorn worker)"
sudo tee /etc/systemd/system/auntiejoy.service >/dev/null <<UNIT
[Unit]
Description=AuntieJoy backend (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR/backend
# ONE worker on purpose: session state lives in-process, so a second worker would
# answer half the turns with a session it has never seen.
ExecStart=$APP_DIR/backend/.venv/bin/python -m uvicorn app.main:app \\
          --host 127.0.0.1 --port 8078 --workers 1
Restart=always
RestartSec=3
# The .env is read from the working directory, which is why WorkingDirectory matters.
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable auntiejoy >/dev/null
sudo systemctl restart auntiejoy

# ---------------------------------------------------------------- basic auth
# OFF by default -- see the ENABLE_BASIC_AUTH note at the top of this script. The two block
# variables below are what the web-server configs interpolate, so "off" genuinely removes the
# directive rather than leaving a stale credential behind.
CADDY_AUTH_BLOCK=""
NGINX_AUTH_BLOCK=""
BASIC_HASH=""
GENERATED_PASS=""

if [ "$ENABLE_BASIC_AUTH" != "1" ]; then
    log "HTTP Basic Auth is OFF -- the site is served with no login prompt"
    echo "  Anyone with the URL can open the app and reach every API route the page calls."
    echo "  Re-enable with:  ENABLE_BASIC_AUTH=1 ./deploy/provision.sh"
    # Drop any credential from an earlier run: leaving /etc/nginx/.htpasswd on disk would be a
    # password file nobody reads, which is worse than no password file.
    sudo rm -f /etc/nginx/.htpasswd 2>/dev/null || true
else
    log "Configuring HTTP Basic Auth"
    # RE-RUNNING A DEPLOY MUST NOT CHANGE THE OPERATOR'S PASSWORD.
    # This used to regenerate unconditionally, which silently locked the operator out of a site
    # they were already using -- and the replacement only ever appeared in this script's output,
    # which by then had scrolled away. So: reuse what is already on the box unless BASIC_PASS is
    # passed explicitly. The plaintext is then unknowable here, which is correct -- it is a hash.
    if [ -n "${BASIC_PASS:-}" ]; then
        log "  BASIC_PASS was supplied -- replacing the credential"
    elif [ "$WS" = "caddy" ] && [ -r /etc/caddy/Caddyfile ]; then
        # Caddy stores a bcrypt hash, so the existing one can simply be carried across.
        BASIC_HASH="$(grep -oE '\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}' /etc/caddy/Caddyfile | head -1)"
    fi

    if [ "$WS" = "caddy" ]; then
        if [ -n "$BASIC_HASH" ]; then
            log "  reusing the existing Basic Auth credential (pass BASIC_PASS=... to change it)"
        else
            if [ -z "${BASIC_PASS:-}" ]; then
                BASIC_PASS="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 16)"
                GENERATED_PASS="1"
            fi
            BASIC_HASH="$(caddy hash-password --plaintext "$BASIC_PASS")"
        fi
        CADDY_AUTH_BLOCK="    basic_auth {
        $BASIC_USER $BASIC_HASH
    }
"
    else
        # nginx keeps a file rather than a hash in the config, so "already set up" means the file
        # exists and is not empty. Only rewrite it when we have something new to write.
        if [ -n "${BASIC_PASS:-}" ] || [ ! -s /etc/nginx/.htpasswd ]; then
            if [ -z "${BASIC_PASS:-}" ]; then
                BASIC_PASS="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 16)"
                GENERATED_PASS="1"
            fi
            sudo htpasswd -bc /etc/nginx/.htpasswd "$BASIC_USER" "$BASIC_PASS" >/dev/null
        else
            log "  reusing the existing /etc/nginx/.htpasswd (pass BASIC_PASS=... to change it)"
        fi
        sudo chown root:www-data /etc/nginx/.htpasswd
        sudo chmod 640 /etc/nginx/.htpasswd
        NGINX_AUTH_BLOCK="    auth_basic           \"DBSJoy demo\";
    auth_basic_user_file /etc/nginx/.htpasswd;

"
    fi
fi

# The route list below is the COMPLETE set from app/main.py, not just what the browser calls
# today. Anything NOT matched falls through to the static file server and comes back as
# index.html with a 200 -- i.e. HTML where the caller expected JSON. Keep it exhaustive:
#   /health /stages /scenarios /turn /confirm /field_signal
#   /scenario/{id} /audit/{id} /session/{id}
# /tts and /ws/stt get their own blocks and must not be swallowed by the API rule.

if [ "$WS" = "caddy" ]; then
    # A real domain makes Caddy fetch and renew a certificate automatically. With no domain
    # (or TLS disabled) serve plain HTTP on :80, so a bare IP still works.
    if [ -n "$DOMAIN" ] && [ "$ENABLE_TLS" = "1" ]; then
        CADDY_SITE="$DOMAIN, www.$DOMAIN"
        # A SECOND, HTTP-only site on the bare IP.
        #
        # Caddy's automatic HTTPS redirects http://<ip> to https://<ip>, and no certificate
        # exists for a bare IP -- so without this block the app has NO reachable URL at all
        # whenever the domain fails to resolve. Not hypothetical: dbsjoy.xyz is currently
        # on a registry 'server hold', which removes it from the DNS zone entirely, so the
        # domain answers NXDOMAIN at the TLD while this instance stays perfectly healthy.
        # 'http://' in the site label is what disables automatic HTTPS for this block only.
        if [ -n "$PUBLIC_IP" ]; then
            CADDY_EXTRA_BLOCK="http://$PUBLIC_IP {
$CADDY_AUTH_BLOCK	import auntiejoy_handlers
}"
        else
            CADDY_EXTRA_BLOCK=""
        fi
    else
        CADDY_SITE=":80"
        CADDY_EXTRA_BLOCK=""
    fi
    log "Writing the Caddyfile (site: $CADDY_SITE)"
    # Keep the stock file once, so this change stays reversible.
    [ -f /etc/caddy/Caddyfile.orig ] || sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig
    sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
# AuntieJoy -- generated by deploy/provision.sh. The stock config is kept at Caddyfile.orig.
#
# The route handlers live in ONE snippet, imported by every site block. A second copy for
# the IP fallback would eventually be edited in one place only -- which is precisely the
# class of drift the /tts matcher notes below were written to prevent.
(auntiejoy_handlers) {
	# Ears: Caddy proxies the WebSocket upgrade with no extra configuration needed.
	# 'path /ws/stt' is an EXACT match, not '/ws/stt*'. See the /tts note below.
	@stt path /ws/stt
	handle @stt {
		reverse_proxy 127.0.0.1:8078
	}

	# Streaming mouth: flush_interval -1 turns response buffering off, so audio is sent as it
	# is produced instead of arriving as one lump at the end of the turn.
	#
	# EXACT match, deliberately -- NOT 'handle /tts*'. A prefix matcher also captures
	# '/tts.js', which is a REAL frontend file: the browser asked for JavaScript, got the
	# API's 404 JSON, refused to execute it, and 'stopSpeaking' was therefore never defined.
	# liveFreeTurn() calls stopSpeaking() as its FIRST statement (barge-in), so every send
	# threw ReferenceError and the chat went completely dead -- while /turn itself stayed
	# perfectly healthy on the wire. One prefix matcher, two unrelated-looking symptoms.
	@tts path /tts
	handle @tts {
		reverse_proxy 127.0.0.1:8078 {
			flush_interval -1
		}
	}

	# The API surface, same origin as the page.
	#
	# '/tts/*' is a WILDCARD, and it is the right tool here: it covers /tts/provider (the
	# provider switch) and any future /tts/... route, while the literal '/' before the
	# wildcard is exactly what stops it also capturing '/tts.js'. A bare '/tts*' would
	# capture that real frontend file and take the chat down -- see the note above.
	@api path /turn /confirm /field_signal /health /stages /scenarios /scenario/* /audit/* /session/* /tts/*
	handle @api {
		reverse_proxy 127.0.0.1:8078
	}

	# Everything else is the static frontend.
	handle {
		root * $APP_DIR/frontend
		try_files {path} /index.html
		file_server
	}
}

$CADDY_SITE {
$CADDY_AUTH_BLOCK	import auntiejoy_handlers
}
$CADDY_EXTRA_BLOCK
CADDY

    # Validate BEFORE reloading: a broken Caddyfile would take the site down on reload.
    if sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
        sudo systemctl reload caddy
        echo "  Caddy reloaded."
    else
        echo "ERROR: the generated Caddyfile is invalid. Restoring the stock config." >&2
        sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >&2 || true
        sudo cp /etc/caddy/Caddyfile.orig /etc/caddy/Caddyfile
        sudo systemctl reload caddy 2>/dev/null || true
        die "Caddyfile validation failed -- nothing was changed"
    fi
else
    log "Writing the Nginx vhost (server_name: $SERVER_NAMES)"
    sudo tee /etc/nginx/sites-available/auntiejoy >/dev/null <<NGINX
server {
    listen 80;
    server_name $SERVER_NAMES;

$NGINX_AUTH_BLOCK    root $APP_DIR/frontend;
    index index.html;

    location / { try_files \$uri \$uri/ /index.html; }

    # 'tts' is in the alternation for the same reason Caddy has '/tts/*': it covers
    # /tts/provider without touching /tts.js, because the group is followed by (/|$).
    # The exact 'location = /tts' below still wins for the streaming path itself.
    location ~ ^/(turn|confirm|field_signal|health|stages|scenarios|scenario|session|audit|tts)(/|$) {
        proxy_pass http://127.0.0.1:8078;
        include /etc/nginx/proxy_params;
    }

    # Streaming mouth: buffering OFF or the audio lands in one lump.
    # '= ' makes this an EXACT match. A bare 'location /tts' is a PREFIX match and would also
    # capture /tts.js -- a real frontend file -- handing the browser JSON where it expects
    # JavaScript. That single character is the difference between a working chat and a dead one.
    location = /tts {
        proxy_pass http://127.0.0.1:8078;
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;
        include /etc/nginx/proxy_params;
    }

    # Ears: the WebSocket needs the Upgrade handshake to survive the proxy.
    location = /ws/stt {
        proxy_pass http://127.0.0.1:8078;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        include /etc/nginx/proxy_params;
    }
}
NGINX

    sudo ln -sf /etc/nginx/sites-available/auntiejoy /etc/nginx/sites-enabled/auntiejoy
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t
    sudo systemctl reload nginx
fi

# ---------------------------------------------------------------- tls
if [ "$WS" = "caddy" ]; then
    if [ -n "$DOMAIN" ] && [ "$ENABLE_TLS" = "1" ]; then
        log "TLS: Caddy issues and renews the certificate for $DOMAIN automatically"
        echo "  Requirements: DNS must resolve here AND ports 80/443 must be reachable."
        echo "  Verify with:  curl -I https://$DOMAIN"
    else
        log "No domain: serving plain HTTP on the instance IP"
        echo "  Add the domain later with:  DOMAIN=yourdomain ./deploy/deploy.sh <ip>"
    fi
elif [ "$ENABLE_TLS" = "1" ] && [ -n "$DOMAIN" ]; then
    log "Requesting a TLS certificate for $DOMAIN (needs DNS to already point here)"
    if sudo certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" \
         --non-interactive --agree-tos --register-unsafely-without-email --redirect; then
        echo "TLS issued; http is redirected to https."
    else
        echo "certbot failed (DNS not pointing here yet?). The site still works over HTTP."
        echo "Re-run later with: sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN"
    fi
elif [ -z "$DOMAIN" ]; then
    log "No DOMAIN set: serving over plain HTTP on the instance IP (no TLS)"
    echo "  Add a domain later with:"
    echo "    DOMAIN=yourdomain sudo -E ./deploy/provision.sh"
    echo "  which re-runs this script and issues the certificate."
fi

# ---------------------------------------------------------------- verify
log "Verifying"
sleep 2
if curl -fsS http://127.0.0.1:8078/health >/dev/null; then
    echo "backend /health: OK"
    curl -fsS http://127.0.0.1:8078/health | python3 -m json.tool | head -30
else
    echo "backend /health did NOT answer; check: sudo journalctl -u auntiejoy -n 50"
fi

if [ "$RUN_SELFTEST" = "1" ]; then
    log "Running the scenario suite on the instance"
    # AUNTIEJOY_DB IS REDIRECTED TO A THROWAWAY FILE. Do not remove that override.
    #
    # app/audit.py builds its store from Settings.DB_PATH at IMPORT time, so without this the
    # suite writes its ~1100 synthetic scenario events straight into the LIVE audit database.
    # Measured on this instance before the fix: 7976 of 8108 events (98%) were selftest noise
    # left by previous deploys, against only 9 real chat sessions. The append-only audit chain
    # is the artefact this project exists to demonstrate, so filling it with test fixtures is
    # the one side effect worth being careful about.
    #
    # The LLM keys are blanked so the suite exercises the deterministic mock agents: it is
    # then fast, free and reproducible, and its expected scenario counts are only valid there.
    # Blanked rather than unset, because python-dotenv re-adds any key that is absent from the
    # environment -- 'env -u HUNYUAN_API_KEY' would simply let .env put it back.
    ( cd "$APP_DIR/backend" && \
        AUNTIEJOY_DB=/tmp/aj_instance_selftest.db \
        HUNYUAN_API_KEY= SILICONFLOW_API_KEY= \
        ./.venv/bin/python -m app.selftest --selftest 2>&1 | tail -8 ) \
        || echo "selftest did not pass; investigate before the demo"
fi

log "Done"
if [ -n "$DOMAIN" ]; then
    # Test the VALUE, not just whether it is set: ${ENABLE_TLS:+s} would expand for "0"
    # too, since "0" is a non-empty string, and print an https:// URL that does not exist.
    if [ "$ENABLE_TLS" = "1" ]; then REACH="https://$DOMAIN"; else REACH="http://$DOMAIN"; fi
else
    REACH="http://$(curl -fsS --max-time 3 ifconfig.me 2>/dev/null || echo '<public-ip>')"
fi

# Only show a password when there actually is one. When auth is off, say so plainly rather
# than printing a blank that looks like a missing value.
if [ "$ENABLE_BASIC_AUTH" != "1" ]; then
    BASIC_LINE="OFF -- no login prompt; anyone with the URL can use the app"
elif [ -n "$GENERATED_PASS" ]; then
    BASIC_LINE="user '$BASIC_USER'  password '$BASIC_PASS'   <-- NEW, note it now"
else
    BASIC_LINE="user '$BASIC_USER'  password UNCHANGED (pass BASIC_PASS=... to rotate)"
fi
cat <<SUMMARY

  App dir      : $APP_DIR
  Service      : sudo systemctl status auntiejoy
  Logs         : sudo journalctl -u auntiejoy -f
  Basic Auth   : $BASIC_LINE
  URL          : $REACH

  Still to do by hand:
    1. Edit $APP_DIR/backend/.env with the LLM + Tencent credentials, then
       sudo systemctl restart auntiejoy
    2. Check $REACH/health reports engine: live and the voice fields.
    3. Open $REACH in a browser and check the voice-first composer and the mic.
SUMMARY
