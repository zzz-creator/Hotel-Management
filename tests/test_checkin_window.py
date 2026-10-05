# type: ignore
"""Pin the check-in window rule (DEVIATIONS.md §6 convention, §11 gate)."""
import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as app


class ReservationWindowActiveTests(unittest.TestCase):
    def setUp(self):
        self.year = 2026
        self.ci = date(2026, 10, 4)
        self.co = date(2026, 10, 7)

    def test_arrival_day_is_inside(self):
        self.assertTrue(app.reservation_window_active(self.ci, self.co, date(2026, 10, 4)))

    def test_middle_of_stay_is_inside(self):
        self.assertTrue(app.reservation_window_active(self.ci, self.co, date(2026, 10, 5)))

    def test_checkout_day_is_outside(self):
        # Half-open: the checkout day belongs to the next stay, not this one.
        self.assertFalse(app.reservation_window_active(self.ci, self.co, date(2026, 10, 7)))

    def test_day_before_arrival_is_outside(self):
        self.assertFalse(app.reservation_window_active(self.ci, self.co, date(2026, 10, 3)))

    def test_datetime_business_date_compares_against_dates(self):
        # pyodbc can hand back datetimes; the comparison must not raise.
        self.assertTrue(app.reservation_window_active(
            datetime(2026, 10, 4), datetime(2026, 10, 7), datetime(2026, 10, 5)))

    def test_missing_values_are_outside(self):
        self.assertFalse(app.reservation_window_active(None, self.co, date(2026, 10, 5)))
        self.assertFalse(app.reservation_window_active(self.ci, None, date(2026, 10, 5)))
        self.assertFalse(app.reservation_window_active(self.ci, self.co, None))


if __name__ == "__main__":
    unittest.main()
