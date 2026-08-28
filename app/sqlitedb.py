"""One place that opens a SQLite connection, so every store opens it the same
way.

Nine stores (`app/store.py`, `app/authstore.py`, the three `local*store.py`,
and the rest) each persist a different slice of platform state into the same
`cash_intel.db` file. Each of them used to call `sqlite3.connect(path)` and
take the defaults, which is fine for exactly one process and starts throwing
`database is locked` the moment there are two — the default journal mode
takes a whole-file write lock, and the default busy timeout is zero, so a
reader that arrives during someone else's commit fails outright instead of
waiting the millisecond it would have taken.

That is the difference between "SQLite is the single-writer store" (true, and
deliberate — see the architecture doc) and "SQLite only works in a single
*process*" (never intended, and the thing that made a second uvicorn worker
look impossible). WAL gives concurrent readers alongside one writer, and a
busy timeout turns writer-vs-writer contention into a short wait instead of
an error. Neither changes the single-writer *design*; they make it survive
being written to from more than one process on the same host, which is what a
`web` + `worker` split (app/cluster.py) needs.

None of this makes SQLite a multi-*host* store — network filesystems break
its locking, and a scaled-out deployment across hosts wants Postgres behind
the same store classes. See docs/scaling.md.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

# Applied once per connection. `journal_mode` is persistent (it is a property
# of the database file, not the connection) but setting it every time costs a
# no-op read once it is already WAL, and means a fresh file gets it too.
_PRAGMAS = (
    # concurrent readers + one writer, instead of one whole-file lock
    "PRAGMA journal_mode = WAL",
    # wait for a busy writer rather than raising immediately. In ms; the
    # config value is seconds because every other timeout in this app is.
    f"PRAGMA busy_timeout = {int(config.SQLITE_BUSY_TIMEOUT * 1000)}",
    # fsync at checkpoints rather than every commit. Safe under WAL against
    # process crash (the failure mode this app actually has); the window it
    # opens is OS-level crash, against a store whose contents are platform
    # metadata, not the analyzed data — which lives in the bucket.
    "PRAGMA synchronous = NORMAL",
)

# Deliberately NOT set here: `PRAGMA foreign_keys = ON`. Several schemas
# declare `REFERENCES users(id)` on columns that are documentation rather than
# constraints — an audit row deliberately outlives the account that wrote it,
# and `record_audit` is called with ids the users table need not still hold.
# SQLite leaves foreign keys off by default, so those declarations have never
# been enforced and the code around them was written accordingly. Turning them
# on here would be a semantic change smuggled in under a connection helper,
# and it has nothing to do with what this module is for.


def connect(db_path: Path) -> sqlite3.Connection:
    """A configured connection to `db_path`, with `sqlite3.Row` rows.

    Short-lived by convention: every store opens one per operation and closes
    it with the `with` block, which is what keeps a slow request from holding
    a write lock across an S3 round trip."""
    conn = sqlite3.connect(db_path, timeout=config.SQLITE_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            # A pragma a future/older sqlite spells differently is not worth
            # failing a connection over — the defaults still work, just with
            # the contention behaviour this module exists to improve.
            pass
    return conn
