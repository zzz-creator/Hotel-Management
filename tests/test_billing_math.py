# type: ignore
"""Unit tests for the pure billing arithmetic in main.py.

These cover the parts of check-out that decide what a guest owes, and they are
deliberately database-free: every helper here is monkeypatched so the module can be
imported without a live SQL Server connection.

Run with:  python -m unittest discover -s tests
"""

import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
import reports


class StayNightsTests(unittest.TestCase):
    def test_counts_midnight_crossings(self):
        check_in = datetime(2026, 3, 1, 14, 0)
        check_out = datetime(2026, 3, 4, 11, 0)
        self.assertEqual(app.stay_nights(check_in, check_out), 3)

    def test_same_day_arrival_is_zero_nights(self):
        day = datetime(2026, 3, 1, 22, 0)
        self.assertEqual(app.stay_nights(day, day), 0)

    def test_same_day_departure_is_never_negative(self):
        # A guest who checks out before checking in (data entry slip) must not
        # produce a negative room charge.
        check_in = datetime(2026, 3, 4, 10, 0)
        check_out = datetime(2026, 3, 1, 10, 0)
        self.assertEqual(app.stay_nights(check_in, check_out), 0)

    def test_uses_calendar_days_not_hours(self):
        # Checked in late, out early: still one billed night.
        check_in = datetime(2026, 3, 1, 23, 59)
        check_out = datetime(2026, 3, 2, 0, 5)
        self.assertEqual(app.stay_nights(check_in, check_out), 1)

    def test_missing_dates_are_zero(self):
        self.assertEqual(app.stay_nights(None, datetime(2026, 3, 4)), 0)
        self.assertEqual(app.stay_nights(datetime(2026, 3, 4), None), 0)
        self.assertEqual(app.stay_nights(None, None), 0)

    def test_accepts_plain_dates_as_stored_by_reservations(self):
        # Reservations.CheckInDate/CheckOutDate are `date` columns, so the real
        # check-out path passes dates, not datetimes.
        self.assertEqual(app.stay_nights(date(2026, 3, 1), date(2026, 3, 4)), 3)
        self.assertEqual(app.stay_nights(date(2026, 3, 1), date(2026, 3, 1)), 0)
        self.assertEqual(app.stay_nights(date(2026, 3, 4), date(2026, 3, 1)), 0)

    def test_accepts_mixed_date_and_datetime(self):
        self.assertEqual(app.stay_nights(date(2026, 3, 1), datetime(2026, 3, 4, 9, 0)), 3)
        self.assertEqual(app.stay_nights(datetime(2026, 3, 1, 9, 0), date(2026, 3, 4)), 3)


