"""Static sanity checks for the migration files.

Each check here corresponds to a bug that actually reached a live database and had to be
diagnosed from SSMS output, which is the only reason they exist. py_compile cannot see any
of this -- these files are not Python, and nothing else in the toolchain parses T-SQL.

  1. A GO batch separator inside a BEGIN...END block. SSMS ends the batch at the GO, leaving
     the parser holding an unterminated BEGIN, and every later statement cascades into
     "Msg 102 Incorrect syntax near ')'" / "Msg 156 near the keyword 'IF'".
  2. dbo.sys.* instead of sys.*, which fails at runtime with Msg 208.
  3. Dynamic SQL not run through sp_executesql. Two spellings were each shipped to a live
     database and each failed: EXEC('literal ' + ...) is a parse error (Msg 102), and a bare
     `EXEC @var` with no parentheses is interpreted as a module name, so the statement text
     becomes an object name (Msg 203). In both cases the batch dies, the statement silently
     does not run, and the failure surfaces one step later as Msg 4922.

Comments are stripped before every check. Two of these rules are documented in prose inside
the migrations themselves, so a checker that reads raw text flags the very comment warning
about the bug.
"""
import re
import sys
from pathlib import Path


def strip_noise(line):
    """Remove string-literal contents and `--` comments, respecting quoting.

    Returns the line with comments and literal text blanked so that a quoted 'BEGIN' or an
    EXEC('...') shown inside a comment cannot be read as real code.
    """
    out = []
    in_string = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
                out.append("'")
            i += 1
            continue
        if ch == "'":
            in_string = True
            out.append("'")
            i += 1
            continue
        if ch == '-' and i + 1 < len(line) and line[i + 1] == '-':
            break
        out.append(ch)
        i += 1
    return ''.join(out)


def sql_files():
    """Every hand-written .sql file: the migrations plus the test seed/cleanup scripts."""
    if len(sys.argv) > 1:
        return sorted(Path(sys.argv[1]).glob('*.sql'))
    found = sorted(Path('migrations').glob('*.sql'))
    found += sorted(Path('tests').glob('*.sql'))
    return found


MIGRATIONS = sql_files()
problems = 0


def report(path, lineno, message):
    global problems
    problems += 1
    print('%s:%d  %s' % (path, lineno, message))


for path in MIGRATIONS:
    raw_lines = path.read_text(encoding='utf-8').splitlines()
    code = [strip_noise(line) for line in raw_lines]

    # 1. BEGIN/END balance, as a stack because END also closes a CASE expression.
    #    BEGIN TRANSACTION / BEGIN TRAN is NOT a block -- it has no END -- so it must not be
    #    pushed, or the stack never empties and every later GO looks nested.
    stack = []
    for lineno, line in enumerate(code, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        scrubbed = re.sub(r'\bBEGIN\s+TRAN(?:SACTION)?\b', ' ', upper)
        for match in re.finditer(r'\b(BEGIN|CASE)\b', scrubbed):
            stack.append(match.group(1))
        for _ in re.finditer(r'\bEND\b', scrubbed):
            if stack:
                stack.pop()
            else:
                report(path, lineno, 'END with no matching BEGIN or CASE')
        if re.match(r'^GO\b', upper) and 'BEGIN' in stack:
            report(path, lineno, 'GO inside a BEGIN...END block (breaks the batch)')
    if stack:
        problems += 1
        print('%s  unclosed at end of file: %s' % (path, ', '.join(stack)))

    joined = '\n'.join(code)
    code_text = strip_noise(joined)

    # 2. dbo.sys.* is not a thing.
    for match in re.finditer(r'\bdbo\.sys\.', code_text, re.I):
        report(path, code_text[:match.start()].count('\n') + 1,
               'references dbo.sys.* -- catalog views live in the sys schema')

    # 3. Dynamic SQL must go through sp_executesql, or EXECUTE with parentheses.
    #    EXEC('literal ' + ...) is a parse error (Msg 102), and a bare `EXEC @var` with no
    #    parentheses is read as a module name, so the string becomes an object name and fails
    #    with Msg 203. In both cases the statement silently does not run.
    for match in re.finditer(r'\bEXEC(?:UTE)?\s*\(?\s*N?\'', code_text, re.I):
        tail = code_text[match.end():match.end() + 200]
        if re.search(r"'\s*(?:\+|\|\|)", tail):
            report(path, code_text[:match.start()].count('\n') + 1,
                   "EXEC('literal ' + ...) is a parse error -- use sp_executesql")

    for match in re.finditer(r'\bEXEC(?:UTE)?\s+@\w+', code_text, re.I):
        tail = code_text[match.end():]
        if not re.match(r'\s*\(', tail):
            report(path, code_text[:match.start()].count('\n') + 1,
                   'bare EXEC @var is read as a module name (Msg 203) -- use sp_executesql')

print()
print('migration files scanned: %d | problems: %d' % (len(MIGRATIONS), problems))
sys.exit(1 if problems else 0)
