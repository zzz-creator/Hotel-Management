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
def get_connection(isolation="read_committed"):
    """Context manager that yields a DB connection and ensures it is closed.

    `isolation` selects the transaction isolation level: "read_committed" (default)
    or "serializable". A serializable connection issues
    `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` on open. Autocommit stays off.
    """
    conn = None
    try:
        conn = create_connection()
        if conn is None:
            yield None
            return
        if isolation not in ("read_committed", "serializable"):
            raise ValueError("Unknown isolation level: %r" % isolation)
        if isolation == "serializable":
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
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


def claim_row(cursor, sql, params=()):
    """Execute a claim-style statement and return the affected row count.

    The caller supplies the full SQL (including any `WITH (UPDLOCK, HOLDLOCK)`
    hint and the `WHERE <guard>` predicate) and the parameters. A return value
    of 1 means this caller won the race; 0 means someone else already claimed it.
    """
    cursor.execute(sql, params)
    return cursor.rowcount
