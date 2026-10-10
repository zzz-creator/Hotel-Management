# type: ignore
"""Unit tests for the booking desk money rules and checkout credit.

Everything here is a pure function or a thin DB wrapper, so the whole file runs without
SQL Server. The rules that matter are the ones a guest could be overcharged by, so the
tests pin the exact arithmetic and the sign/scope of every money movement.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
from patch_main import patch_main
from hotel import session


class BookingQuoteTests(unittest.TestCase):
    """booking_quote() must match the check-out folio: room subtotal + its tax."""

    def test_quote_is_subtotal_plus_tax(self):
        q = app.booking_quote(3, 200.0, 0.13)
        self.assertEqual(q["subtotal"], 600.0)
        self.assertEqual(q["tax"], 78.0)
        self.assertEqual(q["total"], 678.0)

    def test_single_night(self):
        q = app.booking_quote(1, 120.0, 0.10)
        self.assertEqual(q["subtotal"], 120.0)
        self.assertEqual(q["tax"], 12.0)
        self.assertEqual(q["total"], 132.0)

    def test_zero_tax_is_supported(self):
        q = app.booking_quote(4, 100.0, 0.0)
        self.assertEqual(q["tax"], 0.0)
        self.assertEqual(q["total"], 400.0)

    def test_money_is_rounded_to_cents(self):
        # 3 nights at 33.33 with 8.25% tax must not leak binary float noise.
        q = app.booking_quote(3, 33.33, 0.0825)
        self.assertEqual(q["subtotal"], 99.99)
        self.assertEqual(q["total"], round(99.99 * 1.0825, 2))
        for value in (q["subtotal"], q["tax"], q["total"]):
            self.assertEqual(value, round(value, 2))

    def test_negative_inputs_are_clamped_not_rejected(self):
        # A mistyped rate or nights must never produce a negative quote the guest sees.
        q = app.booking_quote(-2, -50.0, -0.1)
        self.assertEqual(q["nights"], 0)
        self.assertEqual(q["subtotal"], 0.0)
        self.assertEqual(q["total"], 0.0)

    def test_none_inputs_do_not_raise(self):
        q = app.booking_quote(None, None, None)
        self.assertEqual(q["total"], 0.0)


class BookingPaymentOptionTests(unittest.TestCase):
    def test_no_pay_at_checkout_option_is_offered(self):
        opts = app.booking_payment_options(3, 200.0, 0.13)
        self.assertTrue(all(o[2] is not None for o in opts),
                        "a deposit is required to secure the room, so no option may be free")
        self.assertFalse(any("No prepayment" in o[1] for o in opts))

    def test_deposit_is_the_first_option(self):
        opts = app.booking_payment_options(3, 200.0, 0.13)
        self.assertEqual(opts[0][0], 1)
        self.assertEqual(opts[0][2], app.PAYMENT_KIND_DEPOSIT)

    def test_exactly_two_options_are_offered(self):
        opts = app.booking_payment_options(3, 200.0, 0.13)
        self.assertEqual(len(opts), 2)
        self.assertEqual([o[2] for o in opts],
                         [app.PAYMENT_KIND_DEPOSIT, app.PAYMENT_KIND_PREPAYMENT])

    def test_deposit_is_one_night_including_its_tax(self):
        opts = app.booking_payment_options(3, 200.0, 0.13)
        deposit = next(o for o in opts if o[2] == app.PAYMENT_KIND_DEPOSIT)
        # One night = 200 + 26 tax.
        self.assertEqual(deposit[3], 226.0)
        self.assertIn("226.00", deposit[1])
        self.assertIn("452.00", deposit[1], "the label should show the remaining balance")

    def test_prepay_covers_the_whole_quote(self):
        opts = app.booking_payment_options(3, 200.0, 0.13)
        prepay = next(o for o in opts if o[2] == app.PAYMENT_KIND_PREPAYMENT)
        self.assertEqual(prepay[3], app.booking_quote(3, 200.0, 0.13)["total"])

    def test_deposit_is_capped_at_the_stay_length(self):
        # A one-night stay must not be asked for a "one night deposit" plus more.
        opts = app.booking_payment_options(1, 200.0, 0.13)
        deposit = next(o for o in opts if o[2] == app.PAYMENT_KIND_DEPOSIT)
        self.assertEqual(deposit[3], 226.0)
        self.assertIn("0.00 at check-out", deposit[1])

    def test_zero_night_stay_falls_back_to_deposit_only(self):
        opts = app.booking_payment_options(0, 200.0, 0.13)
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0][0], 1, "a lone option must still be numbered 1")
        self.assertEqual(opts[0][2], app.PAYMENT_KIND_DEPOSIT)

    def test_a_free_stay_still_records_a_payment_kind(self):
        # A $0 quote cannot offer a pay-in-full, but the booking must still be findable
        # by reference, so it keeps the deposit kind at a $0 amount.
        opts = app.booking_payment_options(0, 0.0, 0.13)
        self.assertEqual(len(opts), 1)
        self.assertIsNotNone(opts[0][2])
        self.assertEqual(opts[0][3], 0.0)

    def test_zero_rated_room_still_offers_a_usable_option(self):
        # nights >= 1 but the rate is $0: the wizard must still be able to ask for
        # a choice, or ask_number(minimum=1, maximum=0) would loop forever.
        opts = app.booking_payment_options(3, 0.0, 0.13)
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0][0], 1)
        self.assertEqual(opts[0][3], 0.0)

    def test_choices_are_contiguous_from_one(self):
        for nights, rate in ((0, 200.0), (0, 0.0), (1, 200.0), (5, 99.5)):
            opts = app.booking_payment_options(nights, rate, 0.13)
            self.assertEqual([o[0] for o in opts], list(range(1, len(opts) + 1)),
                             f"nights={nights} rate={rate}")


class SettleWithPrepaymentTests(unittest.TestCase):
    def test_credit_reduces_the_balance(self):
        s = app.settle_with_prepayment(678.0, 226.0)
        self.assertEqual(s["credit_applied"], 226.0)
        self.assertEqual(s["balance_due"], 452.0)
        self.assertEqual(s["credit_unused"], 0.0)

    def test_credit_never_exceeds_the_bill(self):
        # Guest prepaid a full stay but only ordered a coffee. They must not be handed
        # a negative bill; the surplus has to come back as an unused credit.
        s = app.settle_with_prepayment(50.0, 678.0)
        self.assertEqual(s["credit_applied"], 50.0)
        self.assertEqual(s["balance_due"], 0.0)
        self.assertEqual(s["credit_unused"], 628.0)

    def test_exact_credit_leaves_nothing_due(self):
        s = app.settle_with_prepayment(200.0, 200.0)
        self.assertEqual(s["balance_due"], 0.0)
        self.assertEqual(s["credit_unused"], 0.0)

    def test_no_credit_leaves_the_full_bill(self):
        s = app.settle_with_prepayment(300.0, 0.0)
        self.assertEqual(s["credit_applied"], 0.0)
        self.assertEqual(s["balance_due"], 300.0)

    def test_rate_rise_turns_prepayment_into_a_balance(self):
        # The quote is an estimate: if the rate went up after booking, the guest owes
        # the difference rather than being credited below zero.
        s = app.settle_with_prepayment(900.0, 678.0)
        self.assertEqual(s["balance_due"], 222.0)

    def test_rounding_is_stable_to_cents(self):
        s = app.settle_with_prepayment(0.1 + 0.2, 0.3)
        self.assertEqual(s["balance_due"], 0.0)

    def test_negative_balance_is_impossible(self):
        s = app.settle_with_prepayment(-500.0, 100.0)
        self.assertEqual(s["total"], 0.0)
        self.assertEqual(s["balance_due"], 0.0)
        self.assertEqual(s["credit_applied"], 0.0)

    def test_negative_credit_is_ignored(self):
        # Refund rows are negative, but they must never become a payment at check-out.
        s = app.settle_with_prepayment(500.0, -50.0)
        self.assertEqual(s["credit_applied"], 0.0)
        self.assertEqual(s["balance_due"], 500.0)


class BookingRefundPolicyTests(unittest.TestCase):
    """The policy must be stated at booking time, and must match refund_decision()."""

    def test_deadline_is_cutoff_days_before_checkin(self):
        p = app.booking_refund_policy(date(2026, 11, 24), 7)
        self.assertEqual(p["cutoff"], 7)
        self.assertEqual(p["deadline"], date(2026, 11, 17))

    def test_zero_cutoff_still_promises_a_day_that_actually_refunds(self):
        # cutoff 0 must not put the deadline ON the arrival date, because the arrival
        # date is refused -- the guest must not be promised a refund that never happens.
        p = app.booking_refund_policy(date(2026, 11, 24), 0)
        self.assertEqual(p["deadline"], date(2026, 11, 23))
        self.assertEqual(p["refundable_days"], 1)
        self.assertEqual(app.refund_decision(1, 0, 100.0)["action"], "refund")

    def test_negative_cutoff_is_clamped_like_refund_decision(self):
        self.assertEqual(app.booking_refund_policy(date(2026, 11, 24), -5)["cutoff"], 0)

    def test_summary_states_both_outcomes_and_the_deadline(self):
        summary = app.booking_refund_policy(date(2026, 11, 24), 7)["summary"].lower()
        self.assertIn("2026-11-17", summary, "the actual deadline must be shown")
        self.assertIn("full refund", summary)
        self.assertIn("forfeited", summary)

    def test_summary_names_the_exact_amount_at_risk(self):
        summary = app.booking_refund_policy(date(2026, 11, 24), 7,
                                            amount_charged=135.6)["summary"]
        self.assertIn("$135.60", summary)

    def test_summary_says_online_cancellation_ends_at_checkin(self):
        summary = app.booking_refund_policy(date(2026, 11, 24), 7)["summary"].lower()
        self.assertIn("check-in date onwards", summary)
        self.assertIn("front desk", summary)

    def test_stated_deadline_is_exactly_the_last_refundable_day(self):
        # Walk every day from the stated deadline to the arrival date. The wording is
        # only honest if the deadline is the LAST day that refunds, every earlier day
        # before it refunds too, and the arrival date itself is refused.
        check_in = date(2026, 11, 24)
        for cutoff in (0, 1, 2, 3, 7, 14):
            deadline = app.booking_refund_policy(check_in, cutoff)["deadline"]
            day = deadline
            while day <= check_in:
                days_until = (check_in - day).days
                action = app.refund_decision(days_until, cutoff, 100.0)["action"]
                if day == deadline:
                    self.assertEqual(action, "refund",
                                     f"cutoff={cutoff}: the stated deadline {deadline} must refund")
                elif days_until > 0:
                    self.assertEqual(action, "forfeit",
                                     f"cutoff={cutoff}: {day} is past the deadline but pre-arrival")
                else:
                    self.assertEqual(action, "refused",
                                     f"cutoff={cutoff}: the arrival date must be refused")
                day += timedelta(days=1)


class RefundDecisionTests(unittest.TestCase):
    def test_full_refund_outside_the_cutoff(self):
        d = app.refund_decision(30, 7, 226.0)
        self.assertEqual(d["action"], "refund")
        self.assertEqual(d["kind"], app.PAYMENT_KIND_REFUND)
        self.assertEqual(d["amount"], -226.0, "a refund is a negative movement")

    def test_deposit_forfeited_inside_the_cutoff(self):
        d = app.refund_decision(2, 7, 226.0)
        self.assertEqual(d["action"], "forfeit")
        self.assertEqual(d["kind"], app.PAYMENT_KIND_FORFEIT)
        self.assertEqual(d["amount"], -226.0, "a forfeit also zeroes the stay's net position")

    def test_exactly_on_the_cutoff_boundary_still_refunds(self):
        # "At least N days ahead" is a refund, which is the guest-friendly reading.
        self.assertEqual(app.refund_decision(7, 7, 100.0)["action"], "refund")
        self.assertEqual(app.refund_decision(6, 7, 100.0)["action"], "forfeit")

    def test_stay_already_started_cannot_be_cancelled(self):
        d = app.refund_decision(-1, 7, 226.0)
        self.assertEqual(d["action"], "refused")
        self.assertEqual(d["amount"], 0.0)
        self.assertIsNone(d["kind"])

    def test_checking_in_today_cannot_be_cancelled(self):
        # days == 0 is the arrival date, not "still cancellable": cancel_booking()
        # DELETEs the reservation, so allowing it would wipe an in-house stay.
        d = app.refund_decision(0, 7, 226.0)
        self.assertEqual(d["action"], "refused")
        self.assertEqual(d["amount"], 0.0)
        self.assertIsNone(d["kind"])

    def test_nothing_paid_means_nothing_to_settle(self):
        d = app.refund_decision(30, 7, 0.0)
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["amount"], 0.0)

    def test_zero_cutoff_means_always_refundable_before_checkin(self):
        # A 0-day window leaves no exclusion zone, so any cancellation that happens
        # before check-in is free.
        self.assertEqual(app.refund_decision(5, 0, 100.0)["action"], "refund")
        # ...but a zero cutoff is not a licence to cancel on the arrival date.
        self.assertEqual(app.refund_decision(0, 0, 100.0)["action"], "refused")

    def test_both_outcomes_net_the_stay_back_to_zero(self):
        for days in (30, 2):
            d = app.refund_decision(days, 7, 226.0)
            self.assertEqual(226.0 + d["amount"], 0.0)

    def test_negative_cutoff_is_clamped_to_zero(self):
        # Clamping to 0 means "no exclusion zone", not "always forfeit".
        self.assertEqual(app.refund_decision(3, -5, 100.0)["action"], "refund")


class _FakeConn:
    """Minimal connection stand-in recording statements, for the DB-wrapper tests."""

    def __init__(self, log, rows=None, sums=None, raise_missing_column=False,
                 room_types=None, cards=None):
        self.log = log
        self.rows = rows or []
        self.sums = sums or []
        self.raise_missing_column = raise_missing_column
        self.room_types = room_types or []
        self.cards = cards or []
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.log = conn.log
        self.rows = []
        self.rowcount = -1

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        self.rows = []
        upper = sql.lstrip().upper()
        if self.conn.raise_missing_column and "APPLIEDAMOUNT" in upper:
            raise RuntimeError(self.conn.raise_missing_column
                               if isinstance(self.conn.raise_missing_column, str)
                               else "Invalid column name 'AppliedAmount'.")
        if upper.startswith("INSERT") and "OUTPUT INSERTED.PAYMENTID" in upper:
            self.rows = [(77,)]
        elif upper.startswith("SELECT ROOMTYPE FROM ROOMS"):
            self.rows = [(self.conn.room_types[0],)] if self.conn.room_types else []
        elif upper.startswith("SELECT TOP 1 CARDLAST4"):
            self.rows = [self.conn.cards[0]] if self.conn.cards else []
        elif upper.startswith("SELECT CHECKINDATE, CHECKOUTDATE"):
            # The live reservation for this room. rows=[] means the room is free.
            self.rows = list(self.conn.rows)
        elif upper.startswith("SELECT ISNULL(SUM(AMOUNT - APPLIEDAMOUNT)") or \
                upper.startswith("SELECT ISNULL(SUM(AMOUNT)"):
            self.rows = [(self.conn.sums[0] if self.conn.sums else 0.0,)]
        elif upper.startswith("SELECT PAYMENTID, AMOUNT - APPLIEDAMOUNT"):
            self.rows = list(self.conn.rows)
        elif upper.startswith("UPDATE RESERVATIONPAYMENTS SET ISAPPLIED"):
            self.rowcount = len(self.conn.rows) or 2
        elif upper.startswith("UPDATE RESERVATIONPAYMENTS SET APPLIEDAMOUNT"):
            self.rowcount = 1
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class BookingPaymentRowTests(unittest.TestCase):
    def test_payment_is_written_with_the_card_last_four_only(self):
        log = []
        with patch_main("get_connection", return_value=_FakeConn(log)), \
             patch_main("LAST_CARD_DIGITS", "4242"):
            pid = app.record_booking_payment("9012", "BK-ABC123", date(2026, 3, 1),
                                             app.PAYMENT_KIND_DEPOSIT, 226.0, nights_covered=1)
        self.assertEqual(pid, 77)
        sql, params = log[0]
        self.assertIn("INSERT INTO ReservationPayments", sql)
        self.assertIn("4242", params)
        # The full PAN must never reach the database or the log.
        self.assertNotIn("1234567890123456", str(params))

    def test_no_card_leaves_cardlast4_null(self):
        log = []
        with patch_main("get_connection", return_value=_FakeConn(log)), \
             patch_main("LAST_CARD_DIGITS", None):
            app.record_booking_payment("9012", "BK-ABC123", date(2026, 3, 1),
                                       app.PAYMENT_KIND_PREPAYMENT, 678.0)
        self.assertIsNone(log[0][1][6])

    def test_refund_is_stored_negative(self):
        log = []
        with patch_main("get_connection", return_value=_FakeConn(log)), \
             patch_main("LAST_CARD_DIGITS", None):
            app.record_booking_payment("9012", "BK-ABC123", date(2026, 3, 1),
                                       app.PAYMENT_KIND_REFUND, -226.0)
        # A refund first looks up the original charge's card, so the INSERT is not log[0].
        insert = [p for s, p in log if s.upper().startswith("INSERT INTO RESERVATIONPAYMENTS")]
        self.assertEqual(len(insert), 1)
        self.assertLess(insert[0][4], 0, "a refund must reduce the stay's net paid total")

    def test_caller_transaction_is_used_and_not_committed_here(self):
        log = []
        conn = _FakeConn(log)
        with patch_main("LAST_CARD_DIGITS", None):
            app.record_booking_payment("9012", "BK-ABC123", date(2026, 3, 1),
                                       app.PAYMENT_KIND_DEPOSIT, 10.0, conn=conn)
        self.assertEqual(len(log), 1)
        self.assertFalse(conn.committed, "the caller owns the commit")

    def test_non_numeric_amount_is_refused_without_touching_the_database(self):
        log = []
        with patch_main("get_connection", return_value=_FakeConn(log)):
            self.assertIsNone(app.record_booking_payment("9012", "BK-ABC123", date(2026, 3, 1),
                                                         app.PAYMENT_KIND_DEPOSIT, "free"))
        self.assertEqual(log, [], "a bad amount must not insert a row")

    def test_payment_scopes_to_the_stay_checkin_date(self):
        # A room is re-let after every check-out, so the credit must be tied to the
        # exact stay that paid it, never to the room alone.
        log = []
        saved = session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None
        try:
            with patch_main("get_connection", return_value=_FakeConn(log)):
                app.get_outstanding_booking_credit("9012", date(2026, 3, 1))
        finally:
            session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = saved
        sql, params = log[0]
        self.assertIn("StayCheckIn = ?", sql)
        # Credit is a per-row REMAINDER, not "rows not yet used", so a partly-consumed
        # row still contributes what is left of it.
        self.assertIn("Amount - AppliedAmount", sql)
        self.assertEqual(params, ("9012", date(2026, 3, 1)))


class BookingRefUniquenessTests(unittest.TestCase):
    """A reference must be unique by database guarantee, and a collision is a retry."""

    def test_a_duplicate_key_is_recognised(self):
        for error in (
            RuntimeError("2627"),                                   # bare number
            Exception("('23000', \"[SQL Server]Violation of PRIMARY KEY CONSTRAINT... (2627)\")"),
            Exception("('23000', 2627, '[SQL Server]Cannot insert duplicate key row.')"),
        ):
            self.assertTrue(app._is_duplicate_key_error(error), error)

    def test_sqlstate_23000_is_recognised(self):
        self.assertTrue(app._is_duplicate_key_error(Exception(('23000', 'Cannot insert duplicate key row.'))))
        self.assertTrue(app._is_duplicate_key_error(Exception(('23505', 'unique violation'))))

    def test_other_integrity_violations_are_not_mistaken_for_a_duplicate_key(self):
        # 23503 is a FOREIGN KEY violation and 23512 a CHECK violation. Both are class 23
        # but neither means "this reference is taken", and treating them as such would
        # retry a doomed insert and then report a misleading error to the guest.
        for error in (
            Exception(('23000', 23503, 'The INSERT statement conflicted with the FOREIGN KEY constraint')),
            Exception(('23000', 23512, "Conflict with the CK_ReservationPayments_Kind constraint")),
            Exception("('42S22', 207, \"Invalid column name 'Kind'.\")"),
        ):
            self.assertFalse(app._is_duplicate_key_error(error), error)

    def test_an_ordinary_error_is_not_a_duplicate_key(self):
        for error in (
            RuntimeError("Invalid column name 'X'."),
            Exception("The connection is busy."),
        ):
            self.assertFalse(app._is_duplicate_key_error(error), error)

    def test_a_taken_reference_raises_rather_than_returning_none(self):
        # The caller has to be able to tell "mint another" from "the payment failed".
        log = []
        conn = _FakeConn(log)
        with patch_main("record_booking_payment",
                               side_effect=app.BookingRefTaken("BK-AAAAAA")):
            self.assertIsNone(app._write_booking_charge(conn, "9012", "BK-AAAAAA",
                                                        date(2026, 3, 1), "Deposit", 100.0, 1))

    def test_a_taken_reference_is_retried_with_a_fresh_one(self):
        calls = []

        def _fake(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 1:
                raise app.BookingRefTaken(args[1])
            return 5

        with patch_main("record_booking_payment", side_effect=_fake), \
             patch_main("new_booking_ref", return_value="BK-BBBBBB"):
            used = app._write_booking_charge(_FakeConn([]), "9012", "BK-AAAAAA",
                                              date(2026, 3, 1), "Deposit", 100.0, 1)
        self.assertEqual(calls, ["BK-AAAAAA", "BK-BBBBBB"])
        self.assertEqual(used, "BK-BBBBBB")

    def test_it_gives_up_rather_than_spinning_forever(self):
        with patch_main("record_booking_payment",
                               side_effect=app.BookingRefTaken("BK-AAAAAA")), \
             patch_main("new_booking_ref", return_value="BK-BBBBBB"):
            self.assertIsNone(app._write_booking_charge(
                _FakeConn([]), "9012", "BK-AAAAAA", date(2026, 3, 1), "Deposit", 100.0, 1))

    def test_the_first_attempt_reuses_the_reference_already_shown_to_the_guest(self):
        # book_room() mints the reference before the transaction, so the retry must not
        # silently swap in a different one on the first pass.
        with patch_main("record_booking_payment", return_value=5):
            used = app._write_booking_charge(_FakeConn([]), "9012", "BK-AAAAAA",
                                              date(2026, 3, 1), "Deposit", 100.0, 1)
        self.assertEqual(used, "BK-AAAAAA")

    def test_a_failed_write_is_not_retried_as_if_the_reference_were_taken(self):
        with patch_main("record_booking_payment", return_value=None):
            self.assertIsNone(app._write_booking_charge(
                _FakeConn([]), "9012", "BK-AAAAAA", date(2026, 3, 1), "Deposit", 100.0, 1))


class BookingRefCardAttributionTests(unittest.TestCase):
    """A reversal must name the card the booking was charged to, not the last one used."""

    def setUp(self):
        self._saved = session.LAST_CARD_DIGITS
        session.LAST_CARD_DIGITS = "9999"
        self.addCleanup(lambda: setattr(session, "LAST_CARD_DIGITS", self._saved))

    def _recorded_card(self, kind):
        log = []
        conn = _FakeConn(log, cards=[("4111",)])
        app.record_booking_payment("9012", "BK-AAAAAA", date(2026, 3, 1), kind, -100.0,
                                   conn=conn)
        insert = [p for s, p in log if s.upper().startswith("INSERT INTO RESERVATIONPAYMENTS")]
        return insert[0][6] if insert else "<no insert>"

    def test_a_refund_names_the_original_charges_card(self):
        self.assertEqual(self._recorded_card(app.PAYMENT_KIND_REFUND), "4111")

    def test_a_forfeit_names_the_original_charges_card(self):
        self.assertEqual(self._recorded_card(app.PAYMENT_KIND_FORFEIT), "4111")

    def test_a_new_charge_uses_the_card_just_authorised(self):
        self.assertEqual(self._recorded_card(app.PAYMENT_KIND_DEPOSIT), "9999")

    def test_the_reversal_never_borrows_the_process_wide_last_card(self):
        # A cancellation prompts for no card, so LAST_CARD_DIGITS is still whatever an
        # unrelated guest paid with. It must not leak into the refund row.
        self.assertNotEqual(self._recorded_card(app.PAYMENT_KIND_REFUND), "9999")

    def test_no_original_charge_leaves_the_reversal_card_unknown(self):
        log = []
        app.record_booking_payment("9012", "BK-AAAAAA", date(2026, 3, 1),
                                   app.PAYMENT_KIND_REFUND, -100.0, conn=_FakeConn(log, cards=[]))
        insert = [p for s, p in log if s.upper().startswith("INSERT INTO RESERVATIONPAYMENTS")]
        self.assertIsNone(insert[0][6])


class RoomTypeRevalidationTests(unittest.TestCase):
    """The room's category must be re-read inside the booking transaction."""

    def test_a_matching_type_is_allowed(self):
        conn = _FakeConn([], room_types=["Deluxe"], rows=[])
        ok, _reason = app._book_reservation_in_conn(
            conn, "9012", "Doe", "Jane", date(2026, 3, 1), date(2026, 3, 3), room_type="Deluxe")
        self.assertTrue(ok)

    def test_a_mismatched_type_refuses_the_claim(self):
        # A guest who asked for a Deluxe must not be handed a Standard because the room was
        # re-typed after the availability search ran.
        conn = _FakeConn([], room_types=["Standard"], rows=[])
        ok, reason = app._book_reservation_in_conn(
            conn, "9012", "Doe", "Jane", date(2026, 3, 1), date(2026, 3, 3), room_type="Deluxe")
        self.assertFalse(ok)
        self.assertIn("Standard", reason)
        self.assertIn("Deluxe", reason)

    def test_comparison_ignores_case_and_padding(self):
        conn = _FakeConn([], room_types=["deluxe "], rows=[])
        ok, _reason = app._book_reservation_in_conn(
            conn, "9012", "Doe", "Jane", date(2026, 3, 1), date(2026, 3, 3), room_type="Deluxe")
        self.assertTrue(ok)

    def test_an_untracked_room_is_not_refused(self):
        # No Rooms row means "cannot verify", not "wrong type". Refusing here would break
        # front-desk and legacy rooms that predate the Rooms table.
        conn = _FakeConn([], room_types=[], rows=[])
        ok, _reason = app._book_reservation_in_conn(
            conn, "9012", "Doe", "Jane", date(2026, 3, 1), date(2026, 3, 3), room_type="Deluxe")
        self.assertTrue(ok)

    def test_no_expected_type_means_no_type_check(self):
        conn = _FakeConn([], room_types=["Penthouse"], rows=[])
        ok, _reason = app._book_reservation_in_conn(
            conn, "9012", "Doe", "Jane", date(2026, 3, 1), date(2026, 3, 3))
        self.assertTrue(ok)

    def test_the_type_is_read_on_the_callers_connection(self):
        # get_room_type() opens its OWN connection, which would read outside the open
        # transaction and could see a stale or locked value.
        conn = _FakeConn([], room_types=["Deluxe"], rows=[])
        app._book_reservation_in_conn(conn, "9012", "Doe", "Jane",
                                      date(2026, 3, 1), date(2026, 3, 3), room_type="Deluxe")
        self.assertTrue(any("FROM Rooms" in s for s, _ in conn.log))


