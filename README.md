# JoyPilot

Our entry for the **Tencent Cloud AI Singapore Hackathon 2026, DBS Banking Track**:
the Direct Conversational Transaction Agent (DCTA) challenge, Scenario 1 (voice-enabled
payments). The assistant inside JoyPilot is called Joy. (The repo and some settings still use
the earlier working name, AuntieJoy.)

Joy lets an older customer ("Auntie") pay someone by talking or typing normally, in English,
Mandarin or Bahasa Melayu. "Send fifty dollars to Ah Girl" is enough. The AI only drafts the
payment. Plain code checks it, the customer confirms each field and approves with (mock)
FaceID, and only then is the payment signed and recorded.

> This is a hackathon prototype, not a banking system. The customer, accounts, payees,
> FaceID and the ledger are all synthetic, and the app says so on screen. No AI model in this
> project holds a signing key, a gateway credential or access to the ledger.

## How it works

Every turn goes through five agents and a 16-stage control chain.

**The five agents.** Four of them sit in the payment control chain. Each makes one decision
and returns JSON that must match a strict schema:

| Agent | Stage | What it does |
|---|---|---|
| Guardrails Agent | 3 | Screens the turn for scams, prompt injection and requests Joy must refuse |
| Intent Classifier | 4 | Works out what the customer wants: a payment, a change, a cancel, help |
| Payment Agent | 5 | Drafts the payment (payee, amount, currency, source account) or asks one question |
| Validation Agent | 8 | Checks each field of the draft against what the customer actually said |

The fifth, the **Personalization Agent**, runs outside the chain. It only sees counts of what
happened in the session (the same field rejected twice, the same question asked twice, two
unclear turns in a row). When the customer seems to be struggling, it switches on a slower
mode where Joy confirms each field twice. It never sees the conversation and cannot change a
payment.

**The control chain.** Everything outside the four chain agents is deterministic code:

- payee and account lookup from the customer's saved records (an agent only ever proposes a name)
- a check of the Validation Agent's answer (stage 9)
- risk rules: a S$10,000 demo limit, an extra FaceID check above S$5,000, a limit of five
  drafts per session
- field-by-field confirmation, then mock FaceID
- an HMAC-SHA256 signature over the final payload (a demo key, not a bank HSM)
- a gateway that rebuilds the payload and runs 14 checks: signature, draft version, confirmation,
  validation, risk policy, freeze and cancel status, nonce reuse, expiry and more
- the mock ledger (`Ledger.execute()`), which is the only place a payment is recorded

Connecting a real bank means replacing that one ledger call with DBS's payment API. That
integration is not built.

**Scam protection.** On top of the Guardrails Agent there is a table of known scam types in
`backend/app/scam_patterns.py`: government impersonation, secrecy instructions, coaching,
requests to remove limits, a family emergency from a new number, guaranteed investments,
romance scams, tech support scams, urgency pressure, splitting payments to avoid checks,
hiding a payment, and a caller who is still on the line. Each has cue words in all three
languages and its own reply. The table can only make the Guardrails verdict stricter, never
looser.

**Understanding speech.** The parser reads amounts said as words in all three languages
("one hundred and fifty", 一百五十, "seratus lima puluh"). When an amount really can mean two
things ("one fifty" is S$1.50 or S$150), Joy asks. Two payments in one sentence are read back
and confirmed one at a time. If a name matches nobody in the customer's saved payees (often a
misheard name), Joy asks who was meant instead of guessing.

**Audit trail.** Every step is written to an append-only, hash-chained log in SQLite, and the
receipt screen checks that chain.

## The app

- The customer sees one conversation. They can type, or press the mic and speak.
- **Technical panels** show the control chain, each agent's output and the audit log live.
  Turn them off for a clean, customer-only view.
- **Architecture**, **How it works** and **Execution path** open explainer pages for judges.
- **Voice mode** shows the extra identity check for a voice that cannot be verified.
- **TRTC Mode** switches Joy's voice from Cartesia to Tencent Cloud TTS.

## Repo layout

```
backend/    FastAPI app: the agents, the control chain, the scam table, ASR and TTS
  app/        the code (orchestrator.py runs a turn, controls.py holds the chain's controls)
  tools/      small probes for the Tencent ASR handshake, hotwords and engines
frontend/   the chat UI and the technical panels (plain HTML, CSS and JS, no build step)
deploy/     scripts to deploy to a Tencent Cloud Lighthouse server (see deploy/README.md)
```

