# type: ignore
"""End-to-end verification against a DISPOSABLE database. Creates and drops its own.

Nothing here writes to the live database. The scratch name comes from a config key that is
not the one `[database]` uses, and the script refuses to run at all if the two resolve to
the same name -- see AGENTS.md section 4, which is what authorises the whole thing.

Why this exists: `check_schema_sync.py` proves `database.sql` and the migration FILES agree
with each other, which is a claim about the repo. `check_applied_migrations.py` proves the
running server matches them, which is a claim about one machine that only a human can set
up. Neither ever executes a migration, and neither had ever called the booking code that
migration 022 exists to support. Migration 019 died three times against a live server --
`dbo.sys.*`, a bare `EXEC`, a `GO` inside `BEGIN` -- and passed the static lint every time,
because code you never ran has never met a constraint it did not expect.

So this script does the third thing: build a throwaway database, run the schema through
both install paths, and then actually use it.

    Phase 1  fresh install     empty database <- database.sql
    Phase 2  upgrade path      same schema, then migrations/001..025 in order, with a probe
                               row dropped in before 022 so the backfill that had never
                               seen a row gets to see one
    Phase 3  re-runnability    the same 25 files a second time. AGENTS.md section 4 makes
                               every migration re-runnable; nothing had ever checked that
                               a second run is clean rather than merely plausible.
    Phase 4  live convergence  the scratch schema against the live one, column by column.
                               This is the claim in AGENTS.md section 7, checked rather
                               than asserted. Read-only.
    Phase 5  exercise          the real booking code pointed at the scratch database, on
                               the paths AGENTS.md calls untested
    Phase 6  teardown          drop it, always, even on failure

A note on why Phase 2 does not start from empty: it cannot. Eleven tables -- Rooms,
Reservations, Users, CustomerProfiles, Items, ItemPrices, Promotions, Discounts,
Notifications, Feedback and ValetVehicles -- are never created by any migration in the
folder, because 001-012 were written against a schema that predates the repo's versioning.
The chain has no baseline and is not a from-empty path; `database.sql` is the only such
path, which is exactly what AGENTS.md section 4 says. Running the migrations on top of a
`database.sql` build is therefore the strongest claim available, and it is the one that
matters: every file executes against a real server, against a real schema.

Run with:  python tests/verify_e2e.py
Exit code is non-zero when anything fails or when a phase cannot run. It is a script, not a
test -- nothing in the unit suite runs it, because it needs a server and because it creates
a database.
"""
import configparser
import contextlib
import csv
import io
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pyodbc

ROOT = Path(__file__).resolve().parent.parent
# Running this file puts tests/ on sys.path, not the repository root, so `import main` and
# `import db` would both fail. The root has to be on the path before either is imported.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATABASE_SQL = ROOT / 'database.sql'
MIGRATIONS_DIR = ROOT / 'migrations'
SEED_SQL = ROOT / 'tests' / 'seed_smoke_test.sql'

# Match main.py exactly. Driver 18 would default Encrypt=yes and change how the login
# behaves, so the app pins 17 and this has to pin the same one.
DRIVER = 'ODBC Driver 17 for SQL Server'

# Never a target. The scratch database is dropped at the end of the run, and dropping
# tempdb or master would take the server with it.
FORBIDDEN = {'master', 'tempdb', 'model', 'msdb'}

# An identifier cannot be a bound parameter, so the scratch name is interpolated. Anything
# that is not a plain name is refused rather than quoted, so no injection surface is opened.
SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,120}$')

# database.sql carries this and the live database does not, because no migration ever
# created it. AGENTS.md section 3 says it is a deliberate dead table that must not be wired
# up, so its absence from the live server is correct rather than drift.
EXPECTED_LIVE_DIFFERENCES = frozenset({'GuestRequests'})

problems = 0


def ok(msg):
    print('  ok    %s' % msg)


def fail(msg):
    global problems
    problems += 1
    print('  FAIL  %s' % msg)


def info(msg):
    print('        %s' % msg)


def head(msg):
    print('\n%s' % msg)


# ------------------------------------------------------------------ configuration


def load_targets():
    """Resolve the scratch name and prove it is not the live one. AGENTS.md section 4."""
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(ROOT, 'config.ini'))
    if not cfg.has_section('database'):
        raise SystemExit('config.ini has no [database] section -- nothing to verify against.')

    server = cfg.get('database', 'server', fallback='').strip()
    live = cfg.get('database', 'database', fallback='').strip()
    user = cfg.get('database', 'username', fallback='')
    password = cfg.get('database', 'password', fallback='')

    # Deliberately NOT the [database] key. A default keeps the script runnable without a
    # config edit, but the name can never be the live one -- that is checked below, not
    # assumed from the default.
    scratch = cfg.get('verify', 'verify_database', fallback='').strip() or 'hotelSystem_verify'

    if not server or not live:
        raise SystemExit('[database] needs both server and database set.')

    # Conditions 1 and 2: the live name must not appear in the scratch connection string,
    # and this must refuse rather than trust config.ini to be correct.
    if scratch.lower() == live.lower():
        raise SystemExit('Refusing to run: [verify] verify_database is the live database (%s). '
                         'Point it somewhere else.' % live)
    if scratch.lower() in FORBIDDEN:
        raise SystemExit('Refusing to run: %s is a system database.' % scratch)
    if not SAFE_NAME.match(scratch):
        raise SystemExit('Refusing to run: %r is not a plain database name.' % scratch)

    return server, live, scratch, user, password


def conn_str(server, database, user, password):
    return ('DRIVER={%s};SERVER=%s;DATABASE=%s;UID=%s;PWD=%s'
            % (DRIVER, server, database, user, password))


def connect(server, database, user, password, autocommit=False):
    return pyodbc.connect(conn_str(server, database, user, password), timeout=15,
                          autocommit=autocommit)


# ------------------------------------------------------------------ T-SQL batching