class AllocateBookingCreditTests(unittest.TestCase):
    """The split of one bill's credit across a stay's payment rows.

    This is the behaviour the pre-020 boolean IsApplied could not express: a booking quote
    is an estimate, so a bill can legitimately be SMALLER than the prepayment.
    """

    def test_a_single_row_is_fully_consumed_by_an_equal_bill(self):
        self.assertEqual(app.allocate_booking_credit([(1, 300.0)], 300.0), [(1, 300.0)])

    def test_a_bill_smaller_than_one_prepayment_consumes_only_part_of_it(self):
        # The exact case the old code got wrong: it marked the whole $300 row applied
        # against a $250 bill and reported $50 unused to the guest.
        self.assertEqual(app.allocate_booking_credit([(1, 300.0)], 250.0), [(1, 250.0)])

    def test_credit_is_consumed_oldest_row_first(self):
        self.assertEqual(
            app.allocate_booking_credit([(1, 50.0), (2, 200.0)], 120.0),
            [(1, 50.0), (2, 70.0)],
        )

    def test_never_allocates_more_than_the_credit_asked_for(self):
        alloc = app.allocate_booking_credit([(1, 500.0), (2, 500.0)], 100.0)
        self.assertEqual(round(sum(a for _, a in alloc), 2), 100.0)

    def test_never_exceeds_the_rows_available(self):
        alloc = app.allocate_booking_credit([(1, 40.0)], 100.0)
        self.assertEqual(alloc, [(1, 40.0)])

    def test_negative_and_zero_remainders_are_skipped(self):
        # A Refund row's remainder is negative; it must never be "consumed" to pay a bill.
        self.assertEqual(app.allocate_booking_credit([(1, -50.0), (2, 0.0), (3, 30.0)], 30.0),
                         [(3, 30.0)])

    def test_no_credit_allocates_nothing(self):
        self.assertEqual(app.allocate_booking_credit([(1, 300.0)], 0.0), [])

    def test_no_rows_allocates_nothing(self):
        self.assertEqual(app.allocate_booking_credit([], 250.0), [])

    def test_decimal_precision_does_not_leak_floating_point_dust(self):
        alloc = app.allocate_booking_credit([(1, 0.1), (2, 0.2)], 0.3)
        self.assertEqual(round(sum(a for _, a in alloc), 2), 0.3)
        for _, amount in alloc:
            self.assertEqual(amount, round(amount, 2))

    def test_a_partial_allocation_still_leaves_the_row_reportable(self):
        # The remainder must remain a real number the guest can be told about, which is
        # what AppliedAmount makes possible.
        rows = {pid: amount for pid, amount in app.allocate_booking_credit([(1, 300.0)], 250.0)}
        self.assertAlmostEqual(300.0 - rows[1], 50.0, places=2)


class BookingCreditApplyTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise the post-020 path; the negative control below re-latches
        # the pre-020 fallback explicitly.
        self._saved = session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None

    def tearDown(self):
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = self._saved

    def test_nothing_is_touched_when_there_is_no_credit(self):
        log = []
        self.assertEqual(app.apply_booking_credit("9012", date(2026, 3, 1), 0.0, 12, _FakeConn(log)), 0)
        self.assertEqual(log, [])

    def test_nothing_is_touched_without_an_invoice_id(self):
        log = []
        self.assertEqual(app.apply_booking_credit("9012", date(2026, 3, 1), 226.0, None, _FakeConn(log)), 0)
        self.assertEqual(log, [])

    def test_consumed_amount_is_recorded_per_row(self):
        log = []
        app.apply_booking_credit("9012", date(2026, 3, 1), 120.0, 12, _FakeConn(log, rows=[(1, 50.0), (2, 200.0)]))
        updates = [p for s, p in log if s.upper().startswith("UPDATE RESERVATIONPAYMENTS SET APPLIEDAMOUNT")]
        self.assertEqual(updates, [(50.0, 50.0, 12, 1), (70.0, 70.0, 12, 2)])

    def test_a_fully_consumed_row_is_flagged_by_the_update_itself(self):
        log = []
        app.apply_booking_credit("9012", date(2026, 3, 1), 50.0, 12, _FakeConn(log, rows=[(1, 50.0)]))
        sql = [s.upper() for s, _ in log if "APPLIEDAMOUNT" in s.upper() and s.upper().startswith("UPDATE")][0]
        self.assertIn("ISAPPLIED = CASE WHEN", sql)
        self.assertIn(">= AMOUNT", sql)

    def test_a_partly_used_row_is_not_marked_fully_applied_by_the_update(self):
        # The CASE must be able to leave IsApplied alone, or a $250-of-$300 row would be
        # flagged spent and the $50 remainder would vanish from the outstanding credit.
        log = []
        app.apply_booking_credit("9012", date(2026, 3, 1), 250.0, 12, _FakeConn(log, rows=[(1, 300.0)]))
        sql = [s.upper() for s, _ in log if "APPLIEDAMOUNT" in s.upper() and s.upper().startswith("UPDATE")][0]
        self.assertIn("ELSE ISAPPLIED", sql)

    def test_legacy_shape_marks_rows_whole(self):
        # Pre-020 there is no AppliedAmount, so check-out must still settle the credit
        # rather than leave it claimable forever.
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = False
        log = []
        touched = app.apply_booking_credit("9012", date(2026, 3, 1), 226.0, 12, _FakeConn(log))
        self.assertEqual(touched, 2)
        sql, params = log[0]
        self.assertIn("IsApplied = 1", sql)
        self.assertIn("AppliedToInvoiceID = ?", sql)
        self.assertEqual(params[0], 12)

    def test_outstanding_credit_counts_only_the_remainder(self):
        log = []
        app.get_outstanding_booking_credit("9012", date(2026, 3, 1), _FakeConn(log, sums=[45.5]))
        sql, params = log[0]
        self.assertIn("SUM(Amount - AppliedAmount)", sql)
        self.assertNotIn("IsApplied", sql)
        self.assertEqual(params, ("9012", date(2026, 3, 1)))


