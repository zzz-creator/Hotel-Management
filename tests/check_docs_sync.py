# type: ignore
"""Check that the documentation has not drifted from the code.

AGENTS.md tells an assistant to read docs/SCHEMA.md and docs/BOOKING.md before touching
almost anything. That instruction is only worth anything if those files are correct, and a
reference that rots quietly is worse than no reference: it looks authoritative while
describing a function that was renamed three migrations ago. This script is the thing that
notices.

Four checks, each chosen to have no false positives, because a doc checker that cries wolf
gets ignored:

  1. Every file in migrations/ appears in the docs/SCHEMA.md inventory table.
  2. Every function named in backticks anywhere in the docs is still defined in the code.
  3. Every table in database.sql is documented in docs/SCHEMA.md, and every table
     HEADING in docs/SCHEMA.md exists in database.sql. A heading is a bolded backticked
     name -- `**`Invoices`**` -- which is a convention the file already follows, so the
     check has something exact to anchor on. (The looser "any backticked capitalised word
     is a table" is not used: `Deposit`, `Active` and `Refunded` are values, not tables.)
   4. AGENTS.md links both docs/ files, and its file map covers every project file.

docs/ONBOARDING.md is in DOC_FILES like any other, because a staff runbook is exactly where
a renamed function goes unnoticed: it names the menu paths and helpers a first-time operator
has to follow, and those are the first things to rot.

Check 2's whitelist is the interesting part. Backticked lowercase-then-paren also matches
`sp_executesql(`, `int(`, `max(` and prose like `python(`, so the names that are not
project functions are listed rather than pattern-matched away. SQL keywords come back
uppercase and are never captured.

`docs/DEVIATIONS.md` is checked by checks 2 and 4 like any other doc. It is the registry of
behaviours this app deliberately does not have, so a function it names going away is exactly
the kind of drift worth catching.


Run with:  python tests/check_docs_sync.py
Exit code is non-zero when anything has drifted.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CODE_FILES = ('main.py', 'reports.py', 'ui.py', 'db.py')
DOC_FILES = ('AGENTS.md', 'docs/SCHEMA.md', 'docs/BOOKING.md', 'docs/DEVIATIONS.md',
             'docs/ONBOARDING.md')
SCHEMA_DOC = 'docs/SCHEMA.md'

# Files that are not project artefacts, so the file map should not have to list them.
# The council transcript/report are the inputs this whole round of work came from: a
# generated analysis of the codebase, kept for reference. Neither is read by the app,
# maintained alongside it, or a dependency of anything -- listing them in AGENTS.md would
# imply they are.
NOT_PROJECT_FILES = {
    'requirements.txt',
    'council-report-20260927-120009.html',
    'council-transcript-20260927-120009.md',
}

# Backticked `name(` tokens that are real calls but not project functions: T-SQL builtins,
# Python builtins, and the stdlib/rich/logging names that appear in prose.
NOT_PROJECT_FUNCTIONS = {
    # T-SQL builtins and type names. Types are here because a column list is often
    # written as `RoomNumber` `varchar(10)` PK, where the type is itself backticked.
    'sp_executesql', 'quotename', 'object_id', 'isnull', 'convert', 'cast', 'getdate',
    'varchar', 'nvarchar', 'nchar', 'char', 'decimal', 'numeric', 'bigint', 'smallint',
    'tinyint', 'bit', 'money', 'smallmoney', 'real', 'float', 'date', 'time',
    'datetime', 'datetime2', 'smalldatetime', 'uniqueidentifier',
    # Python builtins
    'int', 'float', 'str', 'bool', 'len', 'min', 'max', 'sum', 'abs', 'round', 'sorted',
    'set', 'dict', 'list', 'tuple', 'range', 'type', 'isinstance', 'hasattr', 'getattr',
    'print', 'input', 'open', 'super', 'next', 'iter', 'any', 'all', 'map', 'filter',
    'zip', 'enumerate', 'repr', 'format', 'bytes', 'bool',
    # stdlib / third-party named in prose
    'random', 're', 'os', 'sys', 'json', 'csv', 'datetime', 'timedelta', 'pathlib',
    'logging', 'rich', 'pyodbc', 'unittest', 'exec', 'eval', 'getdate', 'convert',
}


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as handle:
        return handle.read()


def project_code():
    return '\n'.join(read(name) for name in CODE_FILES)


def defined_functions():
    """Every function or method defined anywhere in the project's Python."""
    return set(re.findall(r'^\s*def\s+([A-Za-z_]\w*)\s*\(', project_code(), re.M))


