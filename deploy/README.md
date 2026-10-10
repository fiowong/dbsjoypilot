# Deploying AuntieJoy to Tencent Cloud Lighthouse

**Your chosen path: you create the instance in the console, I do everything after that.
No domain yet — we serve over HTTP on the instance IP, and add the domain + TLS later.**

> **Adding the EdgeOne layer (DNS + CDN + WAF + free certificate):** see
> [`../DEPLOY-EDGEONE.md`](../DEPLOY-EDGEONE.md). Short version: the origin below is provisioned
> and verified **first**, then EdgeOne is put in front of it. AuntieJoy cannot run on EdgeOne
> Makers alone — its WebSocket, streaming TTS and in-process session state need a persistent
> origin. The reusable procedure is the `tencent-edgeone-deploy` skill.

---

## What is already done

| Item | Status |
|---|---|
| SSH key pair | **NOT PRESENT on this machine** — there is no `~/.ssh` at all. Generate one (STEP 1 below) and bind it to the instance, or obtain the existing private key. `deploy.sh` cannot run until this exists. |
| Tencent Cloud SDK | Installed on the original author's machine; **not required** unless you use `create_lighthouse.py` |
| Instance-creation script | **Written & validated** — `deploy/create_lighthouse.py` (optional, see below) |
| Provisioning script | **Written & syntax-checked** — `deploy/provision.sh` |
| One-command deploy | **Written & syntax-checked** — `deploy/deploy.sh` |
| Frontend for a deployed page | **Fixed** — resolves the page's own origin on port 80/443 |
| Nginx + Basic Auth + systemd | **Automated** inside `provision.sh` (or Caddy, when present) |
| Web server detection | **Automatic** — prefers Caddy if installed & running, else nginx |
| Readiness gate | **Written & GREEN in this repo** — `deploy/verify.sh` (**35 passed, 0 failed**). Run it first. |
| Reachability preflight | **Written & tested** — `deploy/preflight.sh` (SSH, firewall, DNS) |
| Route/Nginx coverage check | **Written & green** — `deploy/check_routes.py` (28/28) |
| Frontend resolver test | **Written & green** — `frontend/test-api-base.mjs` (6 cases) |

### The SSH key (do this before STEP 1)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dbsjoy_lighthouse -N '' -C dbsjoy-lighthouse
cat ~/.ssh/dbsjoy_lighthouse.pub     # paste this into the console in STEP 1
```

`deploy.sh`, `preflight.sh` and `create_lighthouse.py` all default to
`~/.ssh/dbsjoy_lighthouse`, so keeping that name means nothing else needs configuring.

---

## STEP 0 — Before anything: run the readiness gate

```bash
cd dbs-auntiejoy
./deploy/verify.sh
```

It runs **35 checks** and exits non-zero if anything is wrong: shell and Python syntax,
dependencies, the full scenario suite (**100/100** against the deterministic mock agents, with the
ledger held at 3/3), all six frontend API-base modes, route coverage through **both** the nginx
vhost and the Caddy config (28/28), the `/tts.js` shadowing guard, repository seed drift
(detected, repaired, and refused on the audit database), the four jsdom frontend suites, the TTS
voice + Malay checks, the runtime mouth switch, and the domain-adoption guard. It touches no
cloud resource and no network.

> Sections 4/5/6 of the original file (the DBS payment rail configured, fail-closed, and
> idempotent-on-replay) and section 15 (the JSONL export) are **not present here** — this repo
> has no `app/rails.py` and does not ship `export_turns.py` / `cos_sync.py`. See the header
> comment in `verify.sh`.

> Two real bugs were found and fixed by doing this: the Nginx vhost was missing a proxy rule
> for `/session/{id}` (it would have returned HTML where the app expects JSON), and the final
> summary printed `https://` even when TLS was off. Both are now covered by tests.

---

## STEP 1 — You: create the instance in the console

Console → **Lighthouse** → Create instance. These are the exact settings:

