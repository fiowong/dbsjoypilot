# Deploying JoyPilot to Tencent Cloud Lighthouse

This folder deploys JoyPilot to one Tencent Cloud Lighthouse server in Singapore. You create
the server in the console, then one command from your laptop copies the code up and sets
everything else up: the Python environment, a systemd service for the backend, and nginx
(or Caddy, if it is already on the server) in front.

| Script | What it does |
|---|---|
| `verify.sh` | Readiness check to run before every deploy. No network, no cloud |
| `preflight.sh` | Checks from outside that SSH, the firewall and DNS are ready |
| `deploy.sh` | Copies the repo to the server and runs `provision.sh` there |
| `provision.sh` | Runs on the server: installs packages, the service and the web server config |
| `create_lighthouse.py` | Optional: creates the server through the API instead of the console |
| `check_routes.py`, `check_route_shadowing.py` | Used by `verify.sh` to check the web server routes |

## Before you start

**Run the readiness check:**

```bash
./deploy/verify.sh
```

It runs 35 checks and exits with an error if any fail:

- shell and Python syntax, and the backend dependencies
- the full backend test suite (100/100 scenarios with the offline mock agents)
- the frontend API-address tests and the four jsdom frontend suites (skipped if jsdom is not installed)
- the nginx and Caddy route coverage (28/28)
- the voice and TTS checks, and the guard that keeps your domain on a re-deploy

It needs the backend virtual environment first: `cd backend && python3 -m venv .venv &&
./.venv/bin/pip install -r requirements.txt`.

**Make an SSH key** if you do not have one:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dbsjoy_lighthouse -N '' -C dbsjoy-lighthouse
cat ~/.ssh/dbsjoy_lighthouse.pub     # you paste this into the console below
```

All the scripts use `~/.ssh/dbsjoy_lighthouse` by default, so keeping that name means
nothing else needs configuring.

**Account verification.** Tencent Cloud may ask the account owner to verify their identity
before you can buy a server or a domain. Do that first; the scripts cannot work around it.

## 1. Create the server

In the console, go to **Lighthouse** and create an instance:

| Field | Value |
|---|---|
| Region | Singapore (ap-singapore) |
| Image | Ubuntu 22.04 LTS (24.04 also works) |
| Bundle | the smallest Linux bundle is enough (2 vCPU / 2 GB) |
| Login method | SSH key: paste the public key from above |
| Auto-renew | off, so it cannot charge you without you noticing |

You do not need a password. No ICP filing is needed for a server in Singapore.

Note the server's public IP.

**Firewall.** Lighthouse has its own firewall. In the instance's **Firewall** tab, allow:

| Protocol | Port | Source |
|---|---|---|
| TCP | 22 | your IP (or `0.0.0.0/0`) |
| TCP | 80 | `0.0.0.0/0` |
| TCP | 443 | `0.0.0.0/0` |

Do not open port 8078. The backend only listens on `127.0.0.1:8078` and is reached through
the web server.

## 2. Check the server is reachable

```bash
./deploy/preflight.sh <server-ip> [domain]
```

It checks that your SSH key is accepted, that ports 22, 80 and 443 are open, that 8078 is not
reachable from outside, and whether the domain (if you give one) points at the server. If
something is wrong it tells you which console setting to change. A timeout on 80/443 means the
firewall is blocking; "connection refused" is normal before the first deploy.

## 3. Deploy

From the repo root:

```bash
DOMAIN=<your-domain> ./deploy/deploy.sh <server-ip>
```

This:

1. checks SSH works
2. copies the repo up, leaving out `.env`, databases, virtual environments, `.bak` files and the frontend tests
3. installs the packages, builds the virtual environment and installs the backend dependencies
4. creates `backend/.env` from `.env.example` if it does not exist yet (with no secrets in it)
5. installs the `auntiejoy` systemd service. It runs one uvicorn worker, because conversations are held in memory
6. writes the web server config: Caddy if it is already installed and running, otherwise nginx
7. runs the backend test suite on the server and prints the result

Re-running the same command is how you ship a change.

**HTTPS and the mic.** Browsers only allow the microphone over HTTPS (or on localhost), so
for the voice demo you need a domain. With `DOMAIN` set, the deploy gets a free Let's Encrypt
certificate (through certbot for nginx; Caddy does it by itself). That works once the domain
points at the server and ports 80 and 443 are open.

If you have no domain, use `sslip.io`, which maps a name with the IP in it straight to that IP.
For a server at `43.156.247.155`:

```bash
DOMAIN=43-156-247-155.sslip.io ./deploy/deploy.sh 43.156.247.155
```

The site is then at `https://43-156-247-155.sslip.io`. Without any `DOMAIN` the site is served
over plain HTTP on the IP, and everything works except the mic.

