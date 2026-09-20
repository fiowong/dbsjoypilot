# AuntieJoy (draft README)

A voice-first conversational payment prototype built for the **Tencent Cloud AI Singapore
Hackathon 2026 — DBS Banking Track, Challenge 1 (DCTA: Direct Conversational Transaction Agent,
Scenario 1 — Voice-Enabled Payment and Transaction)**.

AuntieJoy lets an elderly customer ("Auntie") make a bank transfer just by talking or typing
naturally, in English, Mandarin, or Bahasa Melayu — while every payment is independently
screened, confirmed field-by-field, and signed by deterministic code, never by the LLM itself.

> **This is a hackathon prototype**, not a production banking system. All customer data,
> accounts, payees, biometric checks (FaceID), and the ledger are synthetic/mocked, clearly
> labelled as such in the UI. No LLM in this project ever holds a signing key, a payment
> gateway credential, or ledger access.

## How it works

- **The customer only ever sees one warm conversation** — type, or speak using the mic button.
- Behind that conversation, every turn passes through 4 bounded LLM agents (Guardrails, Intent
  Classifier, Payment, Independent Validation) and a 16-stage deterministic control chain that
  alone resolves the payee, checks risk, gets field-by-field confirmation, signs the payload,
  and calls the (mocked) bank ledger.
- The app's technical inspector panels — control chain, agent contracts, audit log, MECE
  failure map — show exactly what DBS's systems would see, and can be toggled off for a
  clean, full-width, customer-only view.
- 19 scripted BRD demo scenarios (the happy path plus 18 MECE failure branches, A–F) are built
  in as one-click buttons, alongside genuinely live free-text/voice input wired to the real
  backend.

## Repo layout

```
dbs-auntiejoy/
  backend/     FastAPI backend — the 4 agents + the deterministic control chain
               (see backend/README.md for the full file-by-file breakdown)
  frontend/    The chat UI + technical inspector panels (plain HTML/CSS/JS, no build step)
```

## Running it locally

**Backend** (from `backend/`):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```
This runs against a deterministic mock LLM by default. To connect a real LLM (Tencent Hunyuan
via TokenHub, or any OpenAI-compatible endpoint), copy `backend/.env.example` to `backend/.env`
and fill in your own key — see `backend/README.md` for details.

**Frontend** (from `frontend/`, in a separate terminal):
```bash
python3 -m http.server 8098
```
Then open `http://127.0.0.1:8098` in your browser. The frontend expects the backend at
`http://127.0.0.1:8077` by default.

## Tech stack

- **Backend:** Python, FastAPI, Pydantic — deterministic state machine + 4 typed-schema LLM agents
- **LLM:** Tencent Hunyuan (via TokenHub gateway), OpenAI-compatible client, with automatic
  fallback to a deterministic mock when no API key is configured
- **Frontend:** Vanilla HTML/CSS/JS (no framework, no build step)
- **Voice input:** Browser-native Web Speech API (English / Mandarin / Bahasa Melayu toggle)
- **Audit trail:** Append-only, hash-chained log (SQLite)

## Team

- **Ignatius** — business logic and frontend
- **Fionna Wong** — backend, control-chain architecture, Tencent Cloud / LLM integration

## Status

Built for hackathon submission (deadline: **16 Oct 2026**). See `backend/README.md` for
backend internals and what-to-touch-for-what.
