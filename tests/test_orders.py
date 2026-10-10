# type: ignore
"""place_order() / advance_order() / get_order(): the non-interactive order services
(PLAN-web-api.md phase 5).

These pin what the console's order_item()/queue and the web endpoints both delegate to:
the bill and pay-now recording, a declined card recording nothing, and the audit actor
each move is attributed to. The console's interactive prompts are the adapter layer and
are not exercised here (no automated test drives them); the recording they delegate to is
the thing that must never drift between the two front-ends.
"""
import unittest
from unittest import mock

import db
import core
import billing
import loyalty
import orders


class _FakeCursor:
    def __init__(self, row=None):
        self.row = row or ()
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row


class _FakeConn:
    """A one-row in-memory connection: the row fetchone() answers, commit() records."""

    def __init__(self, row=None):
        self._cursor = _FakeCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self._cursor.executed.append(("COMMIT", ()))


class PlaceOrderTests(unittest.TestCase):
    """The recording tail both front-ends share, line by line."""

    def _record(self, room="9012", items=((1, 12.5, 2),), pay_mode="bill",
                card_processor=None, discount_code=None, quote=None, price_quote=None):
        recorded = []
        subtotal = round(sum(p * q for (_i, p, q) in items), 2)
        if price_quote is None:
            price_quote = (subtotal, subtotal, 0.0, [], 1.0)
        record = lambda room, item, qty, price, paid=False: recorded.append(
            (room, item, qty, price, paid)) or (item + 100)
        with mock.patch.object(billing, "record_transaction_for_room",
                               side_effect=record), \
                mock.patch.object(billing, "price_pay_now",
                                  return_value=price_quote) as price, \
                mock.patch.object(loyalty, "award_billed_order_points") as award, \
                mock.patch.object(orders, "open_order", return_value=7) as opened, \
                mock.patch.object(orders, "add_order_items", return_value=2) as added, \
                mock.patch.object(core, "log_audit") as audit:
            result = orders.place_order(
                room, list(items), pay_mode=pay_mode, card_processor=card_processor,
                discount_code=discount_code, quote=quote, actor="ada")
        return result, recorded, price, award, opened, added, audit

    def test_bill_records_lines_at_list_price_unpaid(self):
        result, recorded, _price, award, opened, added, audit = self._record(
            items=((1, 12.5, 2), (2, 5.0, 1)))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["order_id"], 7)
        self.assertEqual(result["total"], 30.0)
        self.assertEqual(result["paid"], 0.0)
        self.assertEqual(recorded, [("9012", 1, 2, 12.5, False), ("9012", 2, 1, 5.0, False)])
        opened.assert_called_once_with("9012")
        added.assert_called_once_with(7, [(1, 12.5, 2), (2, 5.0, 1)])
        award.assert_not_called()
        self.assertEqual(audit.call_args.kwargs["user"], "ada")

    def test_pay_now_quotes_prices_and_records_discounted_paid_lines(self):
        quote = (23.5, 21.5, 2.0, ["Discount: -$3.50"], 0.86)
        result, recorded, price, award, _opened, added, audit = self._record(
            items=((1, 12.5, 2),), pay_mode="pay_now",
            card_processor=lambda amount: (True, "1111"), discount_code="SAVE10",
            price_quote=quote)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["paid"], 23.5)
        self.assertEqual(result["savings_lines"], ["Discount: -$3.50"])
        price.assert_called_once()
        self.assertEqual(price.call_args.args, (25.0, "9012", "SAVE10"))
        # Each line is charged at its discounted, pre-tax price, paid.
        self.assertEqual(recorded, [("9012", 1, 2, 10.75, True)])
        award.assert_called_once()
        added.assert_called_once_with(7, [(1, 10.75, 2)])
        self.assertEqual(audit.call_args.kwargs["user"], "ada")

    def test_pay_now_uses_the_console_quote_when_given(self):
        quote = (23.5, 21.5, 2.0, [], 0.86)
        result, _recorded, price, _award, _opened, _added, _audit = self._record(
            pay_mode="pay_now", card_processor=lambda amount: True, quote=quote)
        self.assertEqual(result["status"], "ok")
        price.assert_not_called()

    def test_declined_card_records_nothing(self):
        result, recorded, _price, award, opened, added, audit = self._record(
            pay_mode="pay_now", card_processor=lambda amount: (False, "declined"))
        self.assertEqual(result["status"], "declined")
        self.assertIsNone(result["order_id"])
        self.assertEqual(recorded, [])
        opened.assert_not_called()
        added.assert_not_called()
        award.assert_not_called()
        audit.assert_not_called()

    def test_card_processor_exception_is_a_decline(self):
        def explode(amount):
            raise RuntimeError("gateway down")
        result, recorded, _price, _award, opened, _added, _audit = self._record(
            pay_mode="pay_now", card_processor=explode)
        self.assertEqual(result["status"], "declined")
        self.assertEqual(recorded, [])
        opened.assert_not_called()


