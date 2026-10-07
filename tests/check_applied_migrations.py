# type: ignore
"""Read-only check that migrations 022-025 are really applied on the live database.

Every statement in the checks section is a SELECT or a catalog read. Nothing writes.

The static checkers cannot see any of this: `check_schema_sync.py` proves `database.sql`
and the migration FILES agree with each other, which is a claim about the repo. This proves
the running server matches them. Those are different claims, and after a manual SSMS run
only one of them has ever been tested.

It also calls the real code -- the business-date reader, the captured-rate reader, and both
date reports -- because a column existing is not the same as the function that reads it
working. A fresh `database.sql` install and a migrated one can pass the catalog checks and
still disagree at runtime.

Run with:  python tests/check_applied_migrations.py
Exit code is non-zero when anything is missing or has drifted from `database.sql`.
"""
import configparser
import os
import sys
from datetime import date, datetime

import pyodbc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Columns that must exist, checked with COL_LENGTH. Migration 023 is NOT here: it adds a
# ROW to HotelSettings, not a column, and COL_LENGTH on a setting key is meaningless.
EXPECTED_COLUMNS = {
    '022': ('Reservations', 'NightlyRate'),
}

# The shape `database.sql` declares, and therefore the shape a fresh install has.
EXPECTED_SHAPE = {
    'Reservations.NightlyRate': ('decimal', 10, 2, True),
}


def connection_string():
    config = configparser.ConfigParser()
    config.read(os.path.join(ROOT, 'config.ini'))
    return (
        'DRIVER={ODBC Driver 17 for SQL Server};'
        f"SERVER={config.get('database', 'server', fallback='')};"
        f"DATABASE={config.get('database', 'database', fallback='')};"
        f"UID={config.get('database', 'username', fallback='')};"
        f"PWD={config.get('database', 'password', fallback='')}"
    )


def one(cursor, sql, params=()):
    cursor.execute(sql, params)
    return cursor.fetchone()