def split_batches(text):
    """Split a .sql file into batches on GO, the way SSMS does.

    GO is not T-SQL -- it is a client-side batch separator, so pyodbc will not accept a file
    containing it. The BEGIN/CASE stack matters because a GO *inside* a block ends the batch
    there and leaves the block unterminated; AGENTS.md section 4 says that killed migration
    019 twice. The lint in check_migration_sql.py uses the same technique, and BEGIN
    TRANSACTION is excluded because it opens no block and so would never let the stack empty.
    """
    batches, current, stack = [], [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            current.append(line)
            continue
        upper = stripped.upper()
        scrubbed = re.sub(r'\bBEGIN\s+TRAN(?:SACTION)?\b', ' ', upper)
        for match in re.finditer(r'\b(BEGIN|CASE)\b', scrubbed):
            stack.append(match.group(1))
        for _ in re.finditer(r'\bEND\b', scrubbed):
            if stack:
                stack.pop()
        if re.match(r'^GO\b', upper) and 'BEGIN' not in stack:
            batches.append('\n'.join(current))
            current = []
            continue
        current.append(line)
    batches.append('\n'.join(current))
    return [b for b in batches if has_sql(b)]


def has_sql(batch):
    """True if a batch contains anything executable, i.e. not just comments and blanks."""
    for line in batch.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('--'):
            return True
    return False


def strip_use(batch):
    """Drop `USE <db>` from a batch.

    All 26 SQL files in this repo hardcode `USE hotelSystem`, which is what makes them
    untargetable: run one against a scratch database and it switches away from it. The
    connection's default database is already the scratch database, so removing the statement
    is both necessary and sufficient.
    """
    kept = [ln for ln in batch.splitlines() if not re.match(r'^\s*USE\s+\S', ln)]
    return '\n'.join(kept)


def run_file(path, conn, label=None):
    """Execute one .sql file. Returns the number of batches that ran."""
    text = path.read_text(encoding='utf-8-sig')
    ran = 0
    for batch in split_batches(text):
        body = strip_use(batch)
        if not has_sql(body):
            continue
        if re.search(r'\bCREATE\s+DATABASE\b', body, re.I):
            # The script creates its own database, so the file's `IF DB_ID(...) IS NULL
            # CREATE DATABASE hotelSystem` block is both redundant and actively dangerous
            # here: left in, it would build the schema in the LIVE database instead.
            info('skipped the CREATE DATABASE block in %s' % path.name)
            continue
        try:
            conn.cursor().execute(body)
        except Exception as e:
            # Name the batch by its first executable line. The index alone is misleading,
            # because comment-only and CREATE DATABASE batches are skipped, so the count of
            # batches SENT never lines up with the count of batches in the file.
            first = next((ln.strip() for ln in body.splitlines()
                          if ln.strip() and not ln.strip().startswith('--')), '')
            raise RuntimeError('%s%s: batch %d (%s...): %s'
                               % (label or '', path.name, ran + 1, first[:60], e))
        conn.commit()
        ran += 1
    return ran


def run_sql(conn, sql, params=None):
    cur = conn.cursor()
    if params is not None:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    return cur


def one(conn, sql, params=None):
    row = run_sql(conn, sql, params).fetchone()
    return row[0] if row else None


def rows(conn, sql, params=None):
    return list(run_sql(conn, sql, params).fetchall())


def migration_files():
    return sorted(MIGRATIONS_DIR.glob('*.sql'))


# ------------------------------------------------------------------ scratch database


def drop_scratch(server, scratch, user, password):
    """Drop the scratch database if it exists. Single user, so a stale open connection from
    an interrupted run cannot block the drop.

    Autocommit is on because ALTER DATABASE ... SET SINGLE_USER cannot run inside a
    transaction, and with autocommit off pyodbc opens an implicit one for every batch.
    """
    conn = connect(server, 'master', user, password, autocommit=True)
    try:
        if one(conn, 'SELECT DB_ID(?)', (scratch,)):
            info('dropping leftover %s' % scratch)
            conn.cursor().execute(
                'IF DB_ID(?) IS NOT NULL BEGIN '
                'ALTER DATABASE [%s] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; '
                'DROP DATABASE [%s]; END' % (scratch, scratch), scratch)
    finally:
        conn.close()


def create_scratch(server, scratch, user, password):
    """Create the scratch database.

    CREATE DATABASE cannot run inside a transaction or a batch with other statements, so
    autocommit is on and the statement is sent alone. This is the same constraint
    database.sql documents at its own head.
    """
    conn = connect(server, 'master', user, password, autocommit=True)
    try:
        conn.cursor().execute('CREATE DATABASE [%s]' % scratch)
    finally:
        conn.close()


def fresh_scratch(server, scratch, user, password):
    """Drop, create, and build the schema from database.sql. Returns an open connection."""
    drop_scratch(server, scratch, user, password)
    create_scratch(server, scratch, user, password)
    ok('created %s' % scratch)
    conn = connect(server, scratch, user, password)
    ran = run_file(DATABASE_SQL, conn, label='database.sql: ')
    ok('database.sql ran %d batches' % ran)
    return conn


# ------------------------------------------------------------------ catalog inventory


def inventory(conn):
    """Everything about the schema that could differ between two builds of it: column
    type/length/precision/nullability/identity, plus primary keys and unique indexes.

    check_schema_sync.py compares columns, primary keys and UNIQUE indexes in the FILES;
    this is the same three things read back off a server that actually ran them.
    """
    cols = {}
    for name, cname, ctype, clen, prec, scale, nullable, ident in rows(conn, """
        SELECT t.name, c.name, ty.name, c.max_length, c.precision, c.scale,
               c.is_nullable, c.is_identity
        FROM sys.tables t
        JOIN sys.columns c ON c.object_id = t.object_id
        JOIN sys.types   ty ON ty.user_type_id = c.user_type_id
        WHERE t.is_ms_shipped = 0
        ORDER BY t.name, c.column_id"""):
        cols['%s.%s' % (name, cname)] = (ctype, clen, prec, scale, bool(nullable), bool(ident))

    pks = {}
    for tname, cname in rows(conn, """
        SELECT t.name, c.name
        FROM sys.tables t
        JOIN sys.indexes i ON i.object_id = t.object_id AND i.is_primary_key = 1
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = t.object_id AND c.column_id = ic.column_id
        WHERE t.is_ms_shipped = 0
        ORDER BY t.name, ic.key_ordinal"""):
        pks.setdefault(tname, []).append(cname)

    uniques = {}
    for tname, iname, cname in rows(conn, """
        SELECT t.name, i.name, c.name
        FROM sys.tables t
        JOIN sys.indexes i ON i.object_id = t.object_id AND i.is_unique = 1
                            AND i.is_primary_key = 0 AND i.has_filter = 0
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = t.object_id AND c.column_id = ic.column_id
        WHERE t.is_ms_shipped = 0
        ORDER BY t.name, i.name, ic.key_ordinal"""):
        uniques['%s/%s' % (tname, iname)] = cname

    return {'columns': cols, 'pk': pks, 'unique': uniques,
            'tables': sorted({k.split('.')[0] for k in cols})}


def diff_inventories(label_a, inv_a, label_b, inv_b, tolerate=frozenset()):
    """Report every difference between two builds, minus the ones documented as expected.

    Silent agreement is not the goal -- this used to be a claim, and claims are what this
    whole script exists to replace. `tolerate` is a flat set of keys allowed to differ; a
    table's own key also covers its columns, since they share the name prefix.
    """
    global problems
    before = problems

    def allowed(key):
        return key in tolerate or key.split('.')[0] in tolerate

    def diff(section, a, b):
        for key in sorted(set(a) | set(b)):
            if a.get(key) == b.get(key):
                continue
            if allowed(key):
                info('expected difference, %s: %s' % (section, key))
                continue
            fail('%s %s differs: %r' % (section, key, a.get(key, '<missing>')))

    diff('%s table' % label_b, {t: True for t in inv_a['tables']},
         {t: True for t in inv_b['tables']})
    diff('%s column' % label_b, inv_a['columns'], inv_b['columns'])
    diff('%s primary key' % label_b, inv_a['pk'], inv_b['pk'])
    diff('%s unique index' % label_b, inv_a['unique'], inv_b['unique'])
    return problems == before


# ------------------------------------------------------------------ phases 1-3


def phase_fresh_install(server, scratch, user, password):
    head('Phase 1  fresh install: database.sql into an empty database')
    conn = fresh_scratch(server, scratch, user, password)
    try:
        inv = inventory(conn)
        ok('%d tables built, %d columns' % (len(inv['tables']), len(inv['columns'])))
        if 'GuestRequests' in inv['tables']:
            info('GuestRequests present -- AGENTS.md section 3 calls it a deliberate dead table')
        else:
            fail('GuestRequests missing; database.sql is supposed to carry it')
        if 'ConciergeRequests' not in inv['tables']:
            fail('ConciergeRequests missing from a fresh install')
        return inv
    finally:
        conn.close()


def run_all_migrations(conn, label):
    """Run every migration in order. Raises on the first one that fails."""
    for path in migration_files():
        run_file(path, conn, label=label)
        ok('%s applied' % path.name)


def phase_upgrade_path(server, scratch, user, password):
    head('Phase 2  upgrade path: migrations/001..025 against a built schema')
    conn = fresh_scratch(server, scratch, user, password)
    try:
        probe = seed_pre_022_probe(conn)
        run_all_migrations(conn, 'run 1: ')
        check_022_backfill(conn, probe)
        return conn, inventory(conn)
    except Exception:
        conn.close()
        raise


def seed_pre_022_probe(conn):
    """Insert a stay in the shape that existed before 022: no captured rate at all.

    022 backfills Reservations.NightlyRate from the room's category rate. It had never been
    run against a row, because the live database had no reservations in it -- which is
    exactly how a backfill ships broken.

    A `database.sql` install deliberately ships with no rooms at all (docs/ONBOARDING.md:
    rooms are a staff first-run step), and migration 008's room seed only runs on the
    upgrade path, so the probe brings its own room rather than depending on either. The
    category is read from the database instead of hardcoded, so a seed change cannot
    invalidate it.
    """
    types = rows(conn, "SELECT TOP 1 RoomType, NightlyRate FROM RoomTypes "
                       "WHERE NightlyRate > 0 ORDER BY RoomType")
    if not types:
        raise RuntimeError('database.sql seeded no room type carrying a nightly rate')
    room_type, rate = types[0][0], float(types[0][1])

    room = one(conn, "SELECT TOP 1 RoomNumber FROM Rooms WHERE RoomType = ? ORDER BY RoomNumber",
               (room_type,))
    if room is None:
        room = '9901'
        run_sql(conn, "INSERT INTO Rooms (RoomNumber, RoomType, Description) VALUES (?, ?, ?)",
                (room, room_type, 'verify_e2e probe room'))
        conn.commit()
        info('no rooms exist on a fresh install, so the probe added room %s (%s)'
             % (room, room_type))

    run_sql(conn, """
        INSERT INTO Reservations
            (RoomNumber, LastName, FirstName, Floor, CheckInDate, CheckOutDate)
        VALUES (?, N'Backfill', N'Probe', 9, CAST(GETDATE() AS DATE),
                DATEADD(DAY, 2, CAST(GETDATE() AS DATE)))""", (room,))
    conn.commit()
    info('probe reservation in room %s (%s, $%.2f), inserted with no captured rate'
         % (room, room_type, rate))
    return room, rate


def check_022_backfill(conn, probe):
    room, expected = probe
    got = one(conn, 'SELECT NightlyRate FROM Reservations WHERE RoomNumber = ?', (room,))
    if got is None:
        fail('022 did not populate NightlyRate on the row that needed it')
    elif abs(float(got) - expected) > 0.005:
        fail('022 backfilled NightlyRate=%s, expected the category rate %.2f' % (got, expected))
    else:
        ok('022 backfilled NightlyRate=%.2f from the room\'s category rate' % got)


def phase_rerun(server, scratch, user, password):
    """Apply every migration a second time. AGENTS.md section 4 requires each file to be
    re-runnable, and until now that was an instruction rather than a measurement."""
    head('Phase 3  re-runnability: the same 25 files, a second time')
    conn = connect(server, scratch, user, password)
    try:
        run_all_migrations(conn, 'run 2: ')
    finally:
        conn.close()


def phase_live_convergence(server, live, user, password):
    """Compare the scratch schema against the live one. Read-only.

    AGENTS.md section 7 states that the two agree on every table, column, type,
    nullability, primary key, UNIQUE index, CHECK constraint, DEFAULT constraint, foreign key
    and secondary index. This checks the part of that which is checkable this way, against
    the server, rather than against the files.
    """
    head('Phase 4  live convergence: what database.sql built vs what the live server holds')
    scratch = None
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(ROOT, 'config.ini'))
    scratch = cfg.get('verify', 'verify_database', fallback='').strip() or 'hotelSystem_verify'

    live_conn = connect(server, live, user, password)
    try:
        live_inv = inventory(live_conn)
        info('live %s: %d tables, %d columns' % (live, len(live_inv['tables']),
                                                 len(live_inv['columns'])))
        scratch_conn = connect(server, scratch, user, password)
        try:
            scratch_inv = inventory(scratch_conn)
        finally:
            scratch_conn.close()
    finally:
        live_conn.close()

    if diff_inventories('fresh install', scratch_inv, 'live', live_inv,
                        tolerate=EXPECTED_LIVE_DIFFERENCES):
        ok('database.sql matches the live database apart from the one documented difference')


