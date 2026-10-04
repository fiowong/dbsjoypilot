"""Drive the "did you mean <payee>?" recovery end to end, one turn at a time.

WHY THIS EXISTS
---------------
The near-miss payee path spans several turns of state: a fuzzy suggestion is offered, and the
customer's ANSWER is what settles it. It has three different endings (accept / decline / unclear),
and a green `app.selftest` cannot see ANY of them, because the 29 scenarios are single-turn and
nothing there ever sends the follow-up "yes". This script replays real multi-turn conversations
through the real `run_turn` and prints, per turn, what Joy SAID and what state the session reached.

The misheard phrase is the one Tencent realtime ASR actually produced for "Mei Tan" from Singapore
("me tan", engine 16k_zh_en). See tools/asr_probe.py for how that transcript was captured.

USAGE (from backend/)
    HUNYUAN_API_KEY="" AUNTIEJOY_DB=/tmp/joy_probe.db PYTHONPATH="$PWD" .venv/bin/python -B tools/payee_suggestion_probe.py

Blank the key, exactly as app.selftest is run. With a key present every agent attempts a live call
first, and when the upstream is unreachable each one burns its retries before failing closed to the
mock, which makes a five-case run slow enough to be killed. Blanking it exercises the same
deterministic reference policy without the retry storm.

Case 3 walks all the way through the nickname flow, whose teach turn WRITES the nickname store, so
point that at a scratch file too rather than touching the real one:
    ... AUNTIEJOY_NICKNAMES=/tmp/joy_probe_nicknames.json ...

Run one case at a time when chasing a failure (the number is the `=== n.` heading):
    ... tools/payee_suggestion_probe.py 4

READING THE OUTPUT
    OK    the turn behaved the way the design says it must
    FAIL  it did not, and the reason is printed beside it
"""
import asyncio
import os
import sys

from app.config import Settings
from app.orchestrator import TurnRequest, run_turn
from app.repository import get_repository
from app.session import SESSIONS

# What the customer actually said, and what ASR made of it. "me tan" matches no saved payee, and
# the nearest one is "Mei Tan" - which is the whole point of the exercise.
MISHEARD = "send a grand to me tan"
SHORT_MISHEARD = "send fifty to me tan"

# Deliberately NOT Alice: her seeded record carries an untrusted-metadata note, so paying her is
# frozen by design (scenario DEMO5). That is a different control, and it would mask the one under
# test here.
OTHER_PAYEE = "John Smith"

FAILURES = []


def start_case(session_id):
    """A case starts from a clean session AND the shipped nickname store.

    Case 3 walks through the nickname flow, which TEACHES a nickname ("me tan" -> a payee) as part
    of the journey. A taught nickname outlives the case, so without clearing it the next case would
    resolve the same misheard phrase through the lesson instead of through the suggestion this
    script exists to test - and quietly test nothing.
    """
    SESSIONS.pop(session_id, None)
    try:
        os.remove(Settings.NICKNAMES_PATH)
    except OSError:
        pass


