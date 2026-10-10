import logging
import pyodbc
import contextlib

CONNECTION_STRING = None

def init(connection_string: str):
    """Initialize the DB module with a connection string."""
    global CONNECTION_STRING
    CONNECTION_STRING = connection_string


def create_connection():
    """Return a raw pyodbc connection using the configured connection string."""
    if not CONNECTION_STRING:
        logging.error("No CONNECTION_STRING configured for DB module.")
        return None
    try:
        conn = pyodbc.connect(CONNECTION_STRING)
        return conn
    except Exception as e:
        logging.error("Database connection failed: %s", e)
        return None


@contextlib.contextmanager
def get_connection():
    """Context manager that yields a DB connection and ensures it is closed.

    Yields None if the connection could not be established, so callers can use the
    `if conn is None: return` guard. A connection failure is reported by
    create_connection() (which returns None); a failure *inside* the with-block is a
    real error and is deliberately allowed to propagate with its original type and
    message, so a missing migration surfaces as pyodbc's "Invalid column/object name"
    rather than something generic. Callers that genuinely want to tolerate a failure
    (e.g. a table from an unapplied migration) wrap their own body in try/except.

    An exception from the body triggers an explicit `rollback()` before the close. Closing
    an autocommit-off pyodbc connection already discards the open transaction, so this is
    NOT a behaviour change -- it makes the guarantee stated here rather than inherited from
    the driver's close semantics, and it stops anyone reasoning "close rolls back for us"
    from being silently wrong if that ever changes.

    It does NOT defend against autocommit. With `autocommit=True` every statement is
    durable as it is issued and `rollback()` is a no-op, so the guarantee is gone before
    this code runs. That is why `tests/verify_e2e.py` asserts connections open
    autocommit-off, rather than this function trying to detect it per call.

    Do NOT wrap this `yield` in `except Exception` and yield again: that is illegal in a
    generator, and it was exactly the bug that lived in main.py until 3 October 2026.
    """
    conn = None
    try:
        conn = create_connection()
        if conn is None:
            yield None
            return
        yield conn
    except BaseException:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logging.debug("Rollback failed while unwinding; closing anyway.")
        raise
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
