# type: ignore
"""Compare database.sql against the migrations, column by column.

Checks the column SET, the SQL TYPE, and NULL/NOT NULL, so a fresh install from
database.sql matches a database built by applying the migrations in order. Also checks the
PRIMARY KEY of any table a migration re-keys.

Migrations 013-018 CREATE their tables, so they are checked by reading their CREATE
statements. Migrations 019, 020 and 022 instead ALTER tables that already exist, so they
are checked by reading their ADD/ALTER COLUMN statements -- see ALTER_MIGRATIONS below. A
CREATE-TABLE-only pass silently skips those, which is how a re-keyed PK or a later-added
column drifts out of sync with database.sql unnoticed. 022 is the case that motivated
this list: it adds Reservations.NightlyRate, so a fresh install that had the column
missed would not bill a captured rate at all.

Not checked: indexes, foreign keys, CHECK and DEFAULT constraints. Those are real schema
surface too, but they are not machine-comparable at this level of effort; the migrations
are written to be individually idempotent, so a fresh database.sql run is the source of
truth for them.

Run with:  python tests/check_schema_sync.py
Exit code is non-zero when anything is out of sync.
"""
import glob
import re
import sys

# Migrations that modify an existing table instead of creating one. Each is checked by its
# ADD / ALTER COLUMN / PRIMARY KEY statements rather than a CREATE TABLE. Add a new
# number here when a migration ALTERs rather than CREATEs.
ALTER_MIGRATIONS = ('019', '020', '022', '026', '028')

# Order matters: longer type names must come before names that prefix them
# (DATETIME2/DATETIME before DATE, TIME last), or "DATETIME" parses as "DATE".
TYPE_RE = (r'(?:DATETIME2|DATETIME|SMALLDATETIME|NVARCHAR|VARCHAR|NCHAR|CHAR|'
           r'DECIMAL|NUMERIC|UNIQUEIDENTIFIER|SMALLMONEY|BIGINT|SMALLINT|'
           r'REAL|FLOAT|MONEY|BIT|DATE|TIME|INT)')
KEYWORDS = {'CONSTRAINT', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK', 'INDEX', 'IDENTITY',
            'DEFAULT', 'COLLATE', 'REFERENCES'}


def norm_type(raw):
    """Normalise a declared type so varchar(50) and NVARCHAR( 50 ) compare equal."""
    raw = raw.lower().replace(' ', '')
    return re.sub(r'\(|\)', '', raw)


def parse_migration_tables():
    tables = {}
    for f in sorted(glob.glob('migrations/01[3-8]_*.sql')):
        sql = open(f, encoding='utf-8').read()
        for m in re.finditer(
                r'CREATE TABLE (?:IF NOT EXISTS )?(?:dbo\.)?\[?(\w+)\]?\s*\(\s*(.*?)\n\s*\);',
                sql, re.S | re.I):
            table, body = m.group(1), m.group(2)
            cols = {}
            for line in body.splitlines():
                line = line.strip().rstrip(',')
                if not line or re.match(r'(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|INDEX)\b', line, re.I):
                    continue
                cm = re.match(r'(\w+)\s+(' + TYPE_RE + r')\s*(\(\s*[\d,\s]+\s*\))?\s*(.*)$',
                              line, re.I)
                if not cm or cm.group(1).upper() in KEYWORDS:
                    continue
                name, base, args, rest = cm.groups()
                typ = norm_type(base + (args or ''))
                nullable = 'NOT NULL' not in rest.upper()
                cols[name] = (typ, nullable)
            tables[table] = cols
    return tables


def parse_database_sql():
    dsql = open('sql/database.sql', encoding='utf-8').read()
    tables = {}
    for m in re.finditer(r'CREATE TABLE \[dbo\]\.\[(\w+)\]\(\s*(.*?)\n\) ON', dsql, re.S):
        table, body = m.group(1), m.group(2)
        cols = {}
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith('['):
                continue
            cm = re.match(r'\[(\w+)\]\s+\[(' + TYPE_RE + r')\](\(\s*[\d,\s]+\s*\))?\s*(.*?),\s*$',
                          line, re.I)
            if not cm:
                continue
            name, base, args, rest = cm.groups()
            typ = norm_type(base + (args or ''))
            nullable = 'NOT NULL' not in (rest or '').upper()
            cols[name] = (typ, nullable)
        tables[table] = cols
    return tables