class RoomChargeTests(unittest.TestCase):
    """post_room_charge() is the room half of the folio, charged at face value."""

    def _post(self, nights, rate, check_out=None, already_posted=False):
        """Run post_room_charge() against a stubbed room/rate and return (tx_id, calls)."""
        check_in = datetime(2026, 3, 1, 15, 0)
        check_out = check_out or check_in + timedelta(days=nights)
        calls = []

        class FakeCursor:
            def execute(self, sql, params=()):
                calls.append((sql, params))
                self._is_insert = sql.lstrip().upper().startswith("INSERT")

            def fetchone(self):
                # The idempotency probe returns the pre-existing row, the INSERT
                # returns the new identity value.
                if getattr(self, "_is_insert", False):
                    return (4242,)
                return (99,) if already_posted else None

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

        with mock.patch.object(app, "get_connection", return_value=FakeConn()), \
             mock.patch.object(app, "get_room_type", return_value="Deluxe"), \
             mock.patch.object(app, "get_nightly_rate", return_value=rate):
            tx_id = app.post_room_charge("9012", check_in, check_out)
        return tx_id, calls

    def test_charges_nights_times_rate(self):
        tx_id, calls = self._post(nights=3, rate=189.0)
        insert = next(c for c in calls if "INSERT" in c[0])
        params = insert[1]
        # (room, nights, nightly rate, amount, charge group, description)
        self.assertEqual(params[1], 3)
        self.assertEqual(params[2], 189.0)
        self.assertEqual(params[3], 567.0)
        self.assertEqual(params[4], app.CHARGE_GROUP_ROOM)
        self.assertEqual(tx_id, 4242)

    def test_rate_is_snapshotted_onto_the_line(self):
        _, calls = self._post(nights=2, rate=250.0)
        insert = next(c for c in calls if "INSERT" in c[0])
        # The rate is copied into UnitPrice so a later rate change cannot rewrite
        # an invoice that was already issued.
        self.assertEqual(insert[1][2], 250.0)

    def test_is_idempotent_on_the_stay_marker(self):
        _, calls = self._post(nights=2, rate=189.0)
        probe = calls[0]
        self.assertIn("SELECT ID FROM Transactions", probe[0])
        # Both the probe and the stored description key off the same check-in value.
        self.assertIn("9012", probe[1][0])
        self.assertIn("2026-03-01", probe[1][1])

    def test_retried_checkout_does_not_double_charge(self):
        tx_id, calls = self._post(nights=2, rate=189.0, already_posted=True)
        self.assertIsNone(tx_id)
        self.assertFalse(any("INSERT" in c[0] for c in calls))

    def test_zero_night_stay_posts_nothing(self):
        # Same-day arrival/departure must not leave a bogus $0 room line on the folio.
        check_in = datetime(2026, 3, 1, 9, 0)
        tx_id, calls = self._post(nights=0, rate=189.0, check_out=check_in)
        self.assertIsNone(tx_id)
        self.assertEqual(calls, [])

    def test_missing_rate_skips_the_charge(self):
        with mock.patch.object(app, "get_connection"), \
             mock.patch.object(app, "get_room_type", return_value="Standard"), \
             mock.patch.object(app, "get_nightly_rate", return_value=0.0):
            self.assertIsNone(app.post_room_charge("9012", datetime(2026, 3, 1, 15, 0),
                                                   datetime(2026, 3, 4, 11, 0)))

    def test_partial_day_departure_still_bills_full_nights(self):
        # The regression this guards: a 2d21h stay must bill 3 nights, not 2.
        _, calls = self._post(nights=0, rate=100.0,
                              check_out=datetime(2026, 3, 4, 11, 0))
        insert = next(c for c in calls if "INSERT" in c[0])
        self.assertEqual(insert[1][1], 3)


class CapturedRateTests(unittest.TestCase):
    """A booked rate is a contract; the rate card is only a price list.

    Reading RoomTypes at check-out meant any admin rate edit between booking and arrival
    re-priced every confirmed stay in the house, silently, in both directions, and the
    guest found out at the till. stay_nightly_rate() is the precedence that stops it, and
    it is pure precisely so this can be asserted without a database.
    """

    def test_captured_rate_wins_over_the_current_one(self):
        # The rate fell after the guest booked: they still pay what they agreed to.
        self.assertEqual(app.stay_nightly_rate(189.0, 240.0), 189.0)

    def test_captured_rate_also_wins_when_the_rate_rose(self):
        # And in the other direction, which the old code got wrong just as badly.
        self.assertEqual(app.stay_nightly_rate(120.0, 95.0), 120.0)

    def test_missing_capture_falls_back_to_the_current_rate(self):
        # A stay written before migration 022 has no captured rate. It is billed the old
        # way rather than refusing to check the guest out.
        self.assertEqual(app.stay_nightly_rate(None, 189.0), 189.0)

    def test_zero_capture_is_not_treated_as_a_free_room(self):
        # 0 must fall through to the current rate: _rate_or_none() never stores 0
        # precisely because a free room and an uncaptured one would look the same.
        self.assertEqual(app.stay_nightly_rate(0.0, 189.0), 189.0)
        self.assertEqual(app.stay_nightly_rate(0, 189.0), 189.0)

    def test_no_capture_and_no_current_rate_bills_nothing(self):
        self.assertEqual(app.stay_nightly_rate(None, 0.0), 0.0)

    def test_garbage_capture_falls_back_instead_of_raising(self):
        # A hand-edited or truncated value must not break check-out.
        self.assertEqual(app.stay_nightly_rate("not-a-rate", 189.0), 189.0)
        self.assertEqual(app.stay_nightly_rate(None, "not-a-rate"), 0.0)

    def test_rate_is_rounded_to_cents_on_the_way_in(self):
        # The column is DECIMAL(10,2) and the folio line must be the same number as the
        # captured rate, or the invoice snapshot and the reservation disagree.
        self.assertEqual(app.stay_nightly_rate(189.006, 240.0), 189.01)
        self.assertEqual(app._rate_or_none(189.006), 189.01)

    def test_rate_or_none_keeps_positive_and_drops_the_rest(self):
        self.assertEqual(app._rate_or_none(189.0), 189.0)
        self.assertIsNone(app._rate_or_none(0))
        self.assertIsNone(app._rate_or_none(-5))
        self.assertIsNone(app._rate_or_none(None))
        self.assertIsNone(app._rate_or_none("junk"))