# ------------------------------------------------------------------ phase 5


_HARNESS_CONN = [None]

# reports.py writes CSVs to EXPORT_DIR, which is <repo>/exports. Pointed at a temp directory
# for the report checks so that guest data from a database about to be dropped never lands in
# the working tree, and so the repo's exports/ is not quietly seeded with scratch rows. Held
# here because Phase 6 has to remove it whether or not the phase raised.
_EXPORT_DIR = [None]


def read_csv(path):
    """Read a generated report back as (headers, list-of-dicts).

    The report layer's contract is the CSV it leaves behind, so asserting on the file is the
    point -- checking that a function returned a path would prove nothing about the rows.
    """
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames or [], list(reader)


def conn_or_none():
    """The scratch connection, for the few assertions that need raw SQL.

    Reused rather than reopened so the counts read inside a single session are the same one
    the app has just written through.
    """
    return _HARNESS_CONN[0]


def _as_date(value):
    return value.date() if hasattr(value, 'date') else value


def phase_exercise(server, scratch, user, password):
    """Point the real booking code at the scratch database and use it.

    The connection string is a module global rebuilt at import, so re-pointing it is the
    documented seam (db.init). main.py opens no connection at import, so nothing here can
    reach the live database before the override lands.
    """
    head('Phase 5  exercise the real code against %s' % scratch)
    if not SEED_SQL.exists():
        info('seed_smoke_test.sql absent; skipping')
        return

    run_file(SEED_SQL, conn_or_none(), label='seed_smoke_test.sql: ')
    ok('seed_smoke_test.sql applied')

    # seed_smoke_test.sql predates 022 and inserts straight into Reservations, so its rows
    # arrive with NightlyRate NULL -- in production the booking path writes that column and
    # the seed simply bypasses it. Re-running 022's own backfill gives the seeded stay the
    # SHAPE a real booking has, which is the only way get_captured_nightly_rate() is ever
    # asked to read a row that is not NULL.
    run_sql(conn_or_none(), """
        UPDATE r
           SET r.NightlyRate = rt.NightlyRate
          FROM dbo.Reservations r
          JOIN dbo.Rooms rm ON rm.RoomNumber = r.RoomNumber
          JOIN dbo.RoomTypes rt ON rt.RoomType = rm.RoomType
         WHERE r.NightlyRate IS NULL
           AND rt.NightlyRate IS NOT NULL
           AND rt.NightlyRate > 0""")
    conn_or_none().commit()
    info('022-shaped backfill applied: %s reservation(s) now carry a captured rate'
         % one(conn_or_none(), 'SELECT COUNT(*) FROM Reservations WHERE NightlyRate IS NOT NULL'))

    # A booking reference lives on ReservationPayments, not on Reservations, so "has no
    # booking" means "has no payment row" -- which is exactly how a front-desk stay
    # differs from a booking-desk one, and why the seed gives guest A no reference.
    stay = rows(conn_or_none(), """
        SELECT TOP 1 r.RoomNumber, r.CheckInDate, r.CheckOutDate, r.CustomerID
        FROM Reservations r
        WHERE r.CustomerID IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM ReservationPayments p
                          WHERE p.RoomNumber = r.RoomNumber)
        ORDER BY r.CheckInDate""")
    if not stay:
        info('the seed produced no in-house stay; skipping the code checks')
        return
    room, check_in, check_out, customer_id = stay[0]
    check_in, check_out = _as_date(check_in), _as_date(check_out)
    info('using stay %s / %s -> %s, customer %s' % (room, check_in, check_out, customer_id))

    import db as dbmod
    import main as app

    scratch_cs = conn_str(server, scratch, user, password)
    app.CONNECTION_STRING = scratch_cs
    app.database = scratch
    dbmod.init(scratch_cs)
    # Cached module state that would otherwise answer for a previous database, and the
    # config default, which is allowed to be off. This phase is about the code running at
    # all, so both are set to the permissive value.
    app._RESERVATIONS_CAPTURED_RATE_SUPPORT = None
    app.LOYALTY_ENABLED = True

    # --- how the app reaches the database at all -----------------------------------
    # Two probes, both regression guards for the deletion of main.py's second copy of
    # get_connection() (AGENTS.md section 7). Nothing else here would notice that copy
    # coming back, because a context manager that swallows every error still returns
    # usable connections -- it just reports nothing that went wrong.
    if app.get_connection is dbmod.get_connection:
        ok('main.get_connection is db.get_connection: one definition, not two')
    else:
        fail('main.get_connection is NOT db.get_connection. main.py must not define its '
             'own copy: the old one wrapped the yield in except Exception and re-yielded, '
             'which is illegal in a generator, so every database error surfaced as '
             '"RuntimeError: generator didn\'t stop after throw()" and named nothing.')

    # The behavioural half of the same guard, because identity alone would not catch a
    # future edit that changes db.py's copy and leaves main.py aliasing it. A statement
    # that cannot succeed raises; against a table that does not exist this is read-only,
    # and the with-block closes the connection on the way out so nothing is left open.
    try:
        with app.get_connection() as probe_conn:
            probe_conn.cursor().execute('SELECT * FROM NoSuchTable_FaultInjection')
    except pyodbc.Error as e:
        ok('a failing statement escapes as %s: %s'
           % (type(e).__name__, str(e).splitlines()[0][:60]))
    except Exception as e:
        fail('a failing statement escaped as %s, not a pyodbc error: %s. The context '
             'manager is swallowing the real exception and substituting one of its own, '
             'so a query failure reaches the console with a message that names nothing.'
             % (type(e).__name__, e))
    else:
        fail('SELECT against a table that does not exist returned without raising')

    # --- the read path 022 exists to feed -----------------------------------------
    biz = app.business_date()
    if isinstance(biz, date):
        ok('business_date() -> %s' % biz)
    else:
        fail('business_date() -> %r, expected a date' % (biz,))

    if app._reservations_have_captured_rate() is True:
        ok('Reservations.NightlyRate is present')
    else:
        fail('_reservations_have_captured_rate() is not True after 022')

    captured = app.get_captured_nightly_rate(room)
    if captured is not None:
        ok('get_captured_nightly_rate(%s) -> %s' % (room, captured))
    else:
        fail('get_captured_nightly_rate(%s) returned None for a stay with a captured rate'
             % room)

    # --- the once-only room charge, retried the way a re-ran check-out would --------
    before = one(conn_or_none(),
                 "SELECT COUNT(*) FROM Transactions WHERE RoomNumber = ? "
                 "AND ItemID IS NULL AND ChargeGroup = 'Room'", (room,))
    first = app.post_room_charge(room, check_in, check_out)
    second = app.post_room_charge(room, check_in, check_out)
    after = one(conn_or_none(),
                "SELECT COUNT(*) FROM Transactions WHERE RoomNumber = ? "
                "AND ItemID IS NULL AND ChargeGroup = 'Room'", (room,))
    if first:
        ok('post_room_charge() posted a room charge (%s)' % first)
    else:
        fail('post_room_charge() posted nothing for a real stay')
    if second is None and after == before + 1:
        ok('a second post_room_charge() was refused: the room was charged once')
    else:
        fail('a second post_room_charge() PAID AGAIN (id %s) -- room-charge rows went '
             '%s -> %s. The once-only guard does not match its own row, so a retried '
             'check-out double-charges the guest.' % (second, before, after))

    # --- loyalty: the stay award, and that a repeat does not pay twice --------------
    balance_before = app.get_points_by_customer(customer_id)
    awarded = app.award_stay_points(room, check_in, check_out, customer_id)
    repeat = app.award_stay_points(room, check_in, check_out, customer_id)
    balance_after = app.get_points_by_customer(customer_id)
    if awarded > 0:
        ok('award_stay_points() awarded %s points (balance %s -> %s)'
           % (awarded, balance_before, balance_after))
    else:
        fail('award_stay_points() awarded 0 points for a 2-night stay with a profile')
    if repeat == 0 and balance_after == balance_before + awarded:
        ok('a repeat award paid nothing: the per-stay award is idempotent')
    else:
        fail('a repeat award_stay_points() returned %s and moved the balance to %s'
             % (repeat, balance_after))

    # --- order accrual against the folio -------------------------------------------
    order_tx = rows(conn_or_none(), """
        SELECT TOP 3 ID FROM Transactions
        WHERE RoomNumber = ? AND IsBilled = 0 AND ItemID IS NOT NULL
        ORDER BY ID""", (room,))
    if order_tx:
        ids = [int(r[0]) for r in order_tx]
        gained = app.award_billed_order_points(room, ids, customer_id)
        if gained > 0:
            ok('award_billed_order_points() credited %s points across %s folio lines'
               % (gained, len(ids)))
        else:
            fail('award_billed_order_points() credited 0 across %s billed lines' % len(ids))
    else:
        info('no open folio lines in the seed; skipped the order-accrual check')

    # --- the loyalty report, and 019's room-scoped semantics ------------------------
    # BOOKING.md section 6 called this untested, and it was. Two things had to be arranged
    # first, because the obvious version of this test would pass either way:
    #
    #   * reports.py writes to <repo>/exports. That would drop guest rows from a database
    #     about to be deleted into the working tree, so EXPORT_DIR is pointed at a temp
    #     directory that Phase 6 removes.
    #   * The seed gives guest A EVERY ledger row in one room, and so do the awards above.
    #     So "this guest's whole history" and "this one stay" are the same set of rows, and
    #     a room-scoped report that wrongly filtered on the room would look identical. One
    #     ledger row is therefore placed in a second room first, which is what makes the two
    #     readings distinguishable at all.
    other_room = one(conn_or_none(),
                     "SELECT TOP 1 RoomNumber FROM Rooms WHERE RoomNumber <> ? "
                     "ORDER BY RoomNumber", (room,))
    if other_room is None:
        info('the scratch database has only one room; skipped the room-scoped report check')
    else:
        run_sql(conn_or_none(), """
            INSERT INTO dbo.LoyaltyTransactions
                (CustomerID, RoomNumber, Delta, Reason, CreatedAt, SourceID)
            VALUES (?, ?, 50, N'Stay award (prior visit, Gold)', GETDATE(),
                    'verify:cross-room')""", (customer_id, other_room))
        # Keep the account total consistent with its ledger, so the CSV does not model
        # something impossible. The assertions below are about which rows are selected, not
        # about the arithmetic.
        run_sql(conn_or_none(), "UPDATE dbo.LoyaltyAccounts SET Points = Points + 50 "
                                "WHERE CustomerID = ?", (customer_id,))
        conn_or_none().commit()

        import reports as reportsmod
        _EXPORT_DIR[0] = tempfile.mkdtemp(prefix='verify_e2e_exports_')
        reportsmod.EXPORT_DIR = _EXPORT_DIR[0]

        cross_room = str(other_room)
        scoped_room = str(room)

        def report_rows(**kwargs):
            """Run one loyalty export and read back the CSV it wrote.

            The report layer's contract is the CSV, so asserting the function returned a
            path would prove nothing about which rows it selected.
            """
            paths = reportsmod.export_loyalty_statements('csv', **kwargs)
            if not paths:
                return None, [], []
            csv_headers, csv_rows = read_csv(paths[0])
            return paths[0], csv_headers, csv_rows

        path, headers, data = report_rows(room_number=room)
        if path is None:
            fail('export_loyalty_statements(room_number=%s) wrote no CSV' % room)
        else:
            if os.path.dirname(path) == _EXPORT_DIR[0] and os.path.isfile(path):
                ok('the loyalty report wrote inside the temporary export directory')
            else:
                fail('the loyalty report wrote to %s, outside the temporary export '
                     'directory -- scratch guest rows would be left in the working tree'
                     % path)

            if 'EarnedInRoom' in headers and 'Points' in headers:
                ok('the loyalty statement carries the guest ledger columns')
            else:
                fail('loyalty statement headers are %r, expected EarnedInRoom and Points'
                     % (headers,))

            # The assertion that carries the section. A room-scoped report is meant to
            # report the GUEST's whole history, not the one stay that was asked about; that
            # is the entire content of migration 019. Filtering the ledger on the room would
            # still produce a plausible-looking CSV naming the right guest.
            if any(r.get('EarnedInRoom') == cross_room for r in data):
                ok('a room-scoped statement for room %s also reports the same guest history '
                   'earned in room %s (019: keyed on the guest, not the stay)'
                   % (scoped_room, cross_room))
            else:
                fail('a room-scoped statement for room %s did NOT include the same guests '
                     'ledger rows from room %s. It has been filtered by room, which is the '
                     'fragment 019 replaced: a balance would read as only the part earned '
                     'where you happened to ask about it.'
                     % (scoped_room, cross_room))

            if any(r.get('EarnedInRoom') == scoped_room for r in data):
                ok('the room-scoped statement includes the room it was asked about too')
            else:
                fail('a room-scoped statement for room %s contains no row for that room'
                     % scoped_room)

            # The same guest, asked for by id rather than by room.
            _, _, by_id = report_rows(customer=str(customer_id))
            if any(r.get('EarnedInRoom') == cross_room for r in by_id):
                ok('a customer-scoped statement reports the same whole history')
            else:
                fail('a customer-scoped statement for customer %s did not include the '
                     'ledger rows earned in room %s' % (customer_id, cross_room))

            # And unscoped: every account, so the join must not collapse to one guest.
            _, _, everyone = report_rows()
            if everyone:
                ok('the unscoped loyalty statement exported %s rows' % len(everyone))
            else:
                fail('the unscoped loyalty statement exported no rows, though the seed '
                     'creates a loyalty account')

    # --- availability must not offer a room that is occupied -----------------------
    room_type = app.get_room_type(room)
    free = app.search_availability(check_in, check_out, room_type=room_type, limit=500)
    if free is None:
        fail('search_availability() returned None')
    else:
        offered = [str(r[0]) for r in free]
        if str(room) in offered:
            fail('search_availability() offered room %s while it is occupied %s -> %s'
                 % (room, check_in, check_out))
        else:
            ok('search_availability() did not offer the occupied room %s (%d offered)'
               % (room, len(free)))

    # --- the guest's own identity (019) and the booking reference (021) -------------
    resolved = app.customer_id_for_stay(room, check_in)
    if resolved == customer_id:
        ok('customer_id_for_stay(%s) -> %s' % (room, resolved))
    else:
        fail('customer_id_for_stay(%s) -> %r, expected %s' % (room, resolved, customer_id))

    taken_ref = one(conn_or_none(), "SELECT TOP 1 BookingRef FROM ReservationPayments "
                                    "WHERE Kind IN ('Deposit', 'Prepayment')")
    if taken_ref:
        # `conn=` is what both production callers actually pass -- _write_booking_charge()
        # (main.py:1756) and cancel_booking() (main.py:2771) -- so this is the shipping
        # path. Omitting it was what made this assertion red: it exercised the conn=None
        # branch, which no code in main.py reaches, through main.get_connection()'s broken
        # re-yield. BookingRefTaken never stopped working; the test was asking a question
        # about a path the app does not take.
        try:
            written = app.record_booking_payment(room, taken_ref, check_out, 'deposit', 10.0,
                                                 conn=conn_or_none())
        except app.BookingRefTaken:
            ok('record_booking_payment() raised BookingRefTaken on a reference in use')
        except Exception as e:
            fail('record_booking_payment() raised %s, expected BookingRefTaken: %s'
                 % (type(e).__name__, e))
        else:
            # The unique index fires before the row is written, so this cannot have both
            # inserted and refused. What it can do is swallow the violation and answer
            # None, which leaves a caller unable to tell a taken reference from any other
            # failed write.
            if written is None:
                fail('record_booking_payment() returned None instead of raising '
                     'BookingRefTaken, on the conn= path both production callers use: '
                     'the unique-index violation reached the except in the caller and '
                     '_is_duplicate_key_error() did not recognise it.')
            else:
                fail('record_booking_payment() accepted %s as PaymentID %s, which another '
                     'booking already holds' % (taken_ref, written))
        # A constraint violation does not abort the transaction with XACT_ABORT off, which
        # is the same assumption _write_booking_charge()'s retry loop relies on. Commit so
        # the scratch connection is not left holding the failed statement's transaction.
        run_sql(conn_or_none(), 'SELECT 1')
        conn_or_none().commit()
    else:
        info('no booked stay in the seed; skipped the booking-reference collision check')

    # --- the pure money helpers the booking desk is built on -----------------------
    # Shapes rather than figures: these are exhaustively unit-tested in
    # tests/test_booking.py. What is added here is that they still agree with the rest of
    # the app when it is running against a real server rather than a fixture.
    for label, got, want in (
            ('stay_nights counts calendar nights', app.stay_nights(check_in, check_out), 2),
            ('booking_quote totals the nights plus tax',
             app.booking_quote(2, 100.0, app.TAX_RATE)['total'], 226.00),
            ('booking_payment_options offers a deposit and pay-in-full',
             len(app.booking_payment_options(2, 100.0, app.TAX_RATE)), 2),
            ('refund_decision refunds outside the cutoff',
             app.refund_decision(10, 7, 100.0)['action'], 'refund'),
            ('refund_decision forfeits inside the cutoff',
             app.refund_decision(2, 7, 100.0)['action'], 'forfeit'),
            ('refund_decision refuses on the arrival date',
             app.refund_decision(0, 7, 100.0)['action'], 'refused'),
            ('allocate_booking_credit consumes oldest first',
             app.allocate_booking_credit([(1, 60.0), (2, 60.0)], 100.0),
             [(1, 60.0), (2, 40.0)]),
            ('settle_with_prepayment never credits past the total',
             app.settle_with_prepayment(226.0, 100.0)['balance_due'], 126.00),
            ('stay_nightly_rate prefers the captured rate',
             app.stay_nightly_rate(150.0, 180.0), 150.0),
            ('stay_nightly_rate falls back when nothing was captured',
             app.stay_nightly_rate(None, 180.0), 180.00),
    ):
        if got == want:
            ok('%s' % label)
        else:
            fail('%s -> %r, expected %r' % (label, got, want))

    # --- what a mid-settlement failure leaves behind ---------------------------------
    phase_settlement_faults(app, biz)
    phase_redemption_safety(app, biz)
    phase_rollback_safety(app)

    app._RESERVATIONS_CAPTURED_RATE_SUPPORT = None


