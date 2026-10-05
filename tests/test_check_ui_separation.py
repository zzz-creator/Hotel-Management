# type: ignore
"""Tests for tests/check_ui_separation.py itself.

A checker that parses nothing passes, and a checker that parses the same thing
twice reports work that does not exist. `tests/test_schema_sync.py` exists for
the first reason; this file exists for both.

The double-counting assertions below are here because this checker really did
report 359 keyboard reads for the 176 `input()` calls in `main.py` -- once as
`input` and once as an unrenderable `.strip()` chained onto it. A checker whose
counts are inflated by a factor of two would still have said "not clean", so the
bug was invisible in the only place anyone was reading it.
"""
import ast
import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    'check_ui_separation', ROOT / 'tests' / 'check_ui_separation.py')
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def collect(source):
    """Run the checker over a source string, returning its problems."""
    return checker.collect(ast.parse(source))


class DottedTests(unittest.TestCase):
    def test_a_plain_name(self):
        self.assertEqual(checker.dotted(ast.parse('input()').body[0]), 'input')

    def test_an_attribute_chain(self):
        node = ast.parse('logging.info("x")').body[0]
        self.assertEqual(checker.dotted(node), 'logging.info')

    def test_a_chain_onto_a_call_is_unrenderable(self):
        # `input("x").strip()` -- the outer callee bottoms out in a Call, not a
        # Name. This is the shape that caused the double count, so it has a
        # test of its own rather than being left to the aggregate assertions.
        node = ast.parse('input("x").strip()').body[0]
        self.assertIsNone(checker.dotted(node))

    def test_a_long_chain_does_not_exhaust_the_recursion_limit(self):
        # The 8,269-line main.py has attribute chains long enough to blow a
        # recursive implementation's stack. This one did, on the first run.
        depth = 1200
        expr = 'a' + '.b' * depth + '()'
        self.assertEqual(checker.dotted(ast.parse(expr).body[0]),
                         'a' + '.b' * depth)


class KeyboardReadTests(unittest.TestCase):
    def test_a_bare_input_is_found(self):
        self.assertEqual(len(collect('x = input("Name: ")')), 1)

    def test_a_stripped_prompt_is_counted_once(self):
        # The regression. Two AST nodes, one read of the keyboard.
        problems = collect('x = input("Name: ").strip()')
        self.assertEqual(len(problems), 1, problems)
        self.assertEqual(problems[0][1], 'input')

    def test_a_multi_chained_prompt_is_still_counted_once(self):
        for source in ('x = input("a").strip().lower()',
                       'x = input("a").strip().upper()',
                       'x = input(f"Room type: {y}").strip() or "z"'):
            with self.subTest(source=source):
                self.assertEqual(len(collect(source)), 1, source)

    def test_two_separate_reads_are_counted_twice(self):
        self.assertEqual(len(collect('a = input("1")\nb = input("2")')), 2)

    def test_getpass_is_a_keyboard_read(self):
        self.assertEqual(len(collect('s = getpass.getpass("Secret: ")')), 1)

    def test_getpass_chained_is_counted_once(self):
        self.assertEqual(len(collect('s = getpass.getpass("S: ").strip()')), 1)

    def test_a_mistyped_prompt_guard_is_still_found(self):
        # The defensive `except ValueError` around a prompt must not hide the
        # prompt from the checker.
        source = ('try:\n'
                  '    floor = int(input("Floor: ").strip())\n'
                  'except ValueError:\n'
                  '    return\n')
        self.assertEqual(len(collect(source)), 1)

    def test_a_name_that_merely_contains_input_is_not_a_read(self):
        # `input_label` and `get_input()` are somebody's variable and helper.
        # A substring match would flag both.
        self.assertEqual(collect('input_label = 1\nvalue = get_input()\n'), [])


class UiHelperTests(unittest.TestCase):
    def test_a_console_helper_call_is_found(self):
        self.assertEqual(len(collect('ui.show_menu("T", [])')), 1)

    def test_an_aliased_helper_module_is_found(self):
        # Resolved by the real module name, so renaming the alias is not a way
        # past the checker.
        source = 'import ui as helpers\nhelpers.show_menu("T", [])\n'
        self.assertEqual(len(collect(source)), 2)

    def test_a_bare_from_import_is_found(self):
        self.assertEqual(len(collect('from ui import show_menu')), 1)

    def test_importing_the_ui_module_alone_is_found(self):
        self.assertEqual(len(collect('import ui')), 1)

    def test_a_non_ui_module_is_not_flagged(self):
        self.assertEqual(collect('import ui_helpers_lookalike\n'), [])