class BusinessDateTests(unittest.TestCase):
    """One clock for "which day is it", and reports that can be re-run for a past day."""

    def test_business_date_is_just_today(self):
        # As of 5 October 2026 the operator-set clock was removed at the owner's request:
        # "today" is the wall clock. Reports take an explicit date/window for re-runs.
        self.assertEqual(app.business_date(), datetime.now().date())

    def test_business_date_ignores_the_stored_setting(self):
        # A stale HotelSettings row must not move the clock.
        with mock.patch.object(app, "get_setting", return_value="1999-01-01"):
            self.assertEqual(app.business_date(), datetime.now().date())


class ReportDateWindowTests(unittest.TestCase):
    """The housekeeping and occupancy reports must not read the wall clock.

    They disagreed with each other about the same rows -- occupancy counted a night as
    sold while `CheckOutDate > night`, housekeeping used `>=` -- and neither could be
    re-run for a day that had already closed. Each takes an explicit date/window so a
    closed day can be re-run by passing its date.
    """

    def _capture(self, fn, **kwargs):
        """Run an export against a stubbed connection and return the SQL it issued."""
        seen = []

        class FakeCursor:
            def execute(self, sql, params=()):
                seen.append((" ".join(sql.split()), params))

            def fetchone(self):
                return ("2026-03-04",)

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return FakeCursor()

        with mock.patch.object(reports, "get_connection", return_value=FakeConn()):
            try:
                fn(**kwargs)
            except Exception:
                pass
        return seen

    def test_housekeeping_uses_the_half_open_window(self):
        sql = " ".join(s for s, _ in self._capture(reports.export_housekeeping))
        self.assertNotIn("GETDATE()", sql)
        self.assertNotIn("HotelSettings", sql,
                         "the stored business-date clock was removed; today's date comes from the wall clock")
        self.assertIn("CheckOutDate >", sql,
                      "a guest departing on the 4th is not in house on the 4th")
        self.assertNotIn("CheckOutDate >=", sql)
        self.assertIn("CheckInDate <=", sql)

    def test_housekeeping_takes_an_explicit_date(self):
        seen = self._capture(reports.export_housekeeping, on_date="2026-03-04")
        for sql, params in seen:
            if "CheckOutDate >" in sql:
                self.assertEqual(params[1], date(2026, 3, 4))

    def test_housekeeping_defaults_to_the_wall_clock(self):
        conn_seen = []

        class FakeCursor:
            def execute(self, sql, params=()):
                conn_seen.append((sql, params))

            def fetchone(self):
                return ("2026-03-04",)

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return FakeCursor()

        with mock.patch.object(reports, "get_connection", return_value=FakeConn()):
            try:
                reports.export_housekeeping()
            except Exception:
                pass
        sql = " ".join(s for s, _ in conn_seen)
        self.assertNotIn("HotelSettings", sql,
                         "the board no longer reads the stored business date")

    def test_occupancy_takes_a_window(self):
        seen = self._capture(reports.export_occupancy, start_date="2026-03-01",
                            end_date="2026-03-07")
        joined = " ".join(s for s, _ in seen)
        self.assertIn("Night >= ?", joined, "occupancy must accept a start date")
        self.assertIn("Night <= ?", joined, "occupancy must accept an end date")
        self.assertIn("2026-03-01", [str(p) for _, params in seen for p in params])

    def test_occupancy_still_uses_the_half_open_window(self):
        joined = " ".join(s for s, _ in self._capture(reports.export_occupancy))
        self.assertIn("CheckOutDate > c.Night", joined)
        self.assertNotIn("CheckOutDate >= c.Night", joined)

    def test_every_report_agrees_on_which_rooms_are_occupied(self):
        # The two reports used to answer differently about the same row, which is only
        # checkable as a shared rule: both must exclude a guest leaving on the board date.
        house = " ".join(s for s, _ in self._capture(reports.export_housekeeping))
        occ = " ".join(s for s, _ in self._capture(reports.export_occupancy))
        self.assertIn("CheckInDate <=", house)
        self.assertIn("CheckInDate <=", occ)
        self.assertIn("CheckOutDate >", house)
        self.assertIn("CheckOutDate > c.Night", occ)

    def test_unparseable_cli_date_is_ignored_rather_than_crashing(self):
        self.assertIsNone(reports._coerce_date("last tuesday"))
        self.assertIsNone(reports._coerce_date(""))
        self.assertIsNone(reports._coerce_date(None))
        self.assertEqual(reports._coerce_date("2026-03-04"), date(2026, 3, 4))