**Re-deploys keep the domain (Caddy).** If the server uses Caddy and you run `deploy.sh` later
without `DOMAIN`, `provision.sh` reads the domain from the existing Caddy config and keeps it, so a re-deploy
does not quietly drop HTTPS. It prints a line saying which domain it kept.

## 4. Add your keys on the server

`.env` starts empty on purpose. Fill it in on the server and restart:

```bash
ssh -i ~/.ssh/dbsjoy_lighthouse ubuntu@<server-ip>
nano /opt/auntiejoy/backend/.env       # LLM key, Tencent SecretId / SecretKey / AppID, voices
sudo systemctl restart auntiejoy
curl -s localhost:8078/health | python3 -m json.tool
```

`/health` shows what is switched on: `engine` (live or mock), `llm_configured`,
`asr_configured`, `tts_configured` and the TTS provider. `backend/.env.example` lists the keys to
fill in and a few optional settings; the full list is in `backend/app/config.py`.

## Options

Put these in front of `deploy.sh`:

| Setting | Effect |
|---|---|
| `DOMAIN=example.com` | Serve on that domain with HTTPS |
| `ENABLE_TLS=0` | Set a domain but skip the certificate (for when DNS is not live yet) |
| `ENABLE_BASIC_AUTH=1` | Ask for a username and password before the page opens (off by default) |
| `BASIC_PASS=...` | Choose that password instead of a generated one. `BASIC_USER` sets the username (default `dbsjoy`) |
| `RUN_SELFTEST=0` | Skip the test suite on the server |

**Login is off by default.** Anyone with the URL can open the demo and use the chat, which also
means they can spend your Tencent voice credits. The keys themselves stay on the server and
never reach the browser. Turn on `ENABLE_BASIC_AUTH=1` if you want to limit who can open it.

## Using a domain you bought

Add two `A` records at your DNS provider, `@` and `www`, both pointing at the server IP. Delete
any default parking record first. Check they resolve:

```bash
dig +short A your-domain.com
dig +short A www.your-domain.com
```

When both print the server IP, run `preflight.sh` with the domain, then `deploy.sh` with
`DOMAIN` set.

If a new domain never resolves, check its status with the registry. "server hold" means the
registry has suspended it, and only the registrar can lift it, often after account identity
verification. Our first domain, `dbsjoy.xyz`, hit this. For `.xyz` names:

```bash
curl -s "https://rdap.centralnic.com/xyz/domain/your-domain.xyz" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])"
```

## Creating the server through the API (optional)

`create_lighthouse.py` creates the server without the console. It needs a Tencent Cloud API key
with `QcloudLighthouseFullAccess`; use a sub-user key, not the root key.

```bash
pip install tencentcloud-sdk-python
export TENCENTCLOUD_SECRET_ID=...
export TENCENTCLOUD_SECRET_KEY=...
python3 deploy/create_lighthouse.py --dry-run   # checks the API call, creates nothing
python3 deploy/create_lighthouse.py             # creates the server and prints its IP
```

It uploads your SSH key, makes it the only way to log in, opens ports 22, 80 and 443, and turns
auto-renew off. Run it once: each run creates a new server. It does not buy a domain or touch DNS.