# ------------------------------------------------------- settlement fault injection
#
# check_out() is five steps in sequence, each committing on its own connection. Nothing in
# the repo could ask what happens when one of them fails part-way: the unit suite never
# touches a database, and this harness ran every path to success. So a guest could be
# charged with the room still Occupied, keys still live, and no points awarded, with
# nothing in the app able to say so.
#
# The obvious remedy -- wrap settlement in one transaction -- does not apply here and the
# council that proposed it had not read the code. bill_room_transactions() prompts for
# point redemption (main.py:5713), and the comment at main.py:6139-6142 says the room
# charge posts first *specifically so a declined card can be retried without re-posting
# it*. A transaction spanning a console prompt would hold locks across human think-time
# and destroy that retry. So the property worth testing is not atomicity, it is
# RESUMABILITY: a settlement that is interrupted and then re-run must reach the same state
# as one that was never interrupted.
#
# Two guards make that non-vacuous, which is the whole difficulty. The injector has to
# prove it actually fired, and the interrupted state has to differ from the finished one --
# otherwise a scenario passes by never having tested anything.

# The settlement steps in the order check_out() runs them.
SETTLEMENT_STEPS = (
    'post_room_charge',
    'bill_room_transactions',
    'set_room_status',
    'revoke_active_key_cards',
    'award_stay_points',
)