class SplitFolioTests(unittest.TestCase):
    """Discounts apply to F&B only; both groups are taxed; the invoice keeps both totals."""

    def _settle(self, room_subtotal, fnb_subtotal, tax_rate, discount_pct, tier_pct):
        """Reproduce the split-folio arithmetic from bill_room_transactions()."""
        discount_code_amount = round(fnb_subtotal * discount_pct, 2)
        tier_amount = round(fnb_subtotal * tier_pct, 2)
        discounted_fnb = max(0.0, round(fnb_subtotal - discount_code_amount - tier_amount, 2))
        room_tax = round(room_subtotal * tax_rate, 2)
        fnb_tax = round(discounted_fnb * tax_rate, 2)
        return {
            "room_subtotal": room_subtotal,
            "room_tax": room_tax,
            "room_total": round(room_subtotal + room_tax, 2),
            "fnb_subtotal": fnb_subtotal,
            "fnb_discount_code": discount_code_amount,
            "fnb_tier": tier_amount,
            "fnb_tax": fnb_tax,
            "subtotal": round(room_subtotal + discounted_fnb, 2),
            "tax": round(room_tax + fnb_tax, 2),
        }

    def test_discounts_never_reach_the_room_charge(self):
        r = self._settle(room_subtotal=567.0, fnb_subtotal=100.0, tax_rate=0.1,
                         discount_pct=0.20, tier_pct=0.15)
        # The room is billed at full face value.
        self.assertEqual(r["room_subtotal"], 567.0)
        self.assertEqual(r["room_tax"], 56.70)
        self.assertEqual(r["room_total"], 623.70)
        # Only F&B is reduced: 100 - 20 - 15 = 65.
        self.assertEqual(r["fnb_discount_code"], 20.0)
        self.assertEqual(r["fnb_tier"], 15.0)
        self.assertEqual(r["fnb_tax"], 6.50)

    def test_grand_total_is_room_plus_discounted_fnb(self):
        r = self._settle(room_subtotal=300.0, fnb_subtotal=200.0, tax_rate=0.1,
                         discount_pct=0.10, tier_pct=0.00)
        # 300 room + (200 - 20) fnb = 480 subtotal, 48 tax, 528 total.
        self.assertEqual(r["subtotal"], 480.0)
        self.assertEqual(r["tax"], 48.0)
        self.assertEqual(round(r["subtotal"] + r["tax"], 2), 528.0)

    def test_both_groups_are_taxed(self):
        r = self._settle(room_subtotal=100.0, fnb_subtotal=100.0, tax_rate=0.0825,
                         discount_pct=0.0, tier_pct=0.0)
        self.assertEqual(r["room_tax"], 8.25)
        self.assertEqual(r["fnb_tax"], 8.25)
        self.assertEqual(r["tax"], 16.50)

    def test_stacked_discounts_cannot_go_negative(self):
        r = self._settle(room_subtotal=0.0, fnb_subtotal=10.0, tax_rate=0.1,
                         discount_pct=0.80, tier_pct=0.50)
        # 80% + 50% would exceed the spend; the F&B base floors at zero.
        self.assertEqual(r["fnb_discount_code"], 8.0)
        self.assertEqual(r["fnb_tier"], 5.0)
        self.assertGreaterEqual(r["subtotal"], 0.0)
        self.assertGreaterEqual(r["fnb_tax"], 0.0)

    def test_zero_night_stay_with_only_fnb(self):
        r = self._settle(room_subtotal=0.0, fnb_subtotal=50.0, tax_rate=0.10,
                         discount_pct=0.0, tier_pct=0.0)
        self.assertEqual(r["room_total"], 0.0)
        self.assertEqual(r["subtotal"], 50.0)
        self.assertEqual(r["tax"], 5.0)