class OutstandingCreditFallbackTests(unittest.TestCase):
    def setUp(self):
        self._saved = session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None

    def tearDown(self):
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = self._saved

    def test_missing_applied_amount_falls_back_to_the_whole_row_sum(self):
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = False
        log = []
        app.get_outstanding_booking_credit("9012", date(2026, 3, 1), _FakeConn(log, sums=[300.0]))
        sql, _params = log[0]
        self.assertIn("IsApplied = 0", sql)

    def test_a_missing_column_error_latches_the_legacy_path(self):
        conn = _FakeConn([], sums=[0.0], raise_missing_column=True)
        app.get_outstanding_booking_credit("9012", date(2026, 3, 1), conn)
        self.assertIs(session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT, False)

    def test_an_unrelated_error_is_not_latched_as_a_missing_column(self):
        # A deadlock must not pin the process to the legacy path for the rest of the run.
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None
        conn = _FakeConn([], sums=[0.0], raise_missing_column="The connection is busy.")
        self.assertEqual(app.get_outstanding_booking_credit("9012", date(2026, 3, 1), conn), 0.0)
        self.assertIsNone(session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT)

    def test_only_a_column_error_latches(self):
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None
        for message, should_latch in (
            ("Invalid column name 'AppliedAmount'.", True),
            ("Invalid object name 'ReservationPayments'.", True),
            ("The connection is busy.", False),
            ("Timeout expired.", False),
        ):
            session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None
            app._set_partial_credit_unsupported(RuntimeError(message))
            if should_latch:
                self.assertIs(session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT, False, message)
            else:
                self.assertIsNone(session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT, message)