# A Luhn-valid number and a future expiry, so process_credit_card() takes the success path
# and the scenarios exercise settlement rather than the card validator.
FIXTURE_CARD = '4111111111111111'
FIXTURE_EXPIRY = '12/2030'
FIXTURE_CVV = '123'


class InjectedFailure(Exception):
    """A simulated mid-settlement failure. The app never raises this itself."""


def phase_rollback_safety(app):
    """Prove the two guarantees every `with get_connection()` block rests on.

    There is no static check for either of these, which is why they belong here: both are
    claims about what the driver does at close time, and code you have not run has not
    met them. They are also load-bearing for every atomicity argument in the codebase --
    `bill_room_transactions()` relies on an exception before its commit discarding the
    invoice, and the refund retry loop in `book_room()` relies on the same thing.

    Autocommit is asserted first because it is the precondition for the other two. If a
    connection ever opens with `autocommit=True`, every statement commits as it is issued,
    `rollback()` becomes a no-op, and every "atomic because one commit at the end" argument
    in the app silently becomes false -- with nothing anywhere reporting it.
    """
    head('Phase 5d  rollback safety of get_connection()')

    conn = conn_or_none()

    # --- precondition: autocommit must be off, or nothing below means anything --------
    with get_connection_mod().get_connection() as probe:
        if probe is None:
            fail('cannot reach the scratch database; skipped the rollback checks')
            return
        if probe.autocommit:
            fail('connections are opening with autocommit=True. Every uncommitted write is '
                 'already durable, rollback() is a no-op, and no with-block in the app is '
                 'atomic any more.')
            return
    ok('connections are autocommit-off, so an uncommitted write is still discardable')

    room = str(one(conn, 'SELECT TOP 1 r.RoomNumber FROM Rooms r '
                         'WHERE NOT EXISTS (SELECT 1 FROM Transactions t '
                         'WHERE t.RoomNumber = r.RoomNumber) '
                         'ORDER BY r.RoomNumber'))
    marker = 'verify-rollback-probe'
    run_sql(conn, 'DELETE FROM KeyCards WHERE CardNumber = ?', (marker,))

    # --- an exception inside the block must discard the write -------------------------
    try:
        with get_connection_mod().get_connection() as w:
            w.cursor().execute(
                'INSERT INTO KeyCards (CardNumber, RoomNumber, LastName, FirstName, Status, '
                "IssuedAt, ExpiresAt, IssuedBy) VALUES (?, ?, 'Rollback', 'Probe', 'Active', "
                "GETDATE(), GETDATE(), 'verify')", (marker, room))
            raise InjectedFailure('after the insert, before the commit')
    except InjectedFailure:
        pass
    conn_or_none().commit()   # the probe connection is a different one; flush our view
    left = int(one(conn_or_none(), 'SELECT COUNT(*) FROM KeyCards WHERE CardNumber = ?',
                   (marker,)) or 0)
    # Clean up before reporting, not only on the happy path. A leaked marker collides with
    # the next scenario's insert and reports a confusing unique-constraint error that masks
    # the failure that actually mattered -- the same mistake as an assertion that only
    # tidies up when it passes.
    run_sql(conn_or_none(), 'DELETE FROM KeyCards WHERE CardNumber = ?', (marker,))
    if left:
        fail('an exception inside a with-block still left %d row(s) behind. Closing the '
             'connection did not discard the transaction.' % left)
    else:
        ok('an exception inside a with-block discards the uncommitted write')

    # --- and the normal path must still commit ----------------------------------------
    with get_connection_mod().get_connection() as w:
        w.cursor().execute(
            'INSERT INTO KeyCards (CardNumber, RoomNumber, LastName, FirstName, Status, '
            "IssuedAt, ExpiresAt, IssuedBy) VALUES (?, ?, 'Commit', 'Probe', 'Active', "
            "GETDATE(), GETDATE(), 'verify')", (marker, room))
        w.commit()
    kept = int(one(conn_or_none(), 'SELECT COUNT(*) FROM KeyCards WHERE CardNumber = ?',
                   (marker,)) or 0)
    run_sql(conn_or_none(), 'DELETE FROM KeyCards WHERE CardNumber = ?', (marker,))
    if kept == 1:
        ok('a committed write inside a with-block survives the close')
    else:
        fail('a committed write did not survive the close (found %d row(s)); get_connection() '
             'is discarding work it was told to keep' % kept)