| Field | Value |
|---|---|
| Region | **Singapore (ap-singapore)** |
| Availability zone | any (leave default) |
| Image / blueprint | **Ubuntu 22.04 LTS** (24.04 also works) |
| Bundle | **Linux Starter — USD 4.20/month** (2 vCPU / 2 GB / 40 GB SSD / 1 TB traffic) |
| Instance name | `DBSJoy-demo` |
| Purchase duration | 1 month |
| Auto-renew | **off** (manual) — so it cannot silently charge you |
| Login method | **SSH key** → "Create key pair" or "Use existing key" |
| SSH key | paste the public key above (or upload `~/.ssh/dbsjoy_lighthouse.pub`) |

**Do not set a password.** The scripts never need one.

After it is created, note the **public IP** from the instance list.

> ICP filing: not required. This is the international site in Singapore.

### Firewall

Lighthouse has its own firewall, separate from any OS firewall. Add three rules
(**Firewall** tab → Add rule):

| Protocol | Port | Source | Action |
|---|---|---|---|
| TCP | 22 | `0.0.0.0/0` (or your IP, safer) | Allow |
| TCP | 80 | `0.0.0.0/0` | Allow |
| TCP | 443 | `0.0.0.0/0` | Allow |

**Do not open 8078.** The backend binds to `127.0.0.1:8078` and is reached only through Nginx.

---

## STEP 1b — Check it is actually reachable (run this first, always)

```bash
./deploy/preflight.sh <public-ip> [domain]
```

Checks, from outside the instance, the four things that otherwise fail deep inside a deploy with
an opaque message:

| Check | Why it matters |
|---|---|
| SSH auth | A key that is not **bound** to the instance gives `Permission denied` — a different fix from an unreachable host |
| Port 22 | Must be open, or nothing can be deployed |
| Ports 80 / 443 | Must be open, or HTTP and TLS issuance fail. A **timeout** means the Lighthouse firewall is blocking; a **refusal** means the firewall allows it and nothing is listening yet (normal before Nginx) |
| Port 8078 | Must **not** be reachable from the internet |
| DNS | Whether the domain already resolves here — if not, deploy over the IP and add the domain later |

It exits non-zero and prints the exact console steps to fix whatever is wrong. Run it before
`deploy.sh`, not after a failure.

---

## STEP 2 — Me: deploy everything

Tell me the public IP and I run this (or run it yourself):

```bash
cd dbs-auntiejoy
./deploy/deploy.sh <public-ip>
```

That single command:

1. verifies SSH works,
2. copies the repository up (excluding `.env`, databases and venvs),
3. installs python3-venv, nginx, apache2-utils and rsync,
4. builds the virtualenv and installs the backend dependencies,
5. seeds `backend/.env` from `.env.example` (empty of secrets — it never invents credentials),
6. installs a systemd service (**one** uvicorn worker, because session state is in-process),
7. creates the Basic Auth user and writes the Nginx vhost,
8. runs the **28-scenario suite on the instance** and prints the result,
9. prints the URL and the generated Basic Auth password.

It is idempotent — re-running it is the normal way to ship a change.

---

## STEP 3 — You: the live credentials

The provisioner deliberately leaves `.env` blank. On the instance:

```bash
ssh -i ~/.ssh/dbsjoy_lighthouse ubuntu@<public-ip>
nano /opt/auntiejoy/backend/.env        # LLM key + Tencent SecretId/SecretKey/AppID + voices
sudo systemctl restart auntiejoy
curl -s localhost:8078/health | python3 -m json.tool
```

`/health` tells you exactly what is configured — `engine: live`, `asr_configured`,
`tts_configured`, `payment_rail` — so a half-filled file is visible rather than silent.

Then open **`http://<public-ip>`** and log in with the Basic Auth user.

---

## Pointing `dbsjoy.xyz` at the instance — step by step