class AdvanceOrderTests(unittest.TestCase):
    """The lifecycle move, driven by an explicit action and named actor."""

    def _move(self, action, row=("Placed", "9012")):
        conns = [_FakeConn(row), _FakeConn()]
        with mock.patch.object(db, "get_connection", side_effect=conns) as connect, \
                mock.patch.object(core, "log_audit") as audit:
            status = orders.advance_order(7, action=action, actor="mia")
        return status, connect, audit, conns

    def test_advance_moves_to_the_next_state(self):
        status, _connect, audit, conns = self._move("advance")
        self.assertEqual(status, "Preparing")
        first = conns[0].cursor().executed
        self.assertIn("UPDATE Orders SET Status", first[1][0])
        self.assertEqual(first[1][1][0], "Preparing")
        self.assertEqual(audit.call_args.kwargs["user"], "mia")
        # The guest is told from the same actor, not whichever console signed in last.
        notif = conns[1].cursor().executed[0]
        self.assertIn("INSERT INTO Notifications", notif[0])
        self.assertEqual(notif[1][2], "mia")

    def test_cancel_closes_the_order(self):
        status, _connect, _audit, conns = self._move("cancel")
        self.assertEqual(status, "Cancelled")
        self.assertEqual(conns[0].cursor().executed[1][1][0], "Cancelled")

    def test_closed_orders_are_left_alone(self):
        for closed in ("Completed", "Cancelled"):
            status, _connect, audit, _conns = self._move("advance", row=(closed, "9012"))
            self.assertEqual(status, closed)
            audit.assert_not_called()

    def test_unknown_action_is_refused(self):
        status, _connect, audit, _conns = self._move("sideways")
        self.assertEqual(status, "Placed")
        audit.assert_not_called()

    def test_missing_order_is_none(self):
        status, _connect, audit, _conns = self._move("advance", row=())
        self.assertIsNone(status)
        audit.assert_not_called()


class GetOrderTests(unittest.TestCase):
    """The detail read: one order's record, distinguishing missing from unavailable."""

    def test_returns_the_record_tuple(self):
        row = (7, "9012", "Preparing", None, None, None)
        with mock.patch.object(db, "get_connection",
                               return_value=_FakeConn(row)) as connect:
            record = orders.get_order("7")
        self.assertEqual(record, (7, "9012", "Preparing", None, None, None))
        self.assertEqual(connect.call_count, 1)

    def test_garbage_id_is_none_without_touching_the_database(self):
        with mock.patch.object(db, "get_connection") as connect:
            self.assertIsNone(orders.get_order("abc"))
        connect.assert_not_called()

    def test_missing_order_is_none(self):
        with mock.patch.object(db, "get_connection", return_value=_FakeConn()):
            self.assertIsNone(orders.get_order(7))

    def test_connection_failure_is_a_runtime_error(self):
        class _NoneConn:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        with mock.patch.object(db, "get_connection", return_value=_NoneConn()):
            with self.assertRaises(RuntimeError):
                orders.get_order(7)


if __name__ == "__main__":
    unittest.main()