def parse_migration_alters(migration):
    """Extract the columns and primary keys one ALTER-style migration adds or rewrites.

    Migrations 019, 020 and 022 cannot be checked by the CREATE TABLE pass above, because
    they re-key or extend EXISTING tables with ALTER statements -- exactly the part a
    fresh install from database.sql has to match, and exactly the part a naive comparison
    would miss. Returns (adds, alters, pks) where each is
    {table: {column: (type, nullable)}} / {table: pk}.
    """
    adds, alters, pks = {}, {}, {}
    path = glob.glob('migrations/%s_*.sql' % migration)
    if not path:
        return adds, alters, pks
    flat = ' '.join(open(path[0], encoding='utf-8').read().split())

    type_pat = r'(' + TYPE_RE + r')\s*(\(\s*[\d,\s]+\s*\))?'
    for m in re.finditer(r'ALTER TABLE (?:dbo\.)?(\w+) ADD (\w+) ' + type_pat +
                         r'\s*(NOT NULL|NULL)?', flat, re.I):
        table, name = m.group(1), m.group(2)
        if name.upper() in KEYWORDS:
            continue
        nullable = not (m.group(5) or '').upper() == 'NOT NULL'
        adds.setdefault(table, {})[name] = (norm_type(m.group(3) + (m.group(4) or '')), nullable)

    for m in re.finditer(r'ALTER TABLE (?:dbo\.)?(\w+) ALTER COLUMN (\w+) ' + type_pat +
                         r'\s*(NOT NULL|NULL)?', flat, re.I):
        table, name = m.group(1), m.group(2)
        nullable = not (m.group(5) or '').upper() == 'NOT NULL'
        alters.setdefault(table, {})[name] = (norm_type(m.group(3) + (m.group(4) or '')), nullable)

    for m in re.finditer(r'ALTER TABLE (?:dbo\.)?(\w+)\s*ADD CONSTRAINT (\w+) '
                         r'PRIMARY KEY(?: CLUSTERED)?\s*\(\s*\[?(\w+)\]?\s*\)', flat, re.I):
        pks[m.group(1)] = m.group(3)

    return adds, alters, pks


def database_sql_primary_keys():
    """Read each table's PRIMARY KEY column(s) out of database.sql."""
    dsql = ' '.join(open('sql/database.sql', encoding='utf-8').read().split())
    pks = {}
    for m in re.finditer(r'CREATE TABLE \[dbo\]\.\[(\w+)\].*?PRIMARY KEY CLUSTERED '
                         r'\(\s*\[?(\w+)\]?', dsql, re.I):
        pks.setdefault(m.group(1), m.group(2))
    return pks


def check_migration_alters(dbs, migration):
    """Compare one ALTER-style migration's ADD/ALTER/PK statements against database.sql."""
    problems = 0
    adds, alters, pks = parse_migration_alters(migration)
    dbs_pks = database_sql_primary_keys()

    for table in sorted(set(adds) | set(alters)):
        # alters() must WIN over adds(): 019 adds LoyaltyAccounts.CustomerID as NULL and
        # then promotes it to NOT NULL, so the ALTER is the column's final shape.
        cols = dict(adds.get(table, {}))
        cols.update(alters.get(table, {}))
        db_cols = dbs.get(table)
        if db_cols is None:
            print('MISSING TABLE in database.sql: %s' % table)
            problems += 1
            continue
        issues = []
        for name, spec in sorted(cols.items()):
            if name not in db_cols:
                issues.append('missing column %s' % name)
            elif db_cols[name] != spec:
                issues.append('%s: migration %s %s%s vs database.sql %s%s'
                              % (name, migration, spec[0], '' if spec[1] else ' NOT NULL',
                                 db_cols[name][0], '' if db_cols[name][1] else ' NOT NULL'))
        if issues:
            problems += 1
            print('%s (migration %s):' % (table, migration))
            for i in issues:
                print('    %s' % i)
        else:
            print('%-22s OK (migration %s, %d column(s))' % (table, migration, len(cols)))

    for table, pk in sorted(pks.items()):
        actual = dbs_pks.get(table)
        if actual is None:
            print('MISSING TABLE in database.sql: %s' % table)
            problems += 1
        elif actual.lower() != pk.lower():
            problems += 1
            print('%s: migration %s makes %s the PRIMARY KEY, database.sql has %s'
                  % (table, migration, pk, actual))

    return problems


