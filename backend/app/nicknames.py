"""Learned nicknames, the names a customer actually uses out loud.

A nickname is a REDIRECT RULE: "ah girl" -> a beneficiary. Two rules keep that safe, and both are
enforced here rather than trusted to callers:

  1. The store holds ONLY a beneficiary_id. Never a display name, a masked ending, an account
     number or a destination token. The payee record always comes from the trusted repository
     (the trusted repository), so a stale or hand-edited file cannot introduce a destination the
     repository does not already contain, an unknown id simply resolves to nothing.
  2. teach() refuses to bind anything that is not already a saved payee of that customer. Chat
     can express a preference; it can never create a payee or a destination.

Seeded nicknames live on the trusted Payee record (data.py); learned ones live in this JSON file
and are layered on top of it. A taught nickname wins over a seeded one: the customer's own, more
recent instruction is the better answer, and the person check on the read-back still gates it.

This file is deliberately NOT part of the audit log, it is mutable state, not an append-only
record. Every teaching is audited separately as a NICKNAME_LEARNED event by the orchestrator.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from typing import Dict, Optional, Tuple

from .config import Settings
from .repository import get_repository
from .models import Payee

_LOCK = threading.Lock()


def _path() -> str:
    return Settings.NICKNAMES_PATH


def _read() -> Dict[str, Dict[str, str]]:
    """{customer_id: {nickname: beneficiary_id}}. A missing or corrupt file is an empty store,
    a demo must never fail to start because a preference file got damaged."""
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    clean: Dict[str, Dict[str, str]] = {}
    for customer_id, mapping in raw.items():
        if isinstance(mapping, dict):
            clean[str(customer_id)] = {str(k): str(v) for k, v in mapping.items()}
    return clean


def _write(data: Dict[str, Dict[str, str]]) -> None:
    """Write via a temp file + os.replace, so a crash mid-write can never leave a half-written
    store (a truncated JSON file would otherwise lose every nickname the customer taught)."""
    path = os.path.abspath(_path())
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _payee_by_id(customer_id: str, beneficiary_id: str) -> Optional[Payee]:
    return next((p for p in get_repository().get_saved_payees(customer_id)
                 if p.beneficiary_id == beneficiary_id), None)


def seeded_index(customer_id: str) -> Dict[str, Payee]:
    """Nicknames shipped on the trusted payee records."""
    index: Dict[str, Payee] = {}
    for payee in get_repository().get_saved_payees(customer_id):
        for nickname in payee.nicknames:
            key = nickname.strip().lower()
            if key:
                index[key] = payee
    return index


def learned_index(customer_id: str) -> Dict[str, Payee]:
    """Taught nicknames, but ONLY those whose beneficiary still exists in the repository. An
    entry pointing at a deleted payee is ignored rather than resurrected as a free-form name."""
    index: Dict[str, Payee] = {}
    for nickname, beneficiary_id in _read().get(customer_id, {}).items():
        payee = _payee_by_id(customer_id, beneficiary_id)
        if payee is not None:
            index[nickname.strip().lower()] = payee
    return index


def index(customer_id: str) -> Dict[str, Payee]:
    """Every nickname this customer can use, seeded first so learned ones take precedence."""
    merged = seeded_index(customer_id)
    merged.update(learned_index(customer_id))
    return merged


def find(customer_id: str, phrase: Optional[str]) -> Optional[Payee]:
    """An exact nickname match. Returns the trusted Payee record, never a phrase."""
    key = (phrase or "").strip().lower()
    return index(customer_id).get(key) if key else None


def _nickname_pattern(nickname: str) -> str:
    """A boundary that actually works for a Mandarin nickname.

    \\b is only a boundary next to a NON-word character, and CJK characters ARE word characters
    in Unicode mode. So "\\b阿美\\b" cannot match inside "转50新元给阿美": the 给 in front of it is
    a word character, so there is no boundary there. A Mandarin nickname therefore only ever
    matched when it happened to sit next to a space or the end of the string - "send to 阿美"
    matched, "给阿美" did not, and the Mandarin turn came back with no payee at all.

    The anchor is kept exactly where it still means something, on an ASCII edge, so "mum" still
    cannot match inside a longer word. Dropping it entirely was the other option and it is worse:
    it would let "mum" fire on "mumble".
    """
    body = re.escape(nickname)
    left = r"\b" if nickname[:1].isascii() and nickname[:1].isalnum() else ""
    right = r"\b" if nickname[-1:].isascii() and nickname[-1:].isalnum() else ""
    return f"{left}{body}{right}"


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance for very short strings (nickname phrases).

    The early-out is the whole point of the cap: a single character edit can never close a gap
    of two or more characters, so once |len(a) - len(b)| > 1 we already know the answer is > 1
    and can skip the table. For our purpose (<= 1 tolerance) that is all we need to know.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 1:
        return abs(len(a) - len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


def _fuzzy_match_in_text(customer_id: str, text: str,
                         index_map: Dict[str, Payee]) -> Optional[Tuple[str, Payee]]:
    """A mis-spelling that is at most one character off a known ASCII nickname still resolves.

    Only ASCII nicknames are eligible. A Mandarin nickname is matched by _nickname_pattern, and
    CJK has no whitespace to tokenise on, so this whole-phrase window scan would never fire for it
    and we must not change its behaviour. The tolerance is deliberately exactly one edit, so a
    real but different name never resolves: "ah girll" -> "ah girl" (distance 1) matches, while
    "Ah Boy" -> "ah girl" (distance > 1) does not, and an unknown name stays unresolved.

    The scan looks at every contiguous run of text tokens the same length as the nickname's
    tokens and compares the joined phrase character-by-character, which is what lets "ah girll"
    (one stray 'l') align with "ah girl" without touching anything else.
    """
    lowered = (text or "").lower()
    tokens = re.findall(r"[a-z0-9]+", lowered)
    for nickname in sorted(index_map, key=len, reverse=True):
        if not nickname.isascii():
            continue
        nk_tokens = nickname.split()
        n = len(nk_tokens)
        if n == 0 or len(tokens) < n:
            continue
        for start in range(0, len(tokens) - n + 1):
            window = " ".join(tokens[start:start + n])
            if _edit_distance(window, nickname) <= 1:
                return nickname, index_map[nickname]
    return None


def match_in_text(customer_id: str, text: str) -> Optional[Tuple[str, Payee]]:
    """The LONGEST nickname occurring in the text, as (nickname, Payee).

    Longest-first matters: "ah girl" must win over a shorter nickname that is a substring of it,
    and \b-anchoring stops "mum" matching inside a longer word.

    If the exact pattern misses (a spoken mis-spelling), a one-character-off match against a known
    nickname is still the customer's own payee, so we fall back to that tight tolerance before
    giving up. The fallback only ever resolves to a nickname already in the index, so it can never
    invent a destination.
    """
    index_map = index(customer_id)
    lowered = (text or "").lower()
    for nickname in sorted(index_map, key=len, reverse=True):
        if re.search(_nickname_pattern(nickname), lowered):
            return nickname, index_map[nickname]
    return _fuzzy_match_in_text(customer_id, text, index_map)


def teach(customer_id: str, nickname: str, beneficiary_id: str) -> Optional[Payee]:
    """Bind a nickname to an EXISTING saved payee. Returns the payee, or None and changes nothing
    if the nickname is empty or the beneficiary is not this customer's saved payee, which is the
    whole point: chat can name a preference, it can never create a destination."""
    key = (nickname or "").strip().lower()
    if not key:
        return None
    payee = _payee_by_id(customer_id, beneficiary_id)
    if payee is None:
        return None
    with _LOCK:
        data = _read()
        data.setdefault(customer_id, {})[key] = payee.beneficiary_id
        _write(data)
    return payee


def forget(customer_id: str, nickname: str) -> bool:
    """Drop one taught nickname. Seeded nicknames live on the record and are never removed."""
    key = (nickname or "").strip().lower()
    with _LOCK:
        data = _read()
        mapping = data.get(customer_id, {})
        if key not in mapping:
            return False
        del mapping[key]
        _write(data)
    return True