> **STATUS (checked 2026-10-09): `dbsjoy.xyz` is REGISTERED but FROZEN — it does not resolve.**
>
> The domain is paid up (registered 2026-10-08, expires 2027-10-08) and its DNS records are
> intact: querying DNSPod **directly** still returns `43.156.247.155`. But the registry has the
> name on **`server hold`**, so it is not delegated at the TLD and every public resolver answers
> `NXDOMAIN`. The zone data is fine; the delegation is withheld.
>
> **This is not a DNS problem, and no DNS edit can fix it.** Only the registrar (DNSPod /
> Tencent Cloud) can lift a server hold. The likely cause is an incomplete account identity
> verification — the same account's COS bucket creation returns `451 verify your identity in
> account center`. The same failure hit `dbsjoy.space` before it; two domains, one account.
>
> Until it is lifted the origin is reachable **only** as `http://43.156.247.155`, and a plain
> HTTP origin on a bare IP is **not a secure context** — browsers expose
> `navigator.mediaDevices` only on HTTPS or localhost, so the microphone (and with it the
> voice-first half of the demo) cannot run there. The steps below are the reproducible record
> for restoring it once the hold is lifted.

> ### You do not have to pass `DOMAIN=` again — and you must not lose it by forgetting
>
> `provision.sh` rebuilds `/etc/caddy/Caddyfile` from scratch and emits a site block **only for
> `$DOMAIN`**. A plain re-deploy with `DOMAIN` unset therefore used to replace a working HTTPS
> domain config with an IP-only, HTTP-only one, and **nothing in the deploy output looked
> wrong** — the app kept serving on `127.0.0.1:8078`, while port 443 stopped answering and the
> certificate stopped being presented.
>
> `provision.sh` now **adopts the domain the existing Caddyfile already names** when `DOMAIN`
> is not passed, and says so:
>
> ```
> ==> DOMAIN not passed; keeping the domain the box already serves: dbsjoy.xyz
> ==> Writing the Caddyfile (site: dbsjoy.xyz, www.dbsjoy.xyz)
> ```
>
> So `./deploy/deploy.sh 43.156.247.155` is safe to re-run. `verify.sh` section 16 asserts this
> guard is present and runs its matching logic against a domain fixture and an IP-only fixture.
>
> If the public endpoint ever stops answering on 443 while `/health` is fine on the instance,
> check the site label first: `sudo grep -nE '^[a-z0-9:(].*\{' /etc/caddy/Caddyfile`.

**The important bit: the domain must be registered before any DNS record can be added.** A DNS
lookup returning `NXDOMAIN` means the name does not exist in the registry — a purchase first,
then a DNS setting. `.xyz` was registered on 8 Oct 2026 and delegated within minutes.

Confirm the current state at any time:

```bash
dig +short A dbsjoy.xyz        # should print 43.156.247.155
dig dbsjoy.xyz | grep status   # should be NOERROR, not NXDOMAIN
```

### 1. Register the domain

Buy the domain from any registrar. Two options that make the later steps easiest:

- **Tencent Cloud / DNSPod** — the account already in use here, so DNS stays in one console.
- **Any other registrar** — works identically; you just get a different DNS control panel.

**Check the registry status before you build on a name.** A newly registered domain should show
`client transfer prohibited` + `add period` (both normal). If it shows **`server hold`**, the
registry has removed it from the zone and it will never resolve until the registrar lifts it —
which is exactly what happened to `dbsjoy.space`. Check with:

```bash
# Ask the .xyz REGISTRY directly. rdap.org is only a redirector, and it can return an EMPTY
# body for a domain on server hold -- which reads like "no such domain" and sends you looking
# in the wrong place. The registry's own endpoint always answers for a registered name.
curl -s "https://rdap.centralnic.com/xyz/domain/YOURDOMAIN" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'])"
```

A healthy new registration shows `add period` + `client transfer prohibited` (both normal).
**`server hold` is the one that breaks resolution**, and it can only be lifted by the registrar.

### 2. Add the two A records

In the registrar's DNS panel (DNSPod: **DNS 解析** / DNS Records → **添加记录**), add:

| Type | Host / Name | Value | TTL |
|---|---|---|---|
| `A` | `@` (means the bare domain) | `43.156.247.155` | 600 (or default) |
| `A` | `www` | `43.156.247.155` | 600 (or default) |

Notes:

- **Delete any default parking record first.** Registrars often pre-create an `A` record or a
  `CNAME` pointing at a parking page, and two records for the same name conflict.
- Do **not** add a `CNAME` at `@`. A CNAME cannot coexist with the apex, and it would also break
  the certificate check.
