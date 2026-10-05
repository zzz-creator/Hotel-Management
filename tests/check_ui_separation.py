# type: ignore
"""Static guard: `main.py` must contain no user interface.

`main.py` is the business core that both `main-console.py` (rich) and
`main-tkinter.py` (tkinter) call. A prompt, a `rich` widget call or a
`logging.info` left in the core is a UI decision made in the one place two front
ends cannot both honour, so the split stops being a split.

The same argument as the `get_connection()` duplication in AGENTS.md section 7:
that rule existed, was unenforced, and was violated anyway. A convention nothing
checks is a convention one stray `input(` away from gone. So this is machine
checked.

Run with:  python tests/check_ui_separation.py
Exit code is non-zero when the core contains UI.

**This ships with the phase that makes it pass, never before.** A lint that fails
from the moment it is added trains people to ignore it, and the tests fail for an
unrelated reason on the same day. While the split is in progress the check is
allowed to report, and exits 1 with a summary -- that is the honest signal that
Phase 0 is unfinished, not a reason to soften the assertion.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The core. Everything else in the repository is a front end, a helper or a
# checker, and is allowed to talk to the user.
CORE = 'main.py'

# `input(` bare, and `getpass.` -- the two ways this codebase has historically read
# a value at a keyboard. `ui.` is this project's console helper module.
BANNED_CALLS = ('input', 'getpass.getpass', 'getpass.getuser')

# `logging.info` rather than `logging.` generally: the core still logs errors and
# warnings, which is correct. What it must not do is narrate to a screen -- a
# `logging.info` in the core is a `print()` that happens to look like a log line,
# and it is how the two front ends would end up disagreeing about wording.
BANNED_LOG_CALLS = ('logging.info',)

# Module-level attribute access on the console helper module, however it is
# spelled: `ui.show_menu(...)`, `import ui as u` then `u.show_menu(...)`, or a
# bare `from ui import show_menu`. The AST walk below resolves all three.


def read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def dotted(node):
    """Render a callee as dotted text, or None if it is not a name/attribute chain.

    Iterative, not recursive: an attribute chain is walked in a loop because this
    runs over a real file, and the 8,269-line `main.py` has chains long enough to
    exceed the recursion limit on a naive recursive version. (It did, on the first
    run of this checker.)
    """
    # Tolerate being handed the expression statement wrapping the call, which is
    # what `ast.parse('input()').body[0]` gives you.
    if isinstance(node, ast.Expr):
        node = node.value

    parts = []
    target = node.func if isinstance(node, ast.Call) else node
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if not isinstance(target, ast.Name):
        return None
    parts.append(target.id)
    return '.'.join(reversed(parts))


def is_ui_reference(name, aliases=('ui',)):
    """True for `ui.` under any of the three import styles.

    `aliases` carries the local names bound to the module, so `import ui as
    helpers` / `helpers.show_menu(...)` is caught. That is the point: a rename is
    exactly the trick that would otherwise slip past a checker that only looked
    for the literal text `ui.`. Defaults to the plain module name so the function
    is usable on its own, which is what its unit tests do.
    """
    if name is None:
        return False
    return any(name == alias or name.startswith(alias + '.') for alias in aliases)


def collect(tree):
    """Every UI reference in `tree`, as (lineno, text, why).

    One report per *call site*, not per AST node. That distinction is the whole
    implementation: `input("...").strip()` is a single read of the keyboard, but
    it is two AST nodes, and the first version of this checker walked both and
    reported 359 keyboard reads for the 176 `input()` calls that exist.
    """
    problems = []

    # Local names bound to the console helper module, however it was imported.
    # Resolving the alias is the point: `import ui as helpers` must not be a way
    # past a checker that only recognises the literal text `ui.`.
    ui_aliases = {'ui'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'ui':
                    ui_aliases.add(alias.asname or 'ui')
        elif isinstance(node, ast.ImportFrom):
            if node.module == 'ui':
                for alias in node.names:
                    ui_aliases.add(alias.asname or alias.name)

    # Attribute nodes that are the callee of a call. Those were already judged
    # as part of that call, so judging them again is the double count.
    callee_attrs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            callee_attrs.add(id(node.func))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node)
            if name in BANNED_CALLS:
                problems.append((node.lineno, name, 'reads from the keyboard'))
            elif name in BANNED_LOG_CALLS:
                problems.append((node.lineno, name,
                                 'narrates to a screen; use logging.error/warning, '
                                 'or return a value for the front end to render'))
            elif name is None:
                # `dotted()` gave up, which happens for a lambda, a subscript, or
                # a chain like `input("x").strip().lower()` where the innermost
                # callee is itself a call. Only report if the banned name is
                # reached WITHOUT passing through a Call that already reported
                # itself -- otherwise `input("x").strip()` would be counted twice,
                # once by the `input` branch and once here, and every stripped
                # prompt in the file would be a phantom.
                for banned in BANNED_CALLS:
                    root = banned.split('.')[0]
                    already = any(isinstance(inner, ast.Call)
                                  and dotted(inner) in BANNED_CALLS
                                  for inner in ast.walk(node.func))
                    if already:
                        continue
                    if any(isinstance(leaf, ast.Name) and leaf.id == root
                           for leaf in ast.walk(node.func)):
                        problems.append((node.lineno, '<indirect %s>' % root,
                                         'reads from the keyboard'))
                    break
            elif is_ui_reference(name, ui_aliases):
                problems.append((node.lineno, name, 'console UI helper'))

        elif isinstance(node, ast.Attribute):
            # A bare `from ui import show_menu` leaves a reference, not a call,
            # so the branch above never sees it.
            name = dotted(node)
            if (is_ui_reference(name, ui_aliases) and id(node) not in callee_attrs
                    and isinstance(node.ctx, ast.Load)):
                problems.append((node.lineno, name, 'console UI helper'))

    # `import ui` / `from ui import x` at module level, which is the root of the
    # problem even when no attribute is touched.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'ui' or alias.name.startswith('ui.'):
                    problems.append((node.lineno, 'import %s' % alias.name,
                                     'the core must not import a UI module'))
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == 'ui' or node.module.startswith('ui')):
                problems.append((node.lineno, 'from %s import ...' % node.module,
                                 'the core must not import a UI module'))

    return sorted(set(problems))


def main():
    path = os.path.join(ROOT, CORE)
    if not os.path.exists(path):
        print('%s not found -- wrong CWD?' % CORE)
        return 1

    source = read(path)
    tree = ast.parse(source)
    problems = collect(tree)

    # Counts by kind, so the summary says what is left rather than just that
    # something is.
    prompts = [p for p in problems if 'keyboard' in p[2]]
    helpers = [p for p in problems if 'UI helper' in p[2] or 'UI module' in p[2]]
    logs = [p for p in problems if 'narrates' in p[2]]

    for lineno, text, why in problems:
        print('%s:%d: %s  -- %s' % (CORE, lineno, text, why))

    print()
    print('%d UI reference(s) in the business core: '
          '%d keyboard read(s), %d console helper(s), %d logging.info'
          % (len(problems), len(prompts), len(helpers), len(logs)))

    if problems:
        print()
        print('%s is the business core. Prompts and screen output belong to' % CORE)
        print('main-console.py or main-tkinter.py; a core helper takes its inputs')
        print('as parameters and returns a result for the front end to render.')
        print('See PLAN-tkinter-frontend.md.')
        return 1

    print('%s is clean: no UI in the business core.' % CORE)
    return 0


if __name__ == '__main__':
    sys.exit(main())