class SettingsTests(unittest.TestCase):
    """HotelSettings rows are admin-editable free text, so reads must never raise."""

    def test_tax_rate_is_read_as_a_fraction(self):
        with mock.patch.object(app, "get_setting", return_value="0.0825"):
            self.assertAlmostEqual(app.get_tax_rate(), 0.0825)

    def test_tax_rate_falls_back_to_the_default(self):
        with mock.patch.object(app, "get_setting", return_value=None):
            self.assertAlmostEqual(app.get_tax_rate(), 0.13)

    def test_garbage_tax_rate_falls_back_instead_of_raising(self):
        # A typo in the settings table must not be able to break check-out.
        with mock.patch.object(app, "get_setting", return_value="not-a-number"):
            self.assertAlmostEqual(app.get_tax_rate(), 0.13)

    def test_blank_tax_rate_falls_back(self):
        with mock.patch.object(app, "get_setting", return_value="   "):
            self.assertAlmostEqual(app.get_tax_rate(), 0.13)

    def test_garbage_price_factor_falls_back(self):
        with mock.patch.object(app, "get_setting", return_value="high"):
            self.assertAlmostEqual(app.get_peak_factor(), 1.20)
            self.assertAlmostEqual(app.get_offpeak_factor(), 0.90)

    def test_garbage_loyalty_settings_fall_back(self):
        with mock.patch.object(app, "get_setting", return_value="lots"):
            self.assertEqual(app.get_loyalty_points_per_night(), 100)
            self.assertEqual(app.get_loyalty_accrual_points_per_unit(),
                             0.5)

    def test_expiration_settings_are_gone(self):
        # Migration 025 removed a knob that was editable on screen but read by nothing,
        # so points never expired. The reader and the constant must stay deleted: leaving
        # either behind would restore a control that promises expiry the app never did.
        self.assertFalse(hasattr(app, "get_loyalty_expiration_days"),
                         "get_loyalty_expiration_days() must not come back")
        self.assertFalse(hasattr(app, "LOYALTY_EXPIRATION_DAYS"),
                         "LOYALTY_EXPIRATION_DAYS must not come back")
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for gone in ("loyalty_expiration_days", "expiration_days"):
            self.assertNotIn(gone, source,
                             f"{gone!r} is still referenced by the app")

    def test_numeric_strings_are_accepted(self):
        # Admins type whole numbers where a float is expected.
        with mock.patch.object(app, "get_setting", return_value="250"):
            self.assertEqual(app.get_loyalty_points_per_night(), 250)

    def test_accrual_rate_keeps_its_fraction(self):
        # The regression: the order rate is a float, and reading it through an int
        # helper turned 0.5 into 0, which would have paid nothing for every order.
        with mock.patch.object(app, "get_setting", return_value="0.5"):
            self.assertEqual(app.get_loyalty_accrual_points_per_unit(), 0.5)

    def test_order_rate_is_read_as_a_float(self):
        # "0.5" must not come back as 0.5 -> int(0.5) == 0. The type is load-bearing.
        with mock.patch.object(app, "get_setting", return_value="0.75"):
            value = app.get_loyalty_accrual_points_per_unit()
            self.assertIsInstance(value, float)
            self.assertEqual(value, 0.75)

    def test_blank_accrual_rate_falls_back_instead_of_paying_nothing(self):
        # A blank setting must fall back to the seeded 0.5. Falling back to 0 would mean
        # a typo in HotelSettings silently switched the whole order programme off.
        with mock.patch.object(app, "get_setting", return_value="   "):
            self.assertEqual(app.get_loyalty_accrual_points_per_unit(),
                             0.5)
        self.assertGreater(0.5, 0)

    def test_order_rate_is_below_the_room_rate(self):
        # The calibration: 100 points on a $120 Standard night is 0.83 pts/$, so the order
        # rate has to sit below it. At the old 3 pts/$, a guest earned 3.6x more per
        # dollar ordering than for the room they slept in.
        order, room = app.points_per_dollar_order_vs_room(
            order_rate=0.5,
            per_night=100,
            nightly_rate=120.0)
        self.assertLess(order, room, "ordering must not out-earn the room")

    def test_order_rate_above_the_room_rate_is_visible(self):
        # The inversion itself, so a future edit to either default fails here. The helper
        # returns the two rates rather than a bool because the settings screen shows them
        # side by side; a boolean would throw away the numbers that make it obvious.
        order, room = app.points_per_dollar_order_vs_room(
            order_rate=3, per_night=100, nightly_rate=120.0)
        self.assertGreater(order, room, "3 pts/$ does out-earn a 100-point night on $120")

    def test_room_rate_uses_the_seeded_standard_rate_by_default(self):
        # Pinned to the seed on purpose: a database that has re-priced Standard must not
        # silently move the calibration the settings screen is judged against.
        _order, room = app.points_per_dollar_order_vs_room()
        expected = 100 / app.DEFAULT_ROOM_TYPE_RATES["Standard"]
        self.assertAlmostEqual(room, expected)

    def test_an_unknown_room_rate_does_not_invert_the_calibration(self):
        # A $0 rate would divide to a divide-by-zero, not a false alarm.
        _order, room = app.points_per_dollar_order_vs_room(nightly_rate=0.0)
        self.assertEqual(room, 0.0)