- If the panel asks for a "record value" with a trailing dot, `43.156.247.155` needs no dot — it
  is an IP address, not a hostname.

### 3. Wait, then verify

Propagation is usually a few minutes but can take longer.

```bash
dig +short A dbsjoy.xyz        # should print 43.156.247.155
dig +short A www.dbsjoy.xyz    # should print 43.156.247.155
```

### 4. Turn on the domain and TLS

Once **both** lookups return the IP:

```bash
./deploy/preflight.sh 43.156.247.155 dbsjoy.xyz   # DNS should now PASS
DOMAIN=dbsjoy.xyz ./deploy/deploy.sh 43.156.247.155
```

`preflight.sh` refuses to pretend this is ready before it is — it reports `NXDOMAIN` distinctly
from "registered but not pointing here", because the two need different actions.

The site is already reachable on **`http://43.156.247.155`** while you sort the domain out. The
certificate cannot be issued before DNS resolves, because the CA has to verify you control the
name.

---

## Later: adding the domain and TLS

When the domain's A record points at the instance IP:

```bash
DOMAIN=dbsjoy.xyz ./deploy/deploy.sh <public-ip>
```

The same command re-provisions with the domain set. Nothing else changes — the frontend already
resolves the page's own origin on both port 80 and port 443, so no code edit is needed.

**If the instance runs Caddy, TLS is automatic.** `provision.sh` prefers Caddy when it is already
installed and running, and a domain block makes Caddy fetch and renew a Let's Encrypt certificate
by itself — no certbot, no `--redirect` step, no renewal cron. Requirements are only that DNS
resolves here and ports 80/443 are reachable. If the instance has no Caddy, nginx + certbot is
used instead. Either way the same command applies.

> **Web server choice is automatic and exclusive.** Caddy and nginx cannot both hold port 80, so
> `provision.sh` detects and uses exactly one. If Caddy is present it is used and the stock
> config is preserved at `/etc/caddy/Caddyfile.orig`.

---

## If you change your mind and want the instance created for you

`deploy/create_lighthouse.py` can do it via the API instead of the console. It needs a CAM key
(Console → Access Management → API Keys, with `QcloudLighthouseFullAccess`; use a sub-user key,
not the root key):

```bash
export TENCENTCLOUD_SECRET_ID=...
export TENCENTCLOUD_SECRET_KEY=...
VENV="$HOME/.workbuddy-ai/binaries/python/envs/default"
"$VENV/bin/python" deploy/create_lighthouse.py --dry-run   # API pre-check, creates nothing
"$VENV/bin/python" deploy/create_lighthouse.py             # creates it, prints the IP
```

It imports the SSH key, creates one instance with the key as the only login method, opens
22/80/443, sets manual renewal, and uses a `ClientToken` so a retried call cannot buy a second
instance. It does **not** buy a domain or touch DNS.

---

## Flags

| Command | Effect |
|---|---|
| `./deploy/preflight.sh <ip> [domain]` | Check SSH, firewall and DNS before deploying |
| `./deploy/verify.sh` | 14-check readiness gate (no network, no cloud) |
| `./deploy/deploy.sh <ip>` | Deploy over HTTP on the IP (your current path) |
| `DOMAIN=x.com ./deploy/deploy.sh <ip>` | Deploy + issue TLS |
| `BASIC_PASS=... ./deploy/deploy.sh <ip>` | Set the Basic Auth password instead of generating one |
| `RUN_SELFTEST=0 ./deploy/deploy.sh <ip>` | Skip the scenario suite on the instance |
| `ENABLE_TLS=0 DOMAIN=x.com ...` | Set a domain but skip certbot (DNS not live yet) |

---

## Why this is API/SSH driven and not a console login

I will not log into your console with the password that was shared in chat:

1. It was sent in plain text — rotate it and enable 2FA before the demo. If you already
   rotated it, the old one is dead anyway.
2. Console logins from a new device normally require email/SMS/2FA, which I cannot complete,
   so the attempt would strand halfway through.
3. The remaining actions are purchases. Those should be an explicit act by the account owner.

An API key, if you choose that route later, is the better answer: scoped, revocable, auditable,
and your password stays out of it entirely.