## Running it locally

**Backend**, from `backend/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8078
```

With no `.env` it runs fully offline: the agents use their deterministic mocks, and the mic
and Tencent voice stay off. To turn things on, copy `backend/.env.example` to `backend/.env`
and fill in what you need. The file only lists what you have to fill in, plus a few optional
settings; the full list is in `backend/app/config.py`:

- **LLM:** `HUNYUAN_API_KEY`, `AUNTIEJOY_LLM_BASE` and `AUNTIEJOY_LLM_MODEL`. Any
  OpenAI-compatible endpoint works, such as Tencent TokenHub. With a key set, each agent calls
  the model, and falls back to its mock on any error. The agent cards show `engine: live` or
  `engine: mock`.
- **Speech to text:** `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY` and
  `TENCENTCLOUD_APPID` for Tencent Cloud realtime ASR.
- **Text to speech:** `CARTESIA_API_KEY` and voice IDs for Cartesia (the default, and the only
  engine with a Malay voice). Tencent TTS uses the same Tencent keys; TRTC streaming also
  needs `TRTC_SDK_APP_ID`.

Live mode is slower than offline mode, because each turn waits for several model calls.

**Frontend**, from `frontend/` in a second terminal:

```bash
python3 serve.py
```

Then open `http://127.0.0.1:8098`. Use `serve.py` rather than `python3 -m http.server`: it
turns off browser caching, so you always see the files on disk.

The page looks for the backend on port 8078 of whatever host served it. If your backend is
somewhere else, add it to the URL, for example `http://127.0.0.1:8098/?api=http://127.0.0.1:8077`.
The "LIVE BACKEND" line in the audit panel shows which backend the page is using.

The mic only works on `localhost` or over HTTPS. Browsers block it on a plain `http://` address.

## Voice

- **Speech to text:** Tencent Cloud realtime ASR over a signed WebSocket. No single Tencent
  engine covers all three languages, so English and Mandarin use `16k_zh_en` and Malay uses
  `16k_multi_lang`. Joy's payee names and nicknames are sent as hotwords, and spoken numbers
  are kept as words. If Tencent is not configured, the browser's own speech recognition is
  used instead.
- **Text to speech:** Cartesia Sonic by default. TRTC Mode switches to Tencent Cloud TTS
  (TextToVoice, or TRTC streaming when configured). If one engine fails, the other answers
  and the top bar says so.

## Tests

From `backend/`:

```bash
python -m app.selftest --selftest
```

With no LLM key this runs offline in a few seconds. It should report 100/100 scenarios
(28 acceptance scenarios and 72 regression tests), plus the parser, schedule, personalization
and repository checks. With `HUNYUAN_API_KEY` set the same suite runs against the live model,
which takes a few minutes.

The frontend tests are in `frontend/*.test.js` and need `jsdom`
(`NODE_PATH=<folder with node_modules> node frontend/shell.test.js`).
`node frontend/test-api-base.mjs` needs nothing extra.

`deploy/verify.sh` runs all of the above and is the check to run before a deploy.

## Deploying

The scripts in `deploy/` set up a Tencent Cloud Lighthouse server in ap-singapore: a Python
virtual environment, a systemd service for the backend and Caddy (or nginx) in front. See
`deploy/README.md` for the full steps. In short, from the repo root:

```bash
DOMAIN=<your-domain> ./deploy/deploy.sh <server-ip>
```

With `DOMAIN` set, Caddy gets an HTTPS certificate automatically, which the mic needs. If
you have no domain yet, `<ip-with-dashes>.sslip.io` (for example `43-156-247-155.sslip.io`)
points at the server and works as one. Ports 80 and 443 must be open in the Lighthouse
firewall. After the first deploy, fill in `.env` on the server and restart the service.

## Tech stack

- **Backend:** Python, FastAPI, Pydantic
- **LLM:** any OpenAI-compatible endpoint (we use Tencent Cloud TokenHub), temperature 0
- **Speech:** Tencent Cloud ASR, Cartesia Sonic and Tencent Cloud TTS / TRTC
- **Frontend:** plain HTML, CSS and JavaScript
- **Storage:** SQLite for the audit log and the synthetic customer records
- **Hosting:** Tencent Cloud Lighthouse

## Team

Iggy Aditya & Fionna Wong
