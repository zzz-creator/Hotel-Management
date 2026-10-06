# type: ignore
"""Unit tests for the loyalty points expiry sweep.

DEVIATIONS.md §4 (closed in this change): points used to live forever, and the
old `loyalty_expiration_days` setting was editable on screen but read by nothing.
These tests pin the sweep's contract: keyed on LoyaltyTransactions.CreatedAt,
idempotent per earn row, FIFO from the current balance, and the balance
recomputed from the ledger so it cannot drift from the rows that make it up.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app


def _as_queue(result_sets):
    if result_sets is None:
        return []
    items = list(result_sets)
    if items and all(isinstance(item, list) for item in items):
        return items
    return [items]


class _FakeCursor:
    """Records statements and hands out one queued result set per SELECT."""

    def __init__(self, log, result_sets):
        self.log = log
        self.result_sets = result_sets
        self.rows = []
        self._index = 0

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            self.rows = list(self.result_sets.pop(0)) if self.result_sets else []
            self._index = 0
        else:
            self.rows = []
        return self

    def fetchone(self):
        if self._index < len(self.rows):
            row = self.rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self):
        return list(self.rows)


class _FakeConn:
    def __init__(self, log, result_sets=None):
        self.log = log
        self.result_sets = _as_queue(result_sets)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.log, self.result_sets)

    def commit(self):
        pass


def _inserts(log):
    return [e for e in log if e[0].upper().startswith("INSERT INTO LOYALTYTRANSACTIONS")]


class ExpirySweepTests(unittest.TestCase):
    def test_expires_old_earns_fifo_and_recomputes_the_balance(self):
        log = []
        # Queries, in order: account list; balance; old earns; guard row #1; guard row #2.
        conn = _FakeConn(log, [[(7,)], [(500,)], [(1, 300), (2, 200)], [], []])
        with mock.patch.object(app, "get_connection", return_value=conn), \
             mock.patch.object(app, "LOYALTY_ENABLED", True), \
             mock.patch.object(app, "log_audit"):
            stats = app.run_loyalty_expiry_sweep(now=datetime(2026, 10, 6))
        self.assertEqual(stats, {"rows": 2, "accounts": 1, "points": 500})
        inserts = _inserts(log)
        self.assertEqual(len(inserts), 2)
        # FIFO from the current balance: 300 first, then 200, and the account row is
        # rebuilt from the ledger rather than decremented by hand.
        self.assertIn(-300, inserts[0][1])
        self.assertIn("expire:1", inserts[0][1])
        self.assertIn(-200, inserts[1][1])
        self.assertIn("expire:2", inserts[1][1])
        recomputes = [e for e in log if e[0].upper().startswith("UPDATE LOYALTYACCOUNTS")]
        self.assertEqual(len(recomputes), 1)
        self.assertIn("SUM(Delta)", recomputes[0][0])

    def test_an_already_expired_row_is_never_expired_twice(self):
        log = []
        # The guard SELECT finds 'expire:1' already present, so the row is skipped.
        conn = _FakeConn(log, [[(7,)], [(500,)], [(1, 300)], [(1,)]])
        with mock.patch.object(app, "get_connection", return_value=conn), \
             mock.patch.object(app, "LOYALTY_ENABLED", True), \
             mock.patch.object(app, "log_audit"):
            stats = app.run_loyalty_expiry_sweep(now=datetime(2026, 10, 6))
        self.assertEqual(stats["rows"], 0)
        self.assertEqual(_inserts(log), [])

    def test_points_already_redeemed_cannot_be_expired(self):
        # Balance is 100 but the old earns total 500: only 100 expires, the rest is
        # left to the guest who has already spent it.
        log = []
        conn = _FakeConn(log, [[(7,)], [(100,)], [(1, 300), (2, 200)], [], []])
        with mock.patch.object(app, "get_connection", return_value=conn), \
             mock.patch.object(app, "LOYALTY_ENABLED", True), \
             mock.patch.object(app, "log_audit"):
            stats = app.run_loyalty_expiry_sweep(now=datetime(2026, 10, 6))
        self.assertEqual(stats["points"], 100)
        inserts = _inserts(log)
        self.assertIn(-100, inserts[0][1])
        self.assertIn(0, inserts[1][1])  # clamped: nothing left to expire

    def test_a_zero_balance_writes_guard_rows_but_takes_nothing(self):
        log = []
        conn = _FakeConn(log, [[(7,)], [(0,)], [(1, 300)], []])
        with mock.patch.object(app, "get_connection", return_value=conn), \
             mock.patch.object(app, "LOYALTY_ENABLED", True), \
             mock.patch.object(app, "log_audit"):
            stats = app.run_loyalty_expiry_sweep(now=datetime(2026, 10, 6))
        self.assertEqual(stats["points"], 0)
        self.assertEqual(len(_inserts(log)), 1)  # the guard row, Delta 0

    def test_cutoff_is_one_year_back_from_now(self):
        log = []
        conn = _FakeConn(log, [[]])  # no old earns at all
        now = datetime(2026, 10, 6)
        with mock.patch.object(app, "get_connection", return_value=conn), \
             mock.patch.object(app, "LOYALTY_ENABLED", True), \
             mock.patch.object(app, "log_audit"):
            app.run_loyalty_expiry_sweep(now=now)
        _sql, params = log[0]
        self.assertEqual(params[0], now - timedelta(days=app.LOYALTY_POINTS_EXPIRY_DAYS))


if __name__ == "__main__":
    unittest.main()
