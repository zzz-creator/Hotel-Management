# type: ignore
"""Unit tests for the AuditLog diff columns (migration 026).

AuditLog used to record only the free-text sentence the caller passed to
log_audit(). These tests pin the new contract: log_audit() stores the old/new
values the caller passes, and a caller that does not know them leaves the
columns NULL rather than inventing them.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
from patch_main import patch_main


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        pass


class LogAuditDiffTests(unittest.TestCase):
    def _audit(self, **kwargs):
        log = []
        with patch_main("get_connection", return_value=_FakeConn(log)), \
             patch_main("CURRENT_USER", "tester"):
            app.log_audit("UPDATE", "Setting", "tax_rate", "tax_rate -> 0.15", **kwargs)
        return log

    def test_old_and_new_values_are_stored(self):
        log = self._audit(old_value="0.13", new_value="0.15")
        sql, params = log[0]
        self.assertIn("OldValue", sql)
        self.assertIn("NewValue", sql)
        self.assertIn("0.13", params)
        self.assertIn("0.15", params)

    def test_no_diff_leaves_the_columns_null(self):
        # A caller that has no before-image (a CREATE, a LOGIN, ...) must not
        # invent one: the columns stay NULL rather than holding a made-up empty
        # string that would claim "the value was blank before".
        log = self._audit()
        _sql, params = log[0]
        self.assertIn(None, params)

    def test_a_none_like_value_stays_none_not_the_string(self):
        log = self._audit(old_value=None, new_value=0.15)
        _sql, params = log[0]
        self.assertIn(None, params)
        self.assertNotIn("None", params)