class AvailabilityOverlapTests(unittest.TestCase):
    def test_touching_stays_do_not_overlap(self):
        # A guest leaving on the 4th frees the room for one arriving on the 4th.
        self.assertFalse(app.stays_overlap(date(2026, 3, 1), date(2026, 3, 4),
                                            date(2026, 3, 4), date(2026, 3, 7)))

    def test_overlapping_window_is_blocked(self):
        self.assertTrue(app.stays_overlap(date(2026, 3, 1), date(2026, 3, 5),
                                           date(2026, 3, 4), date(2026, 3, 7)))

    def test_enclosing_window_is_blocked(self):
        self.assertTrue(app.stays_overlap(date(2026, 3, 1), date(2026, 3, 10),
                                           date(2026, 3, 4), date(2026, 3, 5)))
        self.assertTrue(app.stays_overlap(date(2026, 3, 4), date(2026, 3, 5),
                                           date(2026, 3, 1), date(2026, 3, 10)))

    def test_window_fully_inside_a_stay_is_blocked(self):
        self.assertTrue(app.stays_overlap(date(2026, 3, 1), date(2026, 3, 10),
                                           date(2026, 3, 4), date(2026, 3, 5)))

    def test_disjoint_stays_are_free(self):
        self.assertFalse(app.stays_overlap(date(2026, 3, 1), date(2026, 3, 4),
                                            date(2026, 4, 1), date(2026, 4, 4)))

    def test_partial_day_bounds_use_calendar_dates(self):
        # Departing late on the 4th still frees the night of the 4th, so an arrival
        # that morning is fine; an arrival the night before is not.
        self.assertFalse(app.stays_overlap(datetime(2026, 3, 1, 15, 0), datetime(2026, 3, 4, 11, 0),
                                            datetime(2026, 3, 4, 15, 0), datetime(2026, 3, 6, 11, 0)))
        self.assertTrue(app.stays_overlap(datetime(2026, 3, 1, 15, 0), datetime(2026, 3, 4, 11, 0),
                                           datetime(2026, 3, 3, 15, 0), datetime(2026, 3, 6, 11, 0)))

    def test_missing_bounds_never_overlap(self):
        self.assertFalse(app.stays_overlap(None, date(2026, 3, 4), date(2026, 3, 1), date(2026, 3, 4)))
        self.assertFalse(app.stays_overlap(date(2026, 3, 1), None, date(2026, 3, 1), date(2026, 3, 4)))
        self.assertFalse(app.stays_overlap(None, None, None, None))

    def test_empty_or_reversed_window_blocks_nothing(self):
        # A same-day or data-entry-slipped window occupies no nights, so it must not
        # make a room look occupied.
        self.assertFalse(app.stays_overlap(date(2026, 3, 1), date(2026, 3, 1),
                                            date(2026, 3, 1), date(2026, 3, 4)))
        self.assertFalse(app.stays_overlap(date(2026, 3, 4), date(2026, 3, 1),
                                            date(2026, 3, 1), date(2026, 3, 4)))

    def test_agrees_with_stay_nights_on_touching_boundaries(self):
        # A one-night stay starting the day the previous one ends must be free.
        for gap in range(0, 4):
            end = date(2026, 3, 1 + gap)
            start = date(2026, 3, 4)
            self.assertFalse(
                app.stays_overlap(date(2026, 3, 1), end, start, date(2026, 3, 6)),
                f"unexpected overlap with a stay ending {end}",
            )
            self.assertEqual(app.stay_nights(start, date(2026, 3, 6)), 2)


if __name__ == "__main__":
    unittest.main()