def check(label, ok, detail=""):
    print(f"    {'OK  ' if ok else 'FAIL'}  {label}{('  <- ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILURES.append(f"{label} {detail}".strip())


def spoken(resp):
    """Every sentence Joy actually said this turn, in order."""
    return [e["payload"].get("text", "") for e in resp.events
            if e["event_type"] == "INTERFACE_LINE"]


def keys(resp):
    return [e["payload"].get("message_key") for e in resp.events
            if e["event_type"] == "INTERFACE_LINE"]


def emitted(resp, event_type):
    return [e["payload"] for e in resp.events if e["event_type"] == event_type]


def draft_of(resp):
    return resp.session.get("draft") or {}


async def turn(session_id, text):
    resp = await run_turn(TurnRequest(session_id=session_id, text=text, via_voice=True))
    print(f'\n  > "{text}"')
    for line in spoken(resp):
        print(f'      JOY: {line}')
    print(f"      [state={resp.session['state']} outcome={resp.session['outcome']} "
          f"open={resp.session['open_clarification_field']}]")
    live = SESSIONS.get(session_id)
    if live is not None:
        print(f"      [internal: suggestion={live.pending_payee_suggestion} "
              f"nickname={live.pending_nickname} phrase={live.payee_phrase!r}]")
    prop = emitted(resp, "PAYMENT_PROPOSAL")
    if prop:
        p = prop[-1]
        print(f"      [proposal: status={p.get('status')} recipient={p.get('recipient_phrase')!r} "
              f"field={p.get('clarification_field')}]")
    return resp


async def case_accept_with_yes():
    print("\n=== 1. Accept: the customer answers the suggestion with a plain \"yes\" ===")
    sid = "probe_accept_yes"
    start_case(sid)

    r1 = await turn(sid, MISHEARD)
    check("the question names the one closest payee, not the whole list",
          "PAYEE_DID_YOU_MEAN" in keys(r1) and "Mei Tan" in " ".join(spoken(r1)))
    check("nothing binds while the question is open", draft_of(r1) == {})
    check("the session is waiting on the RECIPIENT",
          r1.session["state"] == "CLARIFICATION_REQUIRED"
          and r1.session["open_clarification_field"] == "RECIPIENT")

    r2 = await turn(sid, "yes")
    d = draft_of(r2)
    check("the confirmed payee is the one on the draft", d.get("payee_display") == "Mei Tan",
          f"got {d.get('payee_display')!r}")
    check("the amount is recovered from the committed evidence, not lost",
          d.get("amount_minor") == 100000, f"got {d.get('amount_minor')!r}")
    check("the payment is read back and ready to confirm",
          r2.session["state"] == "CONFIRMATION_READY", f"got {r2.session['state']}")
    check("the acceptance is on the audit trail",
          bool(emitted(r2, "PAYEE_SUGGESTION_ACCEPTED")))
    check("the question is closed, so it cannot leak into a later payment",
          r2.session["open_clarification_field"] is None)
    check("a spoken yes still authorises nothing", r2.session["ledger"] == "NOT_INVOKED")
    check("the misheard phrase did NOT get adopted back over the answer",
          "Mei Tan" in " ".join(spoken(r2)))


async def case_answer_with_a_different_name():
    print("\n=== 2. Accept: the customer names somebody else instead ===")
    sid = "probe_accept_name"
    start_case(sid)

    await turn(sid, SHORT_MISHEARD)
    r2 = await turn(sid, "no, John Doe")
    d = draft_of(r2)
    check("naming a different payee is a complete answer, and it is honoured",
          d.get("payee_display") == "John Doe", f"got {d.get('payee_display')!r}")
    check("the amount still comes from the evidence", d.get("amount_minor") == 5000,
          f"got {d.get('amount_minor')!r}")
    check("the payment is read back and ready to confirm",
          r2.session["state"] == "CONFIRMATION_READY", f"got {r2.session['state']}")


async def case_decline():
    print("\n=== 3. Decline: the suggestion is wrong, so the full list is asked instead ===")
    sid = "probe_decline"
    start_case(sid)

    await turn(sid, SHORT_MISHEARD)
    r2 = await turn(sid, "no")
    check("the decline is on the audit trail", bool(emitted(r2, "PAYEE_SUGGESTION_DECLINED")))
    check("nothing was bound", draft_of(r2) == {}, f"got {draft_of(r2).get('payee_display')!r}")
    check("the full saved-payee question is asked, so the customer can still answer",
          "NICKNAME_UNKNOWN" in keys(r2))
    check("the payees are named in that question",
          "Alice" in " ".join(spoken(r2)) and "John Doe" in " ".join(spoken(r2)))
    check("the session is now waiting on the nickname, not the suggestion",
          r2.session["open_clarification_field"] == "PAYEE_NICKNAME"
          and r2.session["state"] == "CLARIFICATION_REQUIRED",
          f"got {r2.session['open_clarification_field']} / {r2.session['state']}")

    r3 = await turn(sid, OTHER_PAYEE)
    check("naming the payee answers THAT question too", "NICKNAME_CONFIRM" in keys(r3))

    r4 = await turn(sid, "yes")
    check("and the payment the customer started still reaches a draft",
          draft_of(r4).get("payee_display") == OTHER_PAYEE,
          f"got {draft_of(r4).get('payee_display')!r}")


async def case_unclear():
    print("\n=== 4. Unclear: neither a name nor a yes/no, so the same question is asked again ===")
    sid = "probe_unclear"
    start_case(sid)

    await turn(sid, SHORT_MISHEARD)
    r2 = await turn(sid, "hmm, I'm not sure lah")
    check("the question is re-asked rather than guessed at", "PAYEE_DID_YOU_MEAN" in keys(r2))
    check("it is still asking about the same payee", "Mei Tan" in " ".join(spoken(r2)))
    check("nothing was bound and nothing was discarded",
          draft_of(r2) == {} and r2.session["state"] == "CLARIFICATION_REQUIRED",
          f"got state={r2.session['state']}")

    r3 = await turn(sid, "yes")
    check("the customer can still accept after the re-ask",
          draft_of(r3).get("payee_display") == "Mei Tan",
          f"got {draft_of(r3).get('payee_display')!r}")


async def case_stop_beats_the_question():
    print("\n=== 5. A stop must never be read as an answer to the question ===")
    sid = "probe_stop"
    start_case(sid)

    await turn(sid, SHORT_MISHEARD)
    r2 = await turn(sid, "cancel")
    check("the turn is a cancel", r2.session["state"] == "CANCELLED",
          f"got {r2.session['state']}")
    check("nothing was drafted", draft_of(r2) == {})

    r3 = await turn(sid, "yes")
    check("a stray yes afterwards does not resurrect the payment",
          draft_of(r3) == {} and r3.session["state"] != "CONFIRMATION_READY",
          f"got state={r3.session['state']}")


CASES = {
    "1": case_accept_with_yes,
    "2": case_answer_with_a_different_name,
    "3": case_decline,
    "4": case_unclear,
    "5": case_stop_beats_the_question,
}


async def main():
    get_repository().seed()
    wanted = [a for a in sys.argv[1:] if a in CASES] or list(CASES)
    for name in wanted:
        await CASES[name]()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"All checks passed ({len(wanted)} case(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
