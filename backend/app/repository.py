"""Trusted-repository access layer, the ONE place authoritative master data is read from.

Why this exists
---------------
The demo's authoritative data (who the customer is, which accounts they may pay from, who
their saved payees are, their billers, cards and own accounts) used to live as hard-coded
Python dicts in data.py. This module moves the STORAGE of that data into a real database
while keeping data.py as the seed that fills it.

Three rules that make swapping the callers a drop-in change
-----------------------------------------------------------
1. Every reader returns the SAME objects data.py handed out: the pydantic models from
   models.py (SourceAccount, Payee, Biller, Card, OwnAccount), and for a customer a plain
   dict with the same six keys. Callers change source, never shape.
2. Order is preserved. A customer's account list is ordered on purpose (the first eligible
   account is the default source when the customer names none), so every child row carries
   a position and every read orders by it.
3. Seeding is idempotent and never overwrites: INSERT OR IGNORE on the natural key. A
   restart cannot duplicate or clobber a row. Use reseed() to force a rebuild in dev.

Scope
-----
Step 2 of the database plan: SQLite only, no cloud and no USE_TENCENTDB flag yet. The
Postgres/TencentDB backend and the flag that selects it are later steps, and the method
signatures here are the contract both backends will share.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .config import Settings
from .models import SourceAccount, Payee, Biller, Card, OwnAccount
from . import data as seed_data

# Creation is idempotent, so an existing file is never rebuilt or migrated away.
# Source accounts and the customer's OWN accounts are separate tables on purpose: a source
# account carries `eligible` (it may fund a payment) and an own account does not (it is a
# destination). Merging them would put a nullable `eligible` on a joint account and invite
# treating that joint account as a funding source.
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS customers (
        customer_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        preferred_name TEXT NOT NULL,
        tier TEXT NOT NULL,
        language_preference TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        masked TEXT NOT NULL,
        eligible INTEGER NOT NULL,
        position INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS payees (
        beneficiary_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        masked TEXT NOT NULL,
        destination_token TEXT NOT NULL,
        verified_saved_payee INTEGER NOT NULL DEFAULT 1,
        untrusted_note TEXT,
        nicknames TEXT,
        position INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS billers (
        biller_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        masked TEXT NOT NULL,
        latest_amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        position INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cards (
        card_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        masked TEXT NOT NULL,
        latest_balance_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        position INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS own_accounts (
        account_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        masked TEXT NOT NULL,
        position INTEGER NOT NULL
    )""",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_accounts_customer ON accounts(customer_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_payees_customer ON payees(customer_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_billers_customer ON billers(customer_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_cards_customer ON cards(customer_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_own_accounts_customer ON own_accounts(customer_id, position)",
)

# Children before parents, so a reseed never fights a primary key.
_TABLES = ("accounts", "payees", "billers", "cards", "own_accounts", "customers")


def _json_list(raw: Optional[str]) -> List[str]:
    """A nicknames column back into a list.

    A NULL or unreadable value is an empty list, never an exception: a payee with no
    nicknames is normal, and one corrupt cell must not take the resolver down with it.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


class Repository:
    """Read access to the trusted repository, backed by SQLite."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or Settings.REPO_DB_PATH
        self.ensure_schema()
        self.seed()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """A connection for exactly one unit of work, always closed when it is done.

        SQLite connections are not shareable across threads by default and the API server
        serves requests concurrently, so one short-lived connection per call is both the
        cheap and the correct choice for a local file this small. The inner `with conn`
        commits on success and rolls back on an exception; the finally block always closes.
        """
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            for statement in _INDEXES:
                conn.execute(statement)

    def seed(self) -> None:
        """Fill in any row that is missing from data.py.

        Never overwrites an existing row, so a restart is free to call this and can neither
        duplicate nor clobber data.
        """
        with self._transaction() as conn:
            for customer_id, record in seed_data.CUSTOMERS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO customers "
                    "(customer_id, display_name, preferred_name, tier, language_preference) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (customer_id, record["display_name"], record["preferred_name"],
                     record["tier"], record["language_preference"]),
                )
                for position, account in enumerate(record.get("accounts", [])):
                    conn.execute(
                        "INSERT OR IGNORE INTO accounts "
                        "(account_id, customer_id, display_name, masked, eligible, position) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (account.account_id, customer_id, account.display_name,
                         account.masked, int(account.eligible), position),
                    )

            for customer_id, payees in seed_data.SAVED_PAYEES.items():
                for position, payee in enumerate(payees):
                    conn.execute(
                        "INSERT OR IGNORE INTO payees "
                        "(beneficiary_id, customer_id, display_name, masked, destination_token, "
                        " verified_saved_payee, untrusted_note, nicknames, position) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (payee.beneficiary_id, customer_id, payee.display_name, payee.masked,
                         payee.destination_token, int(payee.verified_saved_payee),
                         payee.untrusted_note, json.dumps(payee.nicknames), position),
                    )

            for customer_id, billers in seed_data.BILLERS.items():
                for position, biller in enumerate(billers):
                    conn.execute(
                        "INSERT OR IGNORE INTO billers "
                        "(biller_id, customer_id, display_name, masked, latest_amount_minor, "
                        " currency, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (biller.biller_id, customer_id, biller.display_name, biller.masked,
                         biller.latest_amount_minor, biller.currency, position),
                    )

            for customer_id, cards in seed_data.CARDS.items():
                for position, card in enumerate(cards):
                    conn.execute(
                        "INSERT OR IGNORE INTO cards "
                        "(card_id, customer_id, display_name, masked, latest_balance_minor, "
                        " currency, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (card.card_id, customer_id, card.display_name, card.masked,
                         card.latest_balance_minor, card.currency, position),
                    )

            for customer_id, accounts in seed_data.OWN_ACCOUNTS.items():
                for position, account in enumerate(accounts):
                    conn.execute(
                        "INSERT OR IGNORE INTO own_accounts "
                        "(account_id, customer_id, display_name, masked, position) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (account.account_id, customer_id, account.display_name,
                         account.masked, position),
                    )

    def reseed(self) -> None:
        """Drop every row and seed again.

        A development convenience: seed() alone will never update a row, so changing
        data.py needs this to take effect.
        """
        with self._transaction() as conn:
            for table in _TABLES:
                conn.execute(f"DELETE FROM {table}")
        self.seed()

    # ---- readers, each returns the shape data.py handed out ----

    def get_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
            ).fetchone()
            if row is None:
                return None
            accounts = [
                SourceAccount(
                    account_id=r["account_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                    eligible=bool(r["eligible"]),
                )
                for r in conn.execute(
                    "SELECT * FROM accounts WHERE customer_id = ? ORDER BY position",
                    (customer_id,),
                ).fetchall()
            ]
            return {
                "customer_id": row["customer_id"],
                "display_name": row["display_name"],
                "preferred_name": row["preferred_name"],
                "tier": row["tier"],
                "language_preference": row["language_preference"],
                "accounts": accounts,
            }

    def get_saved_payees(self, customer_id: str) -> List[Payee]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM payees WHERE customer_id = ? ORDER BY position", (customer_id,)
            ).fetchall()
            return [
                Payee(
                    beneficiary_id=r["beneficiary_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                    destination_token=r["destination_token"],
                    verified_saved_payee=bool(r["verified_saved_payee"]),
                    untrusted_note=r["untrusted_note"],
                    nicknames=_json_list(r["nicknames"]),
                )
                for r in rows
            ]

    def get_payee(self, customer_id: str, beneficiary_id: str) -> Optional[Payee]:
        for payee in self.get_saved_payees(customer_id):
            if payee.beneficiary_id == beneficiary_id:
                return payee
        return None

    def get_billers(self, customer_id: str) -> List[Biller]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM billers WHERE customer_id = ? ORDER BY position", (customer_id,)
            ).fetchall()
            return [
                Biller(
                    biller_id=r["biller_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                    latest_amount_minor=r["latest_amount_minor"],
                    currency=r["currency"],
                )
                for r in rows
            ]

    def get_cards(self, customer_id: str) -> List[Card]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM cards WHERE customer_id = ? ORDER BY position", (customer_id,)
            ).fetchall()
            return [
                Card(
                    card_id=r["card_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                    latest_balance_minor=r["latest_balance_minor"],
                    currency=r["currency"],
                )
                for r in rows
            ]

    def get_own_accounts(self, customer_id: str) -> List[OwnAccount]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM own_accounts WHERE customer_id = ? ORDER BY position",
                (customer_id,),
            ).fetchall()
            return [
                OwnAccount(
                    account_id=r["account_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                )
                for r in rows
            ]


    # ---- cross-customer reads ----
    #
    # Only for the "no customer in scope yet" case: the Intent classifier sees a bill or card
    # word before a session exists. Anything with a real customer_id uses the per-customer reads
    # above, so a customer never sees another customer's directory.

    def get_all_billers(self) -> List[Biller]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM billers ORDER BY customer_id, position"
            ).fetchall()
            return [
                Biller(
                    biller_id=r["biller_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                    latest_amount_minor=r["latest_amount_minor"],
                    currency=r["currency"],
                )
                for r in rows
            ]

    def get_all_cards(self) -> List[Card]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM cards ORDER BY customer_id, position"
            ).fetchall()
            return [
                Card(
                    card_id=r["card_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                    latest_balance_minor=r["latest_balance_minor"],
                    currency=r["currency"],
                )
                for r in rows
            ]

    def get_all_own_accounts(self) -> List[OwnAccount]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM own_accounts ORDER BY customer_id, position"
            ).fetchall()
            return [
                OwnAccount(
                    account_id=r["account_id"],
                    display_name=r["display_name"],
                    masked=r["masked"],
                )
                for r in rows
            ]


_repository: Optional[Repository] = None


def get_repository(path: Optional[str] = None) -> Repository:
    """The shared repository, built on first use.

    Passing a path returns a separate instance for that path (tests), and never replaces the
    shared one. Nothing is constructed at import time, so importing this module cannot
    create a database file as a side effect.
    """
    global _repository
    if path is not None:
        return Repository(path)
    if _repository is None:
        _repository = Repository()
    return _repository