class LoggingTests(unittest.TestCase):
    def test_logging_info_is_found(self):
        self.assertEqual(len(collect('logging.info("hello")')), 1)

    def test_error_and_warning_are_allowed(self):
        # The core still reports real failures. Only `info`, which narrates to a
        # screen rather than logging, is banned.
        self.assertEqual(collect('logging.error("no")\nlogging.warning("no")\n'), [])

    def test_chained_logging_info_is_counted_once(self):
        self.assertEqual(len(collect('logging.info(f"a {b}")')), 1)


class CleanSourceTests(unittest.TestCase):
    """Source that has no UI in it must report nothing, or the checker is useless."""

    def test_a_business_helper_is_clean(self):
        source = (
            'def stay_nights(check_in, check_out):\n'
            '    """Business logic with no UI."""\n'
            '    if check_out <= check_in:\n'
            '        return 0\n'
            '    return (check_out - check_in).days\n'
        )
        self.assertEqual(collect(source), [])

    def test_a_db_read_is_clean(self):
        source = (
            'def get_room(conn):\n'
            '    cursor = conn.cursor()\n'
            '    cursor.execute("SELECT Status FROM Rooms WHERE RoomNumber = ?", (r,))\n'
            '    row = cursor.fetchone()\n'
            '    return row.Status if row else "Available"\n'
        )
        self.assertEqual(collect(source), [])

    def test_docstrings_mentioning_input_are_not_flagged(self):
        # Prose about prompts is not a prompt. A text match would fail here; the
        # AST walk does not, and this is what makes that worth asserting.
        source = 'def f():\n    """Ask the user with input() and show the result."""\n'
        self.assertEqual(collect(source), [])

    def test_a_comment_mentioning_ui_is_not_flagged(self):
        self.assertEqual(collect('# this replaced ui.show_menu\nx = 1\n'), [])

    def test_a_sql_string_containing_input_is_not_flagged(self):
        source = 'cursor.execute("SELECT input_flag FROM Things")\n'
        self.assertEqual(collect(source), [])


class ProjectCoreTests(unittest.TestCase):
    """The real `main.py`, checked while the split is still in progress.

    These assert counts, not cleanliness. Phase 0 has not finished, so main.py
    still holds the UI -- what must hold is that the counts are the ones a human
    counted, because a checker that silently stops matching is worse than no
    checker at all.
    """

    def test_the_counts_match_a_direct_count_of_the_source(self):
        source = (ROOT / 'main.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        problems = checker.collect(tree)

        bare_input = len([n for n in ast.walk(tree)
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name)
                          and n.func.id == 'input'])
        reported_input = len([p for p in problems if p[1] == 'input'])

        self.assertEqual(bare_input, 176, 'main.py changed; recount and update')
        self.assertEqual(reported_input, bare_input,
                         'the checker and a direct count disagree')

    def test_every_keyboard_read_is_reported_exactly_once(self):
        # The total is the sum of the kinds, not a larger number. This is the
        # assertion that would have caught the 359-vs-176 double count.
        source = (ROOT / 'main.py').read_text(encoding='utf-8')
        problems = checker.collect(ast.parse(source))
        keyboard = [p for p in problems if p[2] == 'reads from the keyboard']

        # 176 input() + 7 getpass.getpass(), no indirect reports remaining.
        self.assertEqual(len(keyboard), 183, keyboard[:5])

    def test_no_reference_is_reported_twice(self):
        # The double-count guard. Two DIFFERENT references on one line are
        # legitimate -- main.py:7990 is `(ui.success if granted else
        # ui.error)(detail)`, one line, two distinct helpers -- so this asserts
        # no (line, name) pair repeats, rather than no line repeating.
        source = (ROOT / 'main.py').read_text(encoding='utf-8')
        problems = checker.collect(ast.parse(source))
        seen = {}
        for lineno, text, why in problems:
            key = (lineno, text)
            if key in seen:
                self.fail('%s reported twice on line %d: %r and %r'
                          % (text, lineno, seen[key], why))
            seen[key] = why

    def test_a_conditional_helper_call_reports_both_branches(self):
        # `(ui.success if ok else ui.error)(detail)` is two helpers on one line
        # and both are real. Confirms the assertion above is not just permissive.
        problems = collect('(ui.success if granted else ui.error)(detail)')
        self.assertEqual(sorted(p[1] for p in problems),
                         ['ui.error', 'ui.success'])


if __name__ == '__main__':
    unittest.main()