def get_connection_mod():
    """The db module, imported lazily so a bad import cannot skip the earlier phases."""
    import db
    return db


def arm_step(app, name):
    """Replace `app.<name>` with a version that raises once, and return a restore handle.

    The wrapper sits OUTSIDE the original function, which matters: five of the six
    settlement helpers have their own `except Exception`, so a failure raised from inside
    one of them would be swallowed by that same helper and the step would look like it
    succeeded. Raising from the wrapper attributes the failure to the step.

    check_out() still catches it -- that is the behaviour under test, not a harness
    artefact. Its handler logs one line and returns to the menu, exactly as it would for a
    real database error, which is why a clerk would see check-out simply not finish.
    """
    original = getattr(app, name)
    probe = {'fired': False}

    def wrapper(*args, **kwargs):
        if not probe['fired']:
            probe['fired'] = True
            raise InjectedFailure(name)
        return original(*args, **kwargs)

    setattr(app, name, wrapper)

    def restore():
        setattr(app, name, original)

    probe['restore'] = restore
    return probe


def _prompt_script(room, last, first, redeem='n', points=0, card=FIXTURE_CARD):
    """Answer check-out's prompts by matching the prompt text, not by queue position.

    Matching on text means the script survives a reordering of the prompts, and -- the
    point of it -- an UNRECOGNISED prompt is recorded rather than swallowed. check_out()
    wraps everything in `except Exception`, so a router that raised would be caught and
    turned into a silent "check-out did not finish", which is indistinguishable from a
    real failure. Collected here and asserted afterwards instead.
    """
    unexpected = []

    def answer(prompt=''):
        text = str(prompt)
        lowered = text.lower()
        if 'last name' in lowered:
            return last
        if 'first name' in lowered:
            return first
        if 'room number' in lowered:
            return str(room)
        if 'redeem points' in lowered:
            return redeem
        if 'number of points' in lowered:
            return str(points)
        if 'discount code' in lowered:
            # No promotional code, so the fixture's invoice total is plain rate + tax and
            # two scenarios cannot diverge because one of them matched a seeded Discounts row.
            return 'n'
        if 'credit card number' in lowered:
            return card
        if 'expiration' in lowered:
            return FIXTURE_EXPIRY
        if 'cvv' in lowered:
            return FIXTURE_CVV
        unexpected.append(text)
        return ''

    return answer, unexpected


def _run_check_out(app, room, last, first, **script):
    """Drive the real check_out() with scripted answers and a silenced console.

    Returns the prompts it did not recognise. check_out() returns None on every path,
    success included, so the caller judges the outcome from database state -- which is
    the only thing that can tell an interrupted settlement from a finished one.
    """
    answer, unexpected = _prompt_script(room, last, first, **script)
    sink = io.StringIO()
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.CRITICAL)
    had_input = hasattr(app, 'input')
    saved_input = getattr(app, 'input', None)
    app.input = answer
    try:
        with contextlib.redirect_stdout(sink):
            app.check_out()
    finally:
        if had_input:
            app.input = saved_input
        else:
            # Back to the builtin: leaving a function in the module namespace would
            # outlive this scenario and silently answer every later prompt.
            del app.input
        root.setLevel(previous)
    return unexpected


def _reset_settlement_fixture(room, last, first, check_in, check_out, nightly_rate=100.0):
    """Rebuild one isolated in-house stay, shaped like the seed's guest A.

    Reset before every scenario, so each injection point starts from identical state.
    That is not tidiness: without it, "the resumed run matches the clean run" could be a
    coincidence of accumulated state rather than convergence. The seed's stay cannot be
    reused for this either, because Phase 5 has already posted a room charge and awarded
    points against it.

    Returns the new CustomerID.
    """
    conn = conn_or_none()
    lookup = 'SELECT CustomerID FROM CustomerProfiles WHERE LastName = ? AND FirstName = ?'

    # Children before parents: Transactions.RoomNumber and KeyCards.RoomNumber are the only
    # FKs pointing at Reservations.RoomNumber, and Loyalty* at CustomerProfiles.
    run_sql(conn, 'DELETE FROM Transactions WHERE RoomNumber = ?', (room,))
    run_sql(conn, 'DELETE FROM Invoices WHERE RoomNumber = ?', (room,))
    run_sql(conn, 'DELETE FROM KeyCards WHERE RoomNumber = ?', (room,))
    cid = one(conn, lookup, (last, first))
    if cid is not None:
        run_sql(conn, 'DELETE FROM LoyaltyTransactions WHERE CustomerID = ?', (cid,))
        run_sql(conn, 'DELETE FROM LoyaltyAccounts WHERE CustomerID = ?', (cid,))
    run_sql(conn, 'DELETE FROM Reservations WHERE RoomNumber = ?', (room,))
    if cid is not None:
        run_sql(conn, 'DELETE FROM CustomerProfiles WHERE CustomerID = ?', (cid,))

    # RoomType is pinned so the category multiplier is the same for every scenario; a
    # different category would award different stay points and the totals would not compare.
    run_sql(conn,
            "UPDATE Rooms SET Status = 'Occupied', RoomType = 'Standard' "
            'WHERE RoomNumber = ?', (room,))
    run_sql(conn,
            'INSERT INTO CustomerProfiles (LastName, FirstName, Phone) VALUES (?, ?, ?)',
            (last, first, '555-0199'))
    cid = int(one(conn, lookup, (last, first)))
    run_sql(conn,
            'INSERT INTO Reservations (RoomNumber, LastName, FirstName, Floor, CheckInDate, '
            'CheckOutDate, CustomerID, NightlyRate) VALUES (?, ?, ?, 1, ?, ?, ?, ?)',
            (room, last, first, check_in, check_out, cid, nightly_rate))
    run_sql(conn,
            'INSERT INTO LoyaltyAccounts (CustomerID, RoomNumber, Points, Tier, LastUpdated) '
            "VALUES (?, ?, 2600, 'Gold', ?)", (cid, room, check_in))
    run_sql(conn,
            'INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason, '
            "CreatedAt, SourceID) VALUES "
            "(?, ?, 1000, N'Stay award (prior visit)', DATEADD(DAY, -40, ?), ?), "
            "(?, ?, 900, N'Stay award (prior visit)', DATEADD(DAY, -20, ?), ?), "
            "(?, ?, 700, N'Room service spend', DATEADD(DAY, -1, ?), ?)",
            (cid, room, check_in, 'fixture:stay:1',
             cid, room, check_in, 'fixture:stay:2',
             cid, room, check_in, 'fixture:order:1'))
    # Two unbilled F&B lines and one paid-at-order line, which is the shape
    # bill_room_transactions() looks for: the last is consolidated onto the check-out
    # invoice rather than billed again.
    run_sql(conn,
            'INSERT INTO Transactions (RoomNumber, ItemID, Quantity, UnitPrice, Amount, '
            "CreatedAt, IsBilled, InvoiceID, PaidEarlier, Description, ChargeGroup) VALUES "
            "(?, 4, 1, 20.00, 20.00, GETDATE(), 0, NULL, 0, N'Room Service Meal', 'F&B'), "
            "(?, 8, 1, 30.00, 30.00, GETDATE(), 0, NULL, 0, N'Breakfast Buffet', 'F&B'), "
            "(?, 11, 1, 10.00, 10.00, GETDATE(), 1, NULL, 1, N'Porter / Bellhop', 'F&B')",
            (room, room, room))
    run_sql(conn,
            'INSERT INTO KeyCards (CardNumber, RoomNumber, LastName, FirstName, Status, '
            "IssuedAt, ExpiresAt, IssuedBy) VALUES (?, ?, ?, ?, 'Active', ?, ?, "
            "N'fixture')",
            ('KC-FIXTURE-%s' % room, room, last, first, check_in, check_out))
    conn.commit()
    return cid