class CardInputHardeningTests(unittest.TestCase):
    """A mistyped card must be rejected politely, not raise out of the payment prompt."""

    def test_luhn_rejects_non_numeric_input_without_raising(self):
        for bad in ("abc", "n", "4111-1111-1111-1111", "", None, "4111 1111 1111 1111x"):
            self.assertFalse(app.luhn_check(bad), f"{bad!r} should not validate")

    def test_luhn_still_accepts_a_real_card(self):
        self.assertTrue(app.luhn_check("4111111111111111"))

    def test_bad_card_cancels_payment_and_records_no_digits(self):
        with mock.patch("builtins.input",
                        mock.Mock(side_effect=["nope", "12/2030", "123"])):
            with patch_main("LAST_CARD_DIGITS", "9999"):
                self.assertFalse(app.process_credit_card(10.0))
        self.assertIsNone(session.LAST_CARD_DIGITS,
                          "a declined card must not leave stale digits behind")

    def test_good_card_records_only_last_four(self):
        with mock.patch("builtins.input",
                        mock.Mock(side_effect=["4111111111111111", "12/2030", "123"])):
            self.assertTrue(app.process_credit_card(10.0))
        self.assertEqual(session.LAST_CARD_DIGITS, "1111")

    def test_expiry_must_be_a_real_month(self):
        self.assertTrue(app.validate_expiration_date("12/2030"))
        for bad in ("00/2030", "13/2030", "12/30", "", "nope", "2030-12"):
            self.assertFalse(app.validate_expiration_date(bad), f"{bad!r} should be rejected")


