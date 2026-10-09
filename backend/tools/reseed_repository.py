"""Detect and repair drift between data.py and the trusted repository on disk.

WHY THIS EXISTS
---------------
`Repository.seed()` inserts with `INSERT OR IGNORE` on the natural key. That is
deliberate and correct: it makes a restart free, so it can neither duplicate nor
clobber a row. But it has a consequence that is easy to miss --

    **a row that already exists is NEVER updated, even when data.py changes.**

So a deployment that ships a new seed leaves every pre-existing row exactly as it
was. The app starts, `/health` is green, and the whole regression suite passes
(because the suite seeds a fresh temporary database). The only thing that breaks
is the one feature that reads the new field, which looks like a broken feature
rather than a stale row.

The live symptom that prompted this script: the seed adds the nicknames
`"ah girl"` and `"阿美"` to Mei Tan. On an instance whose `auntiejoy_repo.db`
predates that change, "Ah Girl" is not recognised as a saved payee and never
reaches the ASR hotword list, so the voice pipeline looks like it cannot hear
names when it is really just reading old rows.

WHAT IT TOUCHES, AND WHAT IT MUST NEVER TOUCH
---------------------------------------------
It opens the TRUSTED REPOSITORY only (`Settings.REPO_DB_PATH`,
`auntiejoy_repo.db`). It refuses to run if that path resolves to the audit
database: the audit trail is the append-only, hash-chained compliance record, and
rebuilding it would destroy evidence that cannot be recreated. The rail journal
(`AUNTIEJOY_RAIL_DB`) is likewise left alone.

Reseeding the repository is safe here because nothing in the running app writes to
it. `Repository` exposes `seed()`/`reseed()` and read-only getters, and there is no
endpoint that adds a payee or edits an account, so every row is seed-derived. If
the repository ever starts holding operator- or customer-entered data, `--apply`
must stop being automatic and become a reviewed migration.

USAGE (from backend/)
    # report drift only; exit 0 = in step, exit 1 = drifted (usable in a gate)
    PYTHONPATH="$PWD" .venv/bin/python -B tools/reseed_repository.py --check

    # rebuild every row from data.py and verify the result
    PYTHONPATH="$PWD" .venv/bin/python -B tools/reseed_repository.py --apply

    # point at a specific database
    ... tools/reseed_repository.py --check --db /tmp/other_repo.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Dict, Iterator, List, Tuple

try:
    from app import data as seed_data
    from app.config import Settings
except ImportError:  # pragma: no cover - a usage error, not a logic error
    sys.stderr.write(
        "Cannot import the app package.\n"
        "Run this from the backend/ directory with PYTHONPATH set, e.g.\n"
        "  PYTHONPATH=\"$PWD\" .venv/bin/python -B tools/reseed_repository.py --check\n"
    )
    raise SystemExit(2)


# Every row data.py says should exist, as (table, primary_key, {column: value}).
# The column names and the int()/json.dumps() coercions mirror Repository.seed()
# exactly, because a drift report is only useful if it compares the same way the
# writer writes. Keep the two in step: if seed() gains a column, add it here.
def expected_rows() -> Iterator[Tuple[str, str, Dict[str, object]]]:
    for customer_id, record in seed_data.CUSTOMERS.items():
        yield "customers", customer_id, {
            "display_name": record["display_name"],
            "preferred_name": record["preferred_name"],
            "tier": record["tier"],
            "language_preference": record["language_preference"],
        }
        for position, account in enumerate(record.get("accounts", [])):
            yield "accounts", account.account_id, {
                "customer_id": customer_id,
                "display_name": account.display_name,
                "masked": account.masked,
                "eligible": int(account.eligible),
                "position": position,
            }

    for customer_id, payees in seed_data.SAVED_PAYEES.items():
        for position, payee in enumerate(payees):
            yield "payees", payee.beneficiary_id, {
                "customer_id": customer_id,
                "display_name": payee.display_name,
                "masked": payee.masked,
                "destination_token": payee.destination_token,
                "verified_saved_payee": int(payee.verified_saved_payee),
                "untrusted_note": payee.untrusted_note,
                "nicknames": json.dumps(payee.nicknames),
                "position": position,
            }

    for customer_id, billers in seed_data.BILLERS.items():
        for position, biller in enumerate(billers):
            yield "billers", biller.biller_id, {
                "customer_id": customer_id,
                "display_name": biller.display_name,
                "masked": biller.masked,
                "latest_amount_minor": biller.latest_amount_minor,
                "currency": biller.currency,
                "position": position,
            }

    for customer_id, cards in seed_data.CARDS.items():
        for position, card in enumerate(cards):
            yield "cards", card.card_id, {
                "customer_id": customer_id,
                "display_name": card.display_name,
                "masked": card.masked,
                "latest_balance_minor": card.latest_balance_minor,
                "currency": card.currency,
                "position": position,
            }

    for customer_id, accounts in seed_data.OWN_ACCOUNTS.items():
        for position, account in enumerate(accounts):
            yield "own_accounts", account.account_id, {
                "customer_id": customer_id,
                "display_name": account.display_name,
                "masked": account.masked,
                "position": position,
            }


# Which column holds the natural key, per table.
_PRIMARY_KEY = {
    "customers": "customer_id",
    "accounts": "account_id",
    "payees": "beneficiary_id",
    "billers": "biller_id",
    "cards": "card_id",
    "own_accounts": "account_id",
}


def _show(value: object) -> str:
    """Render a column value for a human, not for a diff.

    `nicknames` is stored as `json.dumps(...)`, which escapes non-ASCII by default, so a
    raw repr would print the Chinese nickname as `"\\u963f\\u7f8e"`. The comparison still
    uses the escaped form (that is what `seed()` writes, so that is what must match); only
    the printed report is decoded.
    """
    if isinstance(value, str) and value[:1] in ("[", "{"):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False)
        except ValueError:
            return repr(value)
    return repr(value)


def _compare(db_path: str) -> Tuple[List[str], int]:
    """Return (drift descriptions, rows examined).

    A missing row, a row the seed does not know about, and a row whose column
    value differs are all reported: each one means the running app is reading
    something other than what data.py declares.
    """
    drift: List[str] = []
    checked = 0

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        seen: Dict[str, set] = {}
        for table, key, columns in expected_rows():
            checked += 1
            seen.setdefault(table, set()).add(key)
            try:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE {_PRIMARY_KEY[table]} = ?", (key,)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                # A database older than the current schema: every row is missing.
                drift.append(f"{table}/{key}: cannot read ({exc})")
                continue
            if row is None:
                drift.append(f"{table}/{key}: MISSING from the database")
                continue
            for column, want in columns.items():
                got = row[column] if column in row.keys() else None
                # SQLite hands back ints for INTEGER columns; data.py may hold bools.
                if isinstance(want, int) and isinstance(got, int):
                    same = want == got
                else:
                    same = (want or None) == (got or None)
                if not same:
                    drift.append(
                        f"{table}/{key}.{column}: database={_show(got)} "
                        f"seed={_show(want)}"
                    )

        # Rows present in the database that the seed no longer declares. These are
        # what a stale database accumulates when a payee is renamed or removed.
        for table, key_column in _PRIMARY_KEY.items():
            known = seen.get(table, set())
            try:
                keys = [r[0] for r in conn.execute(f"SELECT {key_column} FROM {table}")]
            except sqlite3.OperationalError:
                continue
            for key in keys:
                if key not in known:
                    drift.append(f"{table}/{key}: EXTRA row, not in the seed")
    finally:
        conn.close()

    return drift, checked


def _guard_audit_db(db_path: str) -> None:
    """Refuse to operate on the audit database, whatever the caller passed."""
    audit = os.path.abspath(Settings.DB_PATH)
    target = os.path.abspath(db_path)
    if target == audit:
        sys.stderr.write(
            "REFUSING: that path is the AUDIT database.\n"
            "  The audit trail is the append-only hash-chained compliance record and\n"
            "  rebuilding it would destroy evidence. This tool only ever touches the\n"
            "  trusted repository (Settings.REPO_DB_PATH).\n"
        )
        raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="report drift and exit 1 if the database is stale")
    mode.add_argument("--apply", action="store_true",
                      help="rebuild every row from data.py, then verify")
    parser.add_argument("--db", default=None,
                        help="repository path (default: Settings.REPO_DB_PATH)")
    args = parser.parse_args()

    db_path = args.db or Settings.REPO_DB_PATH
    _guard_audit_db(db_path)

    if not os.path.exists(db_path):
        print(f"repository : {db_path}")
        print("state      : does not exist yet -- nothing to reseed; the app will "
              "create and seed it on first start.")
        return 0

    print(f"repository : {db_path}")
    drift, checked = _compare(db_path)

    if args.check:
        print(f"rows checked: {checked}")
        if not drift:
            print("state      : IN STEP with data.py")
            return 0
        print(f"state      : DRIFTED -- {len(drift)} difference(s)")
        for line in drift:
            print(f"  {line}")
        print("\nFix with:  PYTHONPATH=\"$PWD\" .venv/bin/python -B "
              "tools/reseed_repository.py --apply")
        return 1

    # --apply
    if drift:
        print(f"drift found : {len(drift)} difference(s)")
        for line in drift:
            print(f"  {line}")
    else:
        print("drift found : none")

    from app.repository import Repository

    Repository(db_path).reseed()
    print("reseeded    : every row rebuilt from data.py")

    drift_after, _ = _compare(db_path)
    if drift_after:
        print(f"VERIFY FAILED: {len(drift_after)} difference(s) remain after reseed")
        for line in drift_after:
            print(f"  {line}")
        return 1

    print("verified    : IN STEP with data.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