def main():
    try:
        conn = pyodbc.connect(connection_string())
    except Exception as e:
        print('CANNOT CONNECT: %s' % e)
        return 1
    problems = 0
    notes = []
    try:
        cursor = conn.cursor()
        # pyodbc's getinfo takes the integer SQLGetInfo attribute; the pyodbc.DBMS_NAME
        # wrapper class does not exist, so the constant is spelled out rather than
        # pretending there is a symbolic one.
        print('connected: SQL Server %s' % conn.getinfo(10))
        print()

        print('columns that must exist (COL_LENGTH):')
        for migration, (table, column) in sorted(EXPECTED_COLUMNS.items()):
            row = one(cursor, 'SELECT COL_LENGTH(?, ?)', (f'dbo.{table}', column))
            exists = bool(row and row[0])
            print('%-42s %s' % (f'{migration}: {table}.{column}',
                                'OK' if exists else 'MISSING'))
            if not exists:
                problems += 1
        print()

        print('column shape vs database.sql:')
        for column_name, expected in sorted(EXPECTED_SHAPE.items()):
            table, column = column_name.split('.')
            row = one(cursor,
                      'SELECT type_name(user_type_id), precision, scale, is_nullable '
                      'FROM sys.columns WHERE object_id = OBJECT_ID(?) AND name = ?',
                      (f'dbo.{table}', column))
            if row is None:
                print('%-42s MISSING' % column_name)
                problems += 1
                continue
            # is_nullable is a BIT, so pyodbc hands back a bool -- comparing it to the
            # string 'YES' is how a correct schema gets reported as wrong.
            actual = (row[0], int(row[1]), int(row[2]), bool(row[3]))
            ok = actual == expected
            print('%-42s %s' % (f'{column_name} type/prec/scale/nullable',
                                'OK' if ok else f'{actual} != {expected}'))
            if not ok:
                problems += 1
        print()

        print('023 business_date row (removed by 029):')
        row = one(cursor, 'SELECT SettingValue FROM dbo.HotelSettings '
                           "WHERE SettingKey = N'business_date'")
        print('%-42s %s' % ('HotelSettings.business_date',
                             'deleted (good)' if row is None else f'still present: {row[0]!r} -- apply migration 029'))
        print()

        print('024 order accrual (must be a positive fraction, not 0):')
        row = one(cursor, 'SELECT SettingValue FROM dbo.HotelSettings '
                           "WHERE SettingKey = N'loyalty_accrual_points_per_unit'")
        if row is None:
            print('%-42s MISSING -- falls back to the built-in default'
                  % 'loyalty_accrual_points_per_unit')
            problems += 1
        else:
            raw = str(row[0]).strip()
            try:
                parsed = float(raw)
            except ValueError:
                print('%-42s NOT A NUMBER: %r' % ('loyalty_accrual_points_per_unit', raw))
                problems += 1
            else:
                ok = parsed > 0
                print('%-42s %s (%r)' % ('loyalty_accrual_points_per_unit',
                                         'OK' if ok else 'ZERO -- pays nothing', raw))
                if not ok:
                    problems += 1
        print()

        print('025 loyalty_expiration_days (must be gone):')
        row = one(cursor, "SELECT COUNT(*) FROM dbo.HotelSettings "
                           "WHERE SettingKey = N'loyalty_expiration_days'")
        gone = int(row[0]) == 0 if row else False
        print('%-42s %s' % ('row deleted',
                            'OK' if gone else f'STILL PRESENT ({row[0]} row(s))'))
        if not gone:
            problems += 1
        print()

        print('022 backfill:')
        row = one(cursor, 'SELECT COUNT(*), SUM(CASE WHEN NightlyRate IS NULL THEN 1 '
                           'ELSE 0 END) FROM dbo.Reservations')
        total, nulls = int(row[0]), int(row[1] or 0)
        print('  %d live reservation row(s), %d with NULL NightlyRate' % (total, nulls))
        if total and nulls == total:
            notes.append('every live reservation has NULL NightlyRate, which is what an '
                         'unrun backfill looks like; re-apply 022 (its UPDATE is guarded)')
        if total == 0:
            notes.append('Reservations is empty, so the 022 backfill had nothing to do. '
                         'Run tests/seed_smoke_test.sql to have something to verify against.')
        print()
    finally:
        conn.close()

    # The catalog checks prove the columns exist. These prove the CODE reads them, which is
    # the claim that actually matters and the one no schema check can make.
    print('the code, against the live database:')
    import main as app
    import reports

    business = app.business_date()
    print('%-42s %s' % ('app.business_date()', business))
    if not isinstance(business, date):
        problems += 1

    if app._reservations_have_captured_rate() is not True:
        print('%-42s the probe says NightlyRate is ABSENT' % 'captured-rate probe')
        problems += 1
    else:
        print('%-42s OK' % 'captured-rate probe')

    print('%-42s informational: busDate is the wall clock as of 5 Oct 2026' % 'app.business_date()')

    # The earn-rate ordering. A fresh install and a migrated one must agree, or the
    # calibration in the settings screen means different things on each.
    order, room = app.points_per_dollar_order_vs_room()
    print('%-42s order %.4f vs room %.4f pts/$' % ('earn-rate ordering', order, room))
    if order >= room:
        print('%-42s ORDERING IS INVERTED' % '')
        problems += 1

    print()
    print('reports against the live database:')
    for label, fn, kwargs in (
        ('export_housekeeping', reports.export_housekeeping, {}),
        ('export_housekeeping (past day)',
         reports.export_housekeeping, {'on_date': '2026-03-04'}),
        ('export_occupancy (window)',
         reports.export_occupancy, {'start_date': '2026-01-01', 'end_date': '2026-12-31'}),
    ):
        try:
            paths = list(fn(**kwargs))
        except Exception as e:
            print('%-42s FAILED: %s: %s' % (label, type(e).__name__, e))
            problems += 1
            continue
        print('%-42s OK (%d file(s))' % (label, len(paths)))
    print()

    for note in notes:
        print('NOTE: %s' % note)
    if notes:
        print()

    print('problems: %d' % problems)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