class InvoiceInsertTests(unittest.TestCase):
    """The check-out INSERT must work both before and after migration 018."""

    BASE = ["9012", 600.0, 0.0, 0.0, 78.0, 678.0, 0, 0.0, 452.0,
            600.0, 78.0, 678.0, 0.0, 0.0, 0.0, 0.0, 226.0]

    def test_prepaid_column_present_when_migration_applied(self):
        sql, params = app._build_invoice_insert(True, self.BASE)
        self.assertIn("PrepaidAmount", sql)
        self.assertIn("OUTPUT INSERTED.InvoiceID", sql)
        self.assertEqual(params[-1], 226.0)
        self.assertEqual(sql.count("?"), len(params), "placeholder count must match params")

    def test_prepaid_column_omitted_before_migration_018(self):
        # Check-out must not break on a database that has not applied 018 yet.
        sql, params = app._build_invoice_insert(False, self.BASE)
        self.assertNotIn("PrepaidAmount", sql)
        self.assertEqual(len(params), 16)
        self.assertEqual(sql.count("?"), len(params))

    def test_placeholder_count_matches_in_both_modes(self):
        for has_prepaid in (True, False):
            sql, params = app._build_invoice_insert(has_prepaid, self.BASE)
            self.assertEqual(sql.count("?"), len(params))

    def test_grand_total_columns_are_never_dropped(self):
        for has_prepaid in (True, False):
            sql, _ = app._build_invoice_insert(has_prepaid, self.BASE)
            for col in ("Subtotal", "TotalAmount", "AmountPaid",
                        "RoomSubtotal", "RoomTotal", "FnbSubtotal"):
                self.assertIn(col, sql)

    def test_values_are_never_interpolated_into_sql(self):
        sql, _ = app._build_invoice_insert(True, self.BASE)
        self.assertNotIn("9012", sql, "all values must be bound, not inlined")

    def test_zero_prepayment_still_writes_the_column(self):
        base = list(self.BASE)
        base[16] = 0.0
        _sql, params = app._build_invoice_insert(True, base)
        self.assertEqual(params[-1], 0.0)


class BookingQuoteIntegrationTests(unittest.TestCase):
    """A prepaid stay and a pay-at-check-out stay must end up billing identically."""

    def test_full_prepayment_means_no_card_at_checkout(self):
        quote = app.booking_quote(3, 200.0, 0.13)
        settled = app.settle_with_prepayment(quote["total"], quote["total"])
        self.assertEqual(settled["balance_due"], 0.0)

    def test_deposit_leaves_the_exact_remainder(self):
        quote = app.booking_quote(3, 200.0, 0.13)
        deposit = app.booking_payment_options(3, 200.0, 0.13)
        deposit_amount = next(o[3] for o in deposit if o[2] == app.PAYMENT_KIND_DEPOSIT)
        settled = app.settle_with_prepayment(quote["total"], deposit_amount)
        self.assertEqual(settled["balance_due"], 452.0)

    def test_a_free_stay_needs_no_payment(self):
        opts = app.booking_payment_options(0, 0.0, 0.13)
        self.assertEqual(sum(o[3] for o in opts), 0.0)


if __name__ == "__main__":
    unittest.main()
