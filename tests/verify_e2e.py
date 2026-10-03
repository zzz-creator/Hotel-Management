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
import os
import re
import sys
from datetime import date
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
        try:
            written = app.record_booking_payment(room, taken_ref, check_out, 'deposit', 10.0)
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
                     'BookingRefTaken: the unique-index violation never reached the caller. '
                     'main.get_connection() catches it and re-yields, so the real error is '
                     'replaced by RuntimeError("generator didn\'t stop after throw()") and '
                     '_is_duplicate_key_error() cannot recognise it.')
            else:
                fail('record_booking_payment() accepted %s as PaymentID %s, which another '
                     'booking already holds' % (taken_ref, written))
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

    app._RESERVATIONS_CAPTURED_RATE_SUPPORT = None


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

    print('\n%s' % ('-' * 62))
    if problems:
        print('%d problem(s). %s was live throughout and is untouched.' % (problems, live))
        return 1
    print('no problems. %s was live throughout and is untouched.' % live)
    return 0


if __name__ == '__main__':
    sys.exit(main())