def _settlement_state(room, last, first):
    """Everything a finished settlement should have decided, as one comparable value.

    Absolute counts rather than a diff against another run, so a regression names the
    thing that doubled instead of only saying the two runs differ.
    """
    conn = conn_or_none()
    lookup = 'SELECT CustomerID FROM CustomerProfiles WHERE LastName = ? AND FirstName = ?'
    cid = one(conn, lookup, (last, first))
    return {
        'room charges': one(conn,
                            "SELECT COUNT(*) FROM Transactions WHERE RoomNumber = ? "
                            "AND ItemID IS NULL AND ChargeGroup = 'Room'", (room,)),
        'invoices': one(conn, 'SELECT COUNT(*) FROM Invoices WHERE RoomNumber = ?', (room,)),
        'invoiced total': round(float(one(conn,
                                          'SELECT COALESCE(SUM(TotalAmount), 0) FROM Invoices '
                                          'WHERE RoomNumber = ?', (room,)) or 0.0), 2),
        'unbilled lines': one(conn, 'SELECT COUNT(*) FROM Transactions '
                                    'WHERE RoomNumber = ? AND IsBilled = 0', (room,)),
        'stay awards': one(conn, "SELECT COUNT(*) FROM LoyaltyTransactions "
                                  "WHERE CustomerID = ? AND SourceID LIKE 'stay:%'", (cid,)),
        'points': one(conn, 'SELECT Points FROM LoyaltyAccounts WHERE CustomerID = ?', (cid,)),
        'room status': one(conn, 'SELECT Status FROM Rooms WHERE RoomNumber = ?', (room,)),
        'active key cards': one(conn, "SELECT COUNT(*) FROM KeyCards WHERE RoomNumber = ? "
                                       "AND Status = 'Active'", (room,)),
    }


def _describe(state):
    return ', '.join('%s=%s' % (k, state[k]) for k in sorted(state))


def phase_settlement_faults(app, base_date):
    """Interrupt settlement at every step and require the retry to converge.

    The claim under test is that re-running check-out after a mid-settlement failure
    reaches the same state as never failing at all. It is a claim about composition --
    each helper is already idempotent individually and the harness proves two of them
    are -- so it can only be tested by failing a step and running the whole thing again.
    """
    head('Phase 5b  settlement resumability under an injected failure')

    check_in = base_date
    check_out = base_date + timedelta(days=2)
    last, first = 'Resumability', 'Test'

    room = one(conn_or_none(),
               'SELECT TOP 1 r.RoomNumber FROM Rooms r '
               'WHERE NOT EXISTS (SELECT 1 FROM Reservations x WHERE x.RoomNumber = r.RoomNumber) '
               'AND NOT EXISTS (SELECT 1 FROM Transactions t WHERE t.RoomNumber = r.RoomNumber) '
               'AND NOT EXISTS (SELECT 1 FROM Invoices i WHERE i.RoomNumber = r.RoomNumber) '
               'AND NOT EXISTS (SELECT 1 FROM KeyCards k WHERE k.RoomNumber = r.RoomNumber) '
               'ORDER BY r.RoomNumber')
    if room is None:
        info('every room is in use; skipped the settlement fault-injection checks')
        return
    room = str(room)
    info('settlement fixture room %s, %s -> %s' % (room, check_in, check_out))

    # --- the baseline: one uninterrupted settlement, which every retry must match ------
    _reset_settlement_fixture(room, last, first, check_in, check_out)
    unexpected = _run_check_out(app, room, last, first)
    if unexpected:
        fail('check_out() asked something this harness does not answer: %r. The scripted '
             'run cannot be trusted until every prompt is accounted for.' % unexpected[:3])
        return
    baseline = _settlement_state(room, last, first)
    settled = (baseline['room charges'] == 1 and baseline['invoices'] == 1
               and baseline['unbilled lines'] == 0 and baseline['stay awards'] == 1
               and baseline['room status'] == 'Dirty' and baseline['active key cards'] == 0)
    if settled:
        ok('an uninterrupted check-out settles: %s' % _describe(baseline))
    else:
        fail('an uninterrupted check-out did NOT settle, so the resumability scenarios '
             'would be comparing against a broken baseline: %s' % _describe(baseline))
        return

    # --- one scenario per step ------------------------------------------------------
    for step in SETTLEMENT_STEPS:
        _reset_settlement_fixture(room, last, first, check_in, check_out)

        probe = arm_step(app, step)
        try:
            unexpected = _run_check_out(app, room, last, first)
        finally:
            probe['restore']()
        interrupted = _settlement_state(room, last, first)

        if unexpected:
            fail('%s: check_out() asked something this harness does not answer: %r'
                 % (step, unexpected[:3]))
            continue
        if not probe['fired']:
            # The scenario proved nothing: without this, a step that quietly stopped being
            # called would report a pass.
            fail('%s was never reached, so no failure was injected there and the scenario '
                 'proved nothing' % step)
            continue
        if interrupted == baseline:
            # Also vacuous: the interruption changed nothing, so there was no partial state
            # for the retry to recover from.
            fail('injecting a failure at %s left the state identical to a finished '
                 'settlement (%s), so the interruption cannot have happened'
                 % (step, _describe(interrupted)))
            continue

        # The retry is exactly what a clerk does after a crash: run check-out again, with
        # nothing injected and nothing else restored first.
        unexpected = _run_check_out(app, room, last, first)
        if unexpected:
            fail('%s: the retry asked something this harness does not answer: %r'
                 % (step, unexpected[:3]))
            continue

        final = _settlement_state(room, last, first)
        if final == baseline:
            ok('a failure at %s leaves a state the retry recovers to exactly (%s)'
               % (step, _describe(final)))
        else:
            fail('a failure at %s left a state the retry did NOT recover from.\n'
                 '               interrupted: %s\n'
                 '               after retry:  %s\n'
                 '               uninterrupted: %s'
                 % (step, _describe(interrupted), _describe(final), _describe(baseline)))


def _fixture_customer(room, last, first):
    return one(conn_or_none(),
               'SELECT CustomerID FROM CustomerProfiles WHERE LastName = ? AND FirstName = ?',
               (last, first))


def _loyalty_reading(room, last, first):
    """The three things a redemption is allowed to change, and nothing else."""
    conn = conn_or_none()
    cid = _fixture_customer(room, last, first)
    return {
        'points': int(one(conn, 'SELECT Points FROM LoyaltyAccounts WHERE CustomerID = ?',
                           (cid,)) or 0),
        'redemption rows': int(one(conn, "SELECT COUNT(*) FROM LoyaltyTransactions "
                                         "WHERE CustomerID = ? AND Reason = 'checkout'", (cid,)) or 0),
        'invoices': int(one(conn, 'SELECT COUNT(*) FROM Invoices WHERE RoomNumber = ?',
                            (room,)) or 0),
    }


