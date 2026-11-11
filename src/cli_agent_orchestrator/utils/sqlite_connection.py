"""One transaction on one ``sqlite3`` connection, and the connection closed after it.

``with sqlite3.connect(path) as conn:`` reads as if it closes the connection, but a
``sqlite3.Connection`` used as a context manager only commits (or rolls back) on exit.
The connection stays open until it is garbage collected, and since it sits in a
reference cycle with its own statement cache, that is the next full cyclic collection,
not the end of the block. Every such block therefore held a file descriptor on the
database until a full GC happened to run.

:func:`closing_transaction` keeps the exact semantics of that ``with`` block (commit on
a normal exit, roll back on an exception, isolation level and pragmas as the caller set
them on the connection) and then always closes the connection.

Standard library only, so every module that talks to SQLite directly can import it
without an import cycle.
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def closing_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Yield ``conn`` inside ``with conn:``, then close it whatever happened.

    Pass the connection straight from the call that opened it, so nothing can fail
    between the open and this block::

        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute(...)
    """
    try:
        with conn:
            yield conn
    finally:
        conn.close()
