"""Learned nicknames, the names a customer actually uses out loud.

A nickname is a REDIRECT RULE: "ah girl" -> a beneficiary. Two rules keep that safe, and both are
enforced here rather than trusted to callers:

  1. The store holds ONLY a beneficiary_id. Never a display name, a masked ending, an account
     number or a destination token. The payee record always comes from the trusted repository
     (SAVED_PAYEES), so a stale or hand-edited file cannot introduce a destination the
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
from .data import SAVED_PAYEES
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
    return next((p for p in SAVED_PAYEES.get(customer_id, [])
                 if p.beneficiary_id == beneficiary_id), None)


def seeded_index(customer_id: str) -> Dict[str, Payee]:
    """Nicknames shipped on the trusted payee records."""
    index: Dict[str, Payee] = {}
    for payee in SAVED_PAYEES.get(customer_id, []):
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


def match_in_text(customer_id: str, text: str) -> Optional[Tuple[str, Payee]]:
    """The LONGEST nickname occurring in the text, as (nickname, Payee).

    Longest-first matters: "ah girl" must win over a shorter nickname that is a substring of it,
    and \b-anchoring stops "mum" matching inside a longer word.
    """
    index_map = index(customer_id)
    lowered = (text or "").lower()
    for nickname in sorted(index_map, key=len, reverse=True):
        if re.search(rf"\b{re.escape(nickname)}\b", lowered):
            return nickname, index_map[nickname]
    return None


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