def doc_texts():
    out = {}
    for name in DOC_FILES:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            print('MISSING DOC: %s -- AGENTS.md requires it' % name)
            sys.exit(1)
        out[name] = read(name)
    return out


def check_migration_inventory(docs):
    """Every migration on disk is listed in the docs/SCHEMA.md inventory table."""
    schema = docs[SCHEMA_DOC]
    problems = 0
    paths = sorted(glob.glob(os.path.join(ROOT, 'migrations', '*.sql')))
    if not paths:
        print('no migrations found -- is the CWD wrong?')
        return 1
    for path in paths:
        name = os.path.basename(path)
        if name in schema:
            print('%-38s OK' % name)
        else:
            problems += 1
            print('%-38s MISSING from the %s inventory' % (name, SCHEMA_DOC))
    return problems


def check_documented_functions(docs, functions):
    """Every `name(` in backticks in the docs is a function this project defines."""
    problems = 0
    for name, text in sorted(docs.items()):
        # Backticked, lowercase-initial, immediately followed by '(' -- which is why SQL
        # keywords (uppercase) and bare identifiers never reach here.
        found = set(re.findall(r'`([a-z_]\w*)\(', text))
        unknown = sorted(f for f in found
                         if f not in functions and f not in NOT_PROJECT_FUNCTIONS)
        for missing in unknown:
            problems += 1
            print('%s: names function `%s(`, which no longer exists' % (name, missing))
        if not unknown:
            print('%-38s OK (%d documented functions all exist)' % (name, len(found)))
    return problems


def database_tables():
    sql = read('database.sql')
    return set(re.findall(r'CREATE TABLE \[dbo\]\.\[(\w+)\]', sql, re.I))


def check_tables(docs):
    """docs/SCHEMA.md documents every table, and invents none."""
    schema = docs[SCHEMA_DOC]
    tables = database_tables()
    problems = 0

    undocumented = sorted(t for t in tables if '`%s`' % t not in schema)
    for name in undocumented:
        problems += 1
        print('database.sql table %s is not documented in %s' % (name, SCHEMA_DOC))

    # A table heading is a bolded backticked name on its own line. Anchoring on the bold
    # marker is what makes this exact: a bare backticked capitalised word is far more
    # likely to be a Kind value ('Deposit') or a setting key than a table.
    headings = set(re.findall(r'^\*\*`(\w+)`\*\*', schema, re.M))
    for name in sorted(headings - tables):
        problems += 1
        print('%s has a table heading for %s, which is not in database.sql'
              % (SCHEMA_DOC, name))

    print('%-38s OK (%d tables, %d headings)' % (SCHEMA_DOC, len(tables), len(headings)))
    return problems


def check_agents_file_map(docs):
    """AGENTS.md links the docs it depends on, and its file map is not stale."""
    agents = docs['AGENTS.md']
    problems = 0

    for required in ('docs/SCHEMA.md', 'docs/BOOKING.md'):
        if required not in agents:
            problems += 1
            print('AGENTS.md does not reference %s -- the read-first gate is broken'
                  % required)

    covered = 0
    for folder in ('', 'tests', 'docs'):
        pattern = os.path.join(ROOT, folder, '*') if folder else os.path.join(ROOT, '*')
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if not os.path.isfile(path) or name in NOT_PROJECT_FILES:
                continue
            if not (name.endswith(('.py', '.ini', '.sql', '.md'))):
                continue
            if name == 'check_docs_sync.py' and folder == 'tests':
                pass  # listed in AGENTS.md alongside the other checkers; checked below
            label = name if not folder else '%s/%s' % (folder, name)
            if name in agents or label in agents:
                covered += 1
            else:
                problems += 1
                print('AGENTS.md file map does not mention %s' % label)
    print('%-38s OK (%d files covered)' % ('AGENTS.md file map', covered))
    return problems


def main():
    docs = doc_texts()
    functions = defined_functions()

    print('migrations:')
    problems = check_migration_inventory(docs)
    print()
    print('documented functions:')
    problems += check_documented_functions(docs, functions)
    print()
    print('tables:')
    problems += check_tables(docs)
    print()
    print('AGENTS.md:')
    problems += check_agents_file_map(docs)
    print()
    print('problems: %d' % problems)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