def phase_redemption_safety(app, base_date):
    """A redemption must not outlive the payment it was given for.

    Redemption used to commit before the card prompt, so a declined card cost the guest
    their points with nothing billed in exchange and no ledger row identifying the loss --
    `SourceID` was NULL. This drives the real check-out through both outcomes.
    """
    head('Phase 5c  a redemption cannot outlive the payment it was given for')

    check_in = base_date
    check_out = base_date + timedelta(days=2)
    last, first = 'Redemption', 'Safety'
    redeem = 500

    room = str(one(conn_or_none(),
                   'SELECT TOP 1 r.RoomNumber FROM Rooms r '
                   'WHERE NOT EXISTS (SELECT 1 FROM Reservations x WHERE x.RoomNumber = r.RoomNumber) '
                   'AND NOT EXISTS (SELECT 1 FROM Transactions t WHERE t.RoomNumber = r.RoomNumber) '
                   'AND NOT EXISTS (SELECT 1 FROM Invoices i WHERE i.RoomNumber = r.RoomNumber) '
                   'AND NOT EXISTS (SELECT 1 FROM KeyCards k WHERE k.RoomNumber = r.RoomNumber) '
                   'ORDER BY r.RoomNumber'))
    if room is None:
        info('every room is in use; skipped the redemption checks')
        return
    _reset_settlement_fixture(room, last, first, check_in, check_out)
    start = _loyalty_reading(room, last, first)
    info('redemption fixture room %s, starting at %s points' % (room, start['points']))

    # --- a declined card must cost the guest nothing -----------------------------------
    # '1234' fails the Luhn check, so process_credit_card() returns False.
    unexpected = _run_check_out(app, room, last, first, redeem='y', points=redeem, card='1234')
    after_decline = _loyalty_reading(room, last, first)

    if unexpected:
        fail('declined-card run asked something this harness does not answer: %r'
             % unexpected[:3])
    elif after_decline['invoices'] != 0:
        # Guards the premise: had the card gone through, this scenario would have been
        # asserting nothing about a declined card at all.
        fail('the premise failed -- the card was accepted (%d invoice(s) written), so this '
             'scenario never exercised a decline' % after_decline['invoices'])
    elif after_decline['points'] != start['points']:
        fail('a declined card took the guest\'s points: %s -> %s, with nothing billed. '
             'The guest owes the full bill AND has lost %s points.'
             % (start['points'], after_decline['points'], start['points'] - after_decline['points']))
    elif after_decline['redemption rows'] != 0:
        fail('a declined card still wrote %d redemption ledger row(s)'
             % after_decline['redemption rows'])
    else:
        ok('a declined card leaves the balance at %s points and writes no redemption row'
           % after_decline['points'])

    # --- a successful redemption deducts exactly once, and is traceable ----------------
    unexpected = _run_check_out(app, room, last, first, redeem='y', points=redeem)
    after_paid = _loyalty_reading(room, last, first)
    conn = conn_or_none()
    cid = _fixture_customer(room, last, first)
    sourceless = int(one(conn, 'SELECT COUNT(*) FROM LoyaltyTransactions WHERE CustomerID = ? '
                               "AND Reason = 'checkout' AND (SourceID IS NULL OR SourceID = '')",
                        (cid,)) or 0)
    redemption_delta = int(one(conn, "SELECT COALESCE(SUM(Delta), 0) FROM LoyaltyTransactions "
                                     "WHERE CustomerID = ? AND Reason = 'checkout'", (cid,)) or 0)
    # Check-out grants the stay award and the order award once payment lands, so the balance
    # is not simply "opening minus the redemption". Assert the invariant that actually holds
    # and covers all three at once: every point the guest now holds is accounted for by a
    # ledger row written during this scenario. The fixture's own seeded history is excluded
    # by its SourceID, since it is already baked into the opening balance.
    ledger_delta = int(one(conn, "SELECT COALESCE(SUM(Delta), 0) FROM LoyaltyTransactions "
                                 "WHERE CustomerID = ? AND (SourceID IS NULL OR "
                                 "SourceID NOT LIKE 'fixture:%')", (cid,)) or 0)
    recorded = one(conn, 'SELECT MAX(PointsRedeemed) FROM Invoices WHERE RoomNumber = ?', (room,))

    if unexpected:
        fail('successful-redemption run asked something this harness does not answer: %r'
             % unexpected[:3])
    elif redemption_delta != -redeem:
        fail('the redemption ledger row should total %d, found %d'
             % (-redeem, redemption_delta))
    elif after_paid['points'] != start['points'] + ledger_delta:
        fail('the balance is not explained by the ledger: the guest holds %s, but the '
             'opening %s plus this scenario\'s ledger rows (%+d) is %s. A balance that '
             'ledger does not explain means a mutation happened outside it.'
             % (after_paid['points'], start['points'], ledger_delta,
                start['points'] + ledger_delta))
    elif after_paid['redemption rows'] != 1:
        fail('expected exactly 1 redemption ledger row, found %s'
             % after_paid['redemption rows'])
    elif sourceless:
        fail('%d redemption ledger row(s) carry no SourceID, so the loss could not be '
             'traced or the charge detected as already made' % sourceless)
    elif recorded is None or int(recorded) != redeem:
        fail('the invoice records PointsRedeemed=%r, expected %s -- the invoice and the '
             'balance disagree' % (recorded, redeem))
    else:
        ok('a settled redemption deducts %s points once with a SourceID, the invoice agrees, '
           'and the balance matches the ledger (%+d across redemption, stay and order awards)'
           % (redeem, ledger_delta))

    # --- and a second pass cannot take them twice --------------------------------------
    unexpected = _run_check_out(app, room, last, first, redeem='y', points=redeem)
    after_second = _loyalty_reading(room, last, first)
    if unexpected:
        fail('repeat run asked something this harness does not answer: %r' % unexpected[:3])
    elif after_second['points'] != after_paid['points']:
        fail('re-running check-out took the points a second time: %s -> %s'
             % (after_paid['points'], after_second['points']))
    elif after_second['redemption rows'] != after_paid['redemption rows']:
        fail('re-running check-out wrote another redemption ledger row (%s -> %s)'
             % (after_paid['redemption rows'], after_second['redemption rows']))
    else:
        ok('re-running check-out takes the points no further (%s points, %s redemption row(s))'
           % (after_second['points'], after_second['redemption rows']))


# ------------------------------------------------------------------ main


def main():
    global problems
    try:
        server, live, scratch, user, password = load_targets()
    except SystemExit as e:
        print('cannot run: %s' % e)
        return 2

    print('verify_e2e: disposable-database verification')
    print('  live    %s on %s (read only, in Phase 4)' % (live, server))
    print('  scratch %s on %s (created and dropped by this script)' % (scratch, server))

    # Reachability is checked against master, not against the scratch database: the scratch
    # database does not exist yet, so connecting to it would fail even for a login that is
    # about to create it successfully. CREATE DATABASE authority itself is not probed here
    # -- the first phase tries it for real, and its own failure says so.
    try:
        probe = connect(server, 'master', user, password)
        one(probe, 'SELECT COUNT(*) FROM sys.databases')
        probe.close()
    except Exception as e:
        print('\n  cannot reach master on %s: %s' % (server, e))
        print('  nothing was created; %s is untouched.' % live)
        return 2

    try:
        phase_fresh_install(server, scratch, user, password)
        conn, _ = phase_upgrade_path(server, scratch, user, password)
        try:
            phase_rerun(server, scratch, user, password)
            phase_live_convergence(server, live, user, password)
            _HARNESS_CONN[0] = connect(server, scratch, user, password)
            try:
                phase_exercise(server, scratch, user, password)
            finally:
                _HARNESS_CONN[0].close()
                _HARNESS_CONN[0] = None
        finally:
            conn.close()
    except Exception as e:
        fail('%s: %s' % (type(e).__name__, e))
    finally:
        head('Phase 6  teardown')
        try:
            drop_scratch(server, scratch, user, password)
            ok('%s dropped' % scratch)
        except Exception as e:
            print('  FAIL  could not drop %s: %s' % (scratch, e))
            print('        it will be recreated by the next run')
            problems += 1
        # The report CSVs describe a guest of a database that no longer exists, so they go
        # with it. Done here rather than in the phase, because the phase may have raised.
        if _EXPORT_DIR[0] and os.path.isdir(_EXPORT_DIR[0]):
            try:
                shutil.rmtree(_EXPORT_DIR[0])
                ok('temporary report exports removed')
            except Exception as e:
                print('  FAIL  could not remove %s: %s' % (_EXPORT_DIR[0], e))
                problems += 1
            _EXPORT_DIR[0] = None

    print('\n%s' % ('-' * 62))
    if problems:
        print('%d problem(s). %s was live throughout and is untouched.' % (problems, live))
        return 1
    print('no problems. %s was live throughout and is untouched.' % live)
    return 0


if __name__ == '__main__':
    sys.exit(main())