def alter_added_columns():
    """{table: {column}} contributed by the ALTER-style migrations.

    A CREATE-TABLE pass compares the columns a migration created against database.sql and
    reports anything database.sql has that the CREATE did not as an "extra column". A
    column a LATER migration adds by ALTER is not extra, it is simply added after the
    CREATE -- 020's ReservationPayments.AppliedAmount is exactly that. Those columns are
    subtracted from the extra set here and verified by check_migration_alters() instead.
    """
    added = {}
    for migration in ALTER_MIGRATIONS:
        adds, alters, _pks = parse_migration_alters(migration)
        for source in (adds, alters):
            for table, cols in source.items():
                added.setdefault(table, set()).update(cols)
    return added


def _filter_literals(text):
    """The string literals in a WHERE fragment, sorted.

    A filtered index's predicate cannot be compared as text: the migration says
    ``Kind IN ('Deposit', 'Prepayment')`` and database.sql, written by SSMS, says
    ``([Kind]='Deposit' OR [Kind]='Prepayment')``. Those are the same index. Comparing the
    SET of literals is the part that carries meaning -- which values the index covers --
    without being brittle about the rest of the syntax.
    """
    return tuple(sorted(re.findall(r"'([^']*)'", text)))


def _index_columns(raw):
    """Column names from an index column list, ignoring SSMS's sort direction.

    database.sql (SSMS-generated) writes ``([Email] ASC)`` where a migration writes
    ``(Email)``, so each entry has its brackets stripped and a trailing ASC/DESC dropped.
    Without this the bracket and direction are compared as part of the column name and every
    index looks like a mismatch.
    """
    columns = []
    for part in raw.split(','):
        # Remove every bracket, not just leading/trailing ones: '[Email] ASC' has its
        # closing bracket in the middle, which str.strip('[]') cannot reach.
        name = re.sub(r'[\[\]]', '', part).strip()
        name = re.sub(r'\s+(ASC|DESC)$', '', name, flags=re.I).strip()
        if name:
            columns.append(name)
    return tuple(columns)


def parse_unique_indexes(sql, bracketed_prefix):
    """{index_name: (table, columns, is_filtered, filter_literals)} per UNIQUE index.

    `bracketed_prefix` selects how the table is qualified: database.sql is SSMS-generated
    and writes `[dbo].[Table]`, while a migration writes `dbo.Table`. Both are accepted
    either way, since the optional group matches nothing when the style does not fit.
    Non-unique indexes are skipped: they carry no correctness guarantee, so a mismatch there
    is a performance note rather than a bug.

    Statements are split on `;` and `GO` before matching, because a filtered index's
    predicate ends at a statement boundary (`... 'Prepayment'); END GO`) and a single
    flattened regex would either stop too early or run into the next statement.
    """
    prefix = r'(?:\[dbo\]|dbo)\.' if bracketed_prefix else r'(?:dbo|\[dbo\])\.'
    found = {}
    for statement in re.split(r'[;]|\bGO\b', sql):
        statement = ' '.join(statement.split())
        m = re.search(r'CREATE UNIQUE (?:NONCLUSTERED |CLUSTERED )?INDEX \[?(\w+)\]? ON ' +
                      prefix + r'\[?(\w+)\]?\s*\(([^)]*)\)(?:\s*WHERE\s*(.*))?$',
                      statement, re.I)
        if not m:
            continue
        name, table, cols, where = m.group(1), m.group(2), m.group(3), m.group(4)
        found[name] = (table, _index_columns(cols), bool(where and where.strip()),
                       _filter_literals(where or ''))
    return found


def check_unique_indexes():
    """Every UNIQUE index a migration creates must exist in database.sql, unfiltered apart.

    A filtered unique index is how 021 makes a booking reference unique, and how 019 keeps
    one NULL Email from blocking a second name-only walk-in profile. Both are correctness
    guarantees that live or die with the index, and a unique index silently missing from
    database.sql would let a fresh install behave differently from a migrated one -- the
    exact class of drift this script exists to catch.
    """
    problems = 0
    expected = {}
    for path in sorted(glob.glob('migrations/*.sql')):
        for name, spec in parse_unique_indexes(open(path, encoding='utf-8').read(), False).items():
            expected[name] = (spec, path)
    actual = parse_unique_indexes(open('sql/database.sql', encoding='utf-8').read(), True)

    for name in sorted(expected):
        spec, path = expected[name]
        table, columns, is_filtered, literals = spec
        migration = re.search(r'(\d{3})', path).group(1)
        if name not in actual:
            problems += 1
            print('MISSING UNIQUE INDEX in database.sql: %s (migration %s, %s)'
                  % (name, migration, table))
            continue
        got_table, got_columns, got_filtered, got_literals = actual[name]
        issues = []
        if got_table.lower() != table.lower():
            issues.append('on %s, migration says %s' % (got_table, table))
        if tuple(c.lower() for c in got_columns) != tuple(c.lower() for c in columns):
            issues.append('columns %s, migration says %s' % (got_columns, columns))
        # Filtered-ness is compared separately from the literals because a predicate like
        # `Email IS NOT NULL` contains no literals at all. Dropping the filter would not
        # show up in a literal comparison, yet it is exactly what lets a second name-only
        # profile insert alongside a NULL Email.
        if got_filtered != is_filtered:
            issues.append('database.sql is %sfiltered, migration says %sfiltered'
                          % ('' if got_filtered else 'NOT ', '' if is_filtered else 'NOT '))
        elif got_literals != literals:
            issues.append('filter %s, migration says %s'
                          % (list(got_literals) or 'none', list(literals) or 'none'))
        if issues:
            problems += 1
            print('%s (migration %s):' % (name, migration))
            for i in issues:
                print('    %s' % i)
        else:
            print('%-42s OK (unique, %s%s)'
                  % (name, table, ', filtered %s' % list(literals) if literals
                     else ', filtered' if is_filtered else ''))

    for name in sorted(set(actual) - set(expected)):
        print('NOTE: %s is unique in database.sql but in no migration' % name)

    return problems


def main():
    mig = parse_migration_tables()
    dbs = parse_database_sql()
    later = alter_added_columns()
    problems = 0

    for table in sorted(mig):
        mc, dc = mig[table], dbs.get(table)
        if dc is None:
            print('MISSING TABLE in database.sql: %s' % table)
            problems += 1
            continue
        issues = []
        for name, spec in sorted(mc.items()):
            if name not in dc:
                issues.append('missing column %s' % name)
            elif dc[name] != spec:
                issues.append('%s: migration %s%s vs database.sql %s%s'
                              % (name, spec[0], '' if spec[1] else ' NOT NULL',
                                 dc[name][0], '' if dc[name][1] else ' NOT NULL'))
        for name in sorted(set(dc) - set(mc) - later.get(table, set())):
            issues.append('extra column %s (%s)' % (name, dc[name][0]))
        if issues:
            problems += 1
            print('%s:' % table)
            for i in issues:
                print('    %s' % i)
        else:
            print('%-22s OK (%d columns)' % (table, len(mc)))

    print()
    for migration in ALTER_MIGRATIONS:
        problems += check_migration_alters(dbs, migration)
    problems += check_unique_indexes()

    compared = len(mig)
    for migration in ALTER_MIGRATIONS:
        adds, alters, _pks = parse_migration_alters(migration)
        compared += len(adds) + len(alters)
    print()
    print('tables compared: %d | tables with issues: %d' % (compared, problems))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
