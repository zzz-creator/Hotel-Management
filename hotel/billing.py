# type: ignore
"""billing: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
import os
from hotel import db
from hotel import ui
from hotel import session
from hotel import booking_ledger
from hotel import core
from hotel import keycards
from hotel import loyalty
from hotel import payments
from hotel import rooms

__all__ = [
    '_invoices_have_prepaid_column',
    '_build_invoice_insert',
    'record_transaction_for_room',
    'post_room_charge',
    'apply_discount',
    'compute_and_apply_discounts',
    'bill_room_transactions',
    'billing_creator',
    'list_invoices_for_room',
    'print_invoice',
    'invoices_menu',
    'void_invoice',
    'refund_invoice',
    'settlement_outstanding',
    'announce_settlement_outstanding',
    'check_out',
]




def _invoices_have_prepaid_column():
    """Whether Invoices.PrepaidAmount exists (migration 018). Cached after the first probe.

    Check-out must keep working before 018 is applied, so the invoice insert is built
    without the column when it is missing rather than failing with an invalid-column error.
    """
    if session._INVOICES_PREPAID_SUPPORT is not None:
        return session._INVOICES_PREPAID_SUPPORT
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT COL_LENGTH('dbo.Invoices', 'PrepaidAmount')")
            row = cursor.fetchone()
            session._INVOICES_PREPAID_SUPPORT = bool(row and row[0])
    except Exception as e:
        logging.debug(f"Invoices.PrepaidAmount probe failed ({type(e).__name__}: {e})")
        session._INVOICES_PREPAID_SUPPORT = False
    return session._INVOICES_PREPAID_SUPPORT


def _build_invoice_insert(has_prepaid, values):
    """Build the check-out Invoices INSERT, with or without PrepaidAmount.

    `values` is the ordered column list ending with the group breakdown. PrepaidAmount
    (migration 018) is appended only when the column exists, so check-out keeps working
    on a database that has not applied 018 yet. Split out from bill_room_transactions()
    so the exact SQL can be asserted in tests without a database.
    """
    cols = [
        "RoomNumber", "Subtotal", "DiscountCodeAmount", "TierDiscountAmount",
        "TaxAmount", "TotalAmount", "PointsRedeemed", "RedemptionValue", "AmountPaid",
        "RoomSubtotal", "RoomTaxAmount", "RoomTotal", "FnbSubtotal",
        "FnbDiscountCodeAmount", "FnbTierDiscountAmount", "FnbTaxAmount",
    ]
    params = list(values)[:len(cols)]
    if has_prepaid:
        cols.append("PrepaidAmount")
        params.append(round(float(values[16] if len(values) > 16 else 0.0), 2))
    placeholders = ",".join("?" for _ in params)
    return (f"INSERT INTO Invoices ({', '.join(cols)}) "
            f"OUTPUT INSERTED.InvoiceID VALUES ({placeholders})", tuple(params))
    # connection closed by context manager

def record_transaction_for_room(room_number: str, item_id: int, quantity: int, unit_price: float, paid: bool = False):
    """Record an ordered item for a room in the Transactions table.

    paid=True inserts the row pre-billed (IsBilled = 1). Returns the new transaction
    ID, or None on failure.
    """
    try:
        unit_price = float(unit_price) if unit_price is not None else 0.0
        amount = round(unit_price * int(quantity), 2)
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Transactions (RoomNumber, ItemID, Quantity, UnitPrice, Amount, IsBilled) "
                "OUTPUT INSERTED.ID VALUES (?, ?, ?, ?, ?, ?)",
                (room_number, item_id, quantity, unit_price, amount, 1 if paid else 0),
            )
            row = cursor.fetchone()
            tx_id = int(row[0]) if row and row[0] is not None else None
            conn.commit()
            logging.info("Recorded transaction for Item ID %s in room %s (%s).",
                         item_id, room_number, "paid now" if paid else "added to bill")
            return tx_id
    except Exception as e:
        logging.error(f"Error recording transaction: {e}")
        return None


def post_room_charge(room_number, check_in, check_out):
    """Post the nightly room charge to the room's folio as an unbilled 'Room' line.

    Runs at check-out (not check-in) so a declined card leaves the charge unbilled and
    the guest can retry, and so the charge reflects the nights actually reserved.

    The rate is the one CAPTURED ON THE RESERVATION when the stay was made
    (`Reservations.NightlyRate`, migration 022), not the current RoomTypes rate. Reading
    the live rate here let an admin rate edit re-price every confirmed booking in the
    house, which is the one thing a booked rate must never be. Only a stay with no
    captured rate -- written before 022, or for a category with no rate -- falls back to
    the current rate, and it says so. UnitPrice is the same number, so the invoice
    snapshot and the captured rate can never disagree either.

    Idempotent on Transactions.Description = 'Room charge for {check_in} - {room_type}',
    which is the exact string the INSERT writes, so a retried check-out cannot
    double-charge the room. The description is composed once and used by both the guard
    and the INSERT on purpose: an earlier version built it twice, the guard tested one
    form and the INSERT wrote the other, and the guard therefore never matched its own row
    -- so every retry billed the room again. `tests/verify_e2e.py` catches that regression.

    Returns the transaction ID, or None (including when the stay is already charged).
    """
    if not room_number:
        return None
    nights = core.stay_nights(check_in, check_out)
    if nights <= 0:
        logging.info("Stay has no nights to charge.")
        return None
    room_type = rooms.get_room_type(room_number)
    captured = rooms.get_captured_nightly_rate(room_number)
    if captured is None:
        logging.info(f"No rate was captured for {room_number}'s stay ({check_in}); "
                     "billing it at the current rate. Apply migration 022 to lock rates "
                     "at booking time.")
    nightly_rate = rooms.stay_nightly_rate(captured, rooms.get_nightly_rate(room_type))
    if nightly_rate <= 0:
        logging.info(f"No nightly rate configured for {room_type}; room charge skipped.")
        return None
    # ONE string, used by both the guard and the INSERT. Building it twice is exactly what
    # let them drift apart: the guard tested `marker` while the INSERT wrote
    # `f"{marker} - {room_type}"`, so the guard could never match its own row and every
    # retry posted the room charge a second time. Found by tests/verify_e2e.py against a
    # real server; no other check could see it, because both statements were well-formed.
    description = f"Room charge for {check_in} - {room_type}"
    amount = round(nightly_rate * nights, 2)
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            # Guard on the exact Description this function writes, so a retried check-out
            # cannot double-charge. Matching on RoomNumber + the full description also
            # scopes the guard to the stay: a room re-let later gets a different check-in
            # date, and therefore a different description.
            cursor.execute(
                "SELECT ID FROM Transactions WHERE RoomNumber = ? AND Description = ?",
                (room_number, description),
            )
            if cursor.fetchone():
                logging.info("Room charge already posted for this stay; not charging again.")
                return None
            cursor.execute(
                "INSERT INTO Transactions (RoomNumber, ItemID, Quantity, UnitPrice, Amount, "
                "IsBilled, ChargeGroup, Description) OUTPUT INSERTED.ID "
                "VALUES (?, NULL, ?, ?, ?, 0, ?, ?)",
                (room_number, nights, nightly_rate, amount, core.CHARGE_GROUP_ROOM,
                 description),
            )
            row = cursor.fetchone()
            tx_id = int(row[0]) if row and row[0] is not None else None
            conn.commit()
            logging.info(f"Posted room charge: {nights} night(s) x ${nightly_rate:.2f} "
                         f"({room_type}) = ${amount:.2f}.")
            return tx_id
    except Exception as e:
        logging.error(f"Error posting room charge: {e}")
        return None

def apply_discount(total_amount):
    try:
        discount_code = input("Enter discount code: ").strip()

        with db.get_connection() as conn:
            if conn is None:
                return total_amount
            cursor = conn.cursor()
            cursor.execute("SELECT DiscountPercentage FROM Discounts WHERE Code = ?", (discount_code,))
            result = cursor.fetchone()

        if result:
            discount_percentage = result[0]
            discount_amount = (total_amount * float(discount_percentage)) / 100
            total_with_discount = total_amount - discount_amount
            logging.info(f"Discount applied! New total amount: ${total_with_discount:.2f}")
            return total_with_discount
        else:
            logging.info("Invalid discount code.")
            return total_amount
    except Exception as e:
        logging.error(f"Error applying discount: {e}")
        return total_amount


def compute_and_apply_discounts(subtotal, room_number):
    """Apply an optional discount code then the loyalty tier discount to a subtotal.

    Shared by order pay-now and billing so both price room service identically.
    Returns (discounted_subtotal, discount_code_amount, tier_discount_amount, savings_lines).
    """
    discounted_subtotal = subtotal
    lines = []
    discount_code_amount = 0.0
    tier_discount_amount = 0.0
    if input("Do you have a discount code? (Y/N): ").strip().lower() == 'y':
        discounted_subtotal = apply_discount(subtotal)
        discount_code_amount = subtotal - discounted_subtotal
        if discount_code_amount > 0:
            lines.append(f"Discount: -${discount_code_amount:.2f}")
    tier_details = loyalty.get_tier_details_by_room(room_number) if core.get_loyalty_enabled() else None
    tier_discount_pct = tier_details["discount_percent"] if tier_details else 0.0
    if tier_discount_pct > 0:
        tier_discount_amount = discounted_subtotal * (tier_discount_pct / 100.0)
        discounted_subtotal -= tier_discount_amount
        lines.append(f"Loyalty Tier Discount ({tier_details['tier']}): -${tier_discount_amount:.2f}")
    return discounted_subtotal, discount_code_amount, tier_discount_amount, lines


def bill_room_transactions(room_number, require_payment=True):
    """Build a bill from a room's unbilled transactions, collect payment, and mark them billed.

    Applies discount code + loyalty tier discount + tax, offers point redemption, then
    processes the card. On success the transactions are marked IsBilled = 1 and any
    deferred order loyalty points are awarded. A $0 bill is confirmed without card entry.

    Stays whose items were all paid at order time (pay-now) still get a check-out
    invoice so the stay has a permanent, itemized record; nothing is owing then.

    Returns True if the bill was settled (or nothing was owing).
    """
    if not room_number:
        logging.info("Invalid room number.")
        return False
    try:
        with db.get_connection() as conn:
            if conn is None:
                logging.info("Database connection failed.")
                return False
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ID, ItemID, Quantity, UnitPrice, Amount, CreatedAt, IsBilled, ChargeGroup, Description "
                "FROM Transactions WHERE RoomNumber = ? AND IsBilled = 0",
                (room_number,),
            )
            rows = cursor.fetchall()
            # Pay-now items (billed at order time) that are not yet linked to an invoice.
            cursor.execute(
                "SELECT ID, ItemID, Quantity, UnitPrice, Amount, CreatedAt, ChargeGroup, Description "
                "FROM Transactions WHERE RoomNumber = ? AND IsBilled = 1 AND InvoiceID IS NULL",
                (room_number,),
            )
            pay_now_rows = cursor.fetchall()
        if not rows and not pay_now_rows:
            logging.info("No unbilled transactions found for that room.")
            return True

        def _group_of(row):
            # Pre-013 rows have no meaningful ChargeGroup; the DB default tags them 'F&B'.
            return (getattr(row, "ChargeGroup", None) or core.CHARGE_GROUP_FNB) == core.CHARGE_GROUP_ROOM

        # Split the folio. The room charge is posted by post_room_charge(); everything
        # else is F&B and is the only group discounts may touch.
        room_rows = [r for r in rows if _group_of(r)]
        fnb_rows = [r for r in rows if not _group_of(r)]
        pay_now_rows = [r for r in pay_now_rows if not _group_of(r)]

        room_subtotal = sum(float(r.Amount) for r in room_rows)
        fnb_subtotal = sum(float(r.Amount) for r in fnb_rows)
        pay_now_amount = sum(float(r.Amount) for r in pay_now_rows)
        subtotal = room_subtotal + fnb_subtotal
        tx_ids = [r.ID for r in rows]

        points_redeemed = 0
        redemption_value = 0.0
        redemption_customer_id = None
        amount_paid = 0.0
        total_before_redemption = 0.0
        discount_code_amount = 0.0
        tier_amount = 0.0
        room_tax = 0.0
        fnb_tax = 0.0
        discounted_fnb_subtotal = fnb_subtotal
        # Credit taken at the booking desk, resolved against this stay's check-in date.
        prepaid_applied = 0.0
        stay_check_in = core._reservation_check_in(room_number)

        if rows:
            receipt_lines = ["----- Receipt -----"]
            for r in room_rows:
                qty = r.Quantity or 1
                amt = float(r.Amount)
                label = getattr(r, "Description", None) or "Room charge"
                receipt_lines.append(f"{label} x{qty}: ${amt:.2f}")
            for r in fnb_rows:
                item_id = r.ItemID
                qty = r.Quantity or 1
                amt = float(r.Amount)
                item_name = getattr(r, "Description", None) or f"Item {item_id}"
                try:
                    with db.get_connection() as conn:
                        if conn is not None:
                            c = conn.cursor()
                            c.execute("SELECT Name FROM Items WHERE ItemID = ?", (item_id,))
                            ir = c.fetchone()
                            if ir:
                                item_name = ir[0]
                except Exception:
                    pass
                receipt_lines.append(f"{item_name} x{qty}: ${amt:.2f}")

            # Discounts are promotional, room rates are contractual, so the F&B group
            # alone is discounted. The room charge passes through at face value.
            if fnb_subtotal > 0:
                discounted_fnb_subtotal, discount_code_amount, tier_amount, savings_lines = compute_and_apply_discounts(fnb_subtotal, room_number)
                receipt_lines += savings_lines

            tax_rate = core.get_tax_rate()
            room_tax = room_subtotal * tax_rate
            fnb_tax = discounted_fnb_subtotal * tax_rate
            tax = room_tax + fnb_tax
            final_total = room_subtotal + discounted_fnb_subtotal + tax
            total_before_redemption = final_total
            if room_subtotal > 0:
                receipt_lines.append(f"Room Charges: ${room_subtotal:.2f}")
            if fnb_subtotal > 0:
                receipt_lines.append(f"Food & Beverage: ${discounted_fnb_subtotal:.2f}")
            receipt_lines.append(f"Subtotal: ${room_subtotal + discounted_fnb_subtotal:.2f}")
            receipt_lines.append(f"Tax (Tax Rate: {tax_rate*100:.0f}%): ${tax:.2f}")
            receipt_lines.append(f"Total Amount: ${final_total:.2f}")
            receipt_lines.append("-------------------")
            ui.box("Receipt", "\n".join(receipt_lines))

            # Loyalty redemption
            if core.get_loyalty_enabled():
                try:
                    points = loyalty.get_points_by_room(room_number)
                    if final_total > 0 and points > 0:
                        logging.info(f"You have {points} points available.")
                        use = input("Redeem points for this bill? (Y/N): ").strip().lower()
                        if use == 'y':
                            max_points_for_bill = int(min(points, int(final_total * core.get_loyalty_redemption_points_per_currency_unit())))
                            logging.info(f"Maximum points usable for this bill: {max_points_for_bill} points.")
                            while True:
                                try:
                                    pts = int(input(f"Enter number of points to redeem (0-{max_points_for_bill}): ").strip())
                                    if 0 <= pts <= max_points_for_bill:
                                        break
                                except ValueError:
                                    pass
                                logging.info("Invalid input. Please enter a valid integer.")
                            if pts > 0:
                                # Record the INTENT only. The deduction happens in the
                                # invoice transaction further down, beside the invoice insert.
                                # Committing it here meant a declined card cost the guest
                                # their points with nothing billed in exchange -- the defect
                                # in docs/DEVIATIONS.md 9.
                                redemption_customer_id = loyalty.customer_id_for_stay(room_number)
                                if redemption_customer_id is None:
                                    logging.info("No loyalty profile for this stay, so points cannot be redeemed.")
                                else:
                                    redemption_value = pts / core.get_loyalty_redemption_points_per_currency_unit()
                                    final_total -= redemption_value
                                    points_redeemed = pts
                                    logging.info(f"Redeeming {pts} points for ${redemption_value:.2f} off. New total: ${final_total:.2f}")
                except Exception as e:
                    logging.error(f"Error during loyalty redemption: {e}")

            # Credit anything paid at the booking desk. Applied after loyalty so it
            # reduces the card charge rather than the loyalty entitlement.
            if stay_check_in is not None:
                prepaid = booking_ledger.get_outstanding_booking_credit(room_number, stay_check_in)
                if prepaid > 0:
                    settled = booking_ledger.settle_with_prepayment(final_total, prepaid)
                    final_total = settled["balance_due"]
                    prepaid_applied = settled["credit_applied"]
                    receipt_lines.append(f"Prepaid at booking: -${prepaid_applied:.2f}")
                    receipt_lines.append(f"Balance due: ${final_total:.2f}")
                    ui.box("Receipt", "\n".join(receipt_lines))
                    logging.info(f"${prepaid_applied:,.2f} of your booking payment has been applied to this bill.")
                    if settled["credit_unused"] > 0:
                        logging.info(f"The final total came to less than you prepaid, so ${settled['credit_unused']:,.2f} "
                                     f"remains as a credit on your account and will be refunded.")

            # Collect payment, then mark transactions billed and record the check-out invoice.
            # The invoice snapshot keeps the pre-redemption total and the amount actually paid.
            amount_paid = max(0.0, final_total)
            if amount_paid <= 0:
                logging.info("Balance is $0.00 - confirming no payment required.")
            elif require_payment and not payments.process_credit_card(amount_paid):
                logging.info("Payment failed. Transactions remain unbilled.")
                return False
        else:
            # Nothing owing at check-out: every item on this stay was already paid when
            # ordered (pay-now). Create a consolidation invoice so the stay has a
            # permanent record linking those items (PaidEarlier = 1). Discount and tax
            # were applied and charged at order time, so this invoice carries the billed
            # line amounts as-is.
            subtotal = pay_now_amount
            discount_code_amount = 0.0
            tier_amount = 0.0
            fnb_subtotal = pay_now_amount
            discounted_fnb_subtotal = pay_now_amount
            room_subtotal = 0.0
            tax = 0.0
            final_total = 0.0
            total_before_redemption = pay_now_amount
            logging.info("Balance is $0.00 (items were paid at order time) - recording check-out invoice.")
        try:
            with db.get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return False
                cursor = conn.cursor()
                placeholders = ",".join("?" for _ in tx_ids)
                # Subtotal/TotalAmount/AmountPaid stay GRAND TOTALS; the Fnb*/Room*
                # columns carry the per-group breakdown for the itemized invoice.
                has_prepaid = _invoices_have_prepaid_column()
                invoice_sql, invoice_params = _build_invoice_insert(has_prepaid, [
                    room_number,
                    round(subtotal, 2),
                    round(discount_code_amount, 2),
                    round(tier_amount, 2),
                    round(tax, 2),
                    round(total_before_redemption, 2),
                    points_redeemed,
                    round(redemption_value, 2),
                    round(amount_paid, 2),
                    round(room_subtotal, 2),
                    round(room_tax, 2),
                    round(room_subtotal + room_tax, 2),
                    round(fnb_subtotal, 2),
                    round(discount_code_amount, 2),
                    round(tier_amount, 2),
                    round(fnb_tax, 2),
                    round(prepaid_applied, 2),
                ])
                cursor.execute(invoice_sql, invoice_params)
                _row = cursor.fetchone()
                invoice_id = int(_row[0]) if _row and _row[0] is not None else None
                # Consume the booking credit in this same transaction, so a rolled-back
                # invoice leaves the credit available for the retry.
                if has_prepaid and stay_check_in is not None and prepaid_applied > 0:
                    booking_ledger.apply_booking_credit(room_number, stay_check_in, prepaid_applied, invoice_id, conn)
                # Deduct the redeemed points in THIS transaction too, for the same reason the
                # booking credit is applied here. The invoice above already claims this
                # discount in PointsRedeemed/RedemptionValue, so the deduction has to land with
                # it. Keyed on the invoice so a repeated redemption is detectable and a retry
                # cannot take the points twice.
                if points_redeemed > 0 and redemption_customer_id is not None:
                    loyalty.redeem_points_for_invoice(
                        redemption_customer_id, points_redeemed, room_number, 'checkout', conn,
                        source_id=f'redeem:{invoice_id}')
                # Mark this bill's items billed and link them to the invoice (settled at check-out).
                if tx_ids:
                    cursor.execute(
                        f"UPDATE Transactions SET IsBilled = 1, InvoiceID = ?, PaidEarlier = 0 WHERE ID IN ({placeholders})",
                        tuple([invoice_id] + tx_ids),
                    )
                # Link earlier pay-now items to the same invoice so it is fully itemized.
                if invoice_id is not None:
                    cursor.execute(
                        "UPDATE Transactions SET InvoiceID = ?, PaidEarlier = 1 "
                        "WHERE RoomNumber = ? AND IsBilled = 1 AND InvoiceID IS NULL",
                        (invoice_id, room_number),
                    )
                conn.commit()
                logging.info("Transactions marked as billed and check-out invoice recorded.")
        except Exception as e:
            logging.error(f"Failed to finalize bill / invoice: {e}")
            return False

        if core.get_loyalty_enabled() and tx_ids:
            loyalty.award_billed_order_points(room_number, tx_ids)
        return True
    except Exception as e:
        logging.error(f"Error generating bill from transactions: {e}")
        return False


def billing_creator():
    logging.info("\n--- Billing Creator ---")
    logging.info("Generate bill from (1) Transactions table or (2) Manual entry")
    choice = input("Enter 1 or 2: ").strip()
    if choice == '1':
        room_number = input("Enter room number to generate bill for: ").strip()
        if bill_room_transactions(room_number):
            logging.info("Bill settled.")
        else:
            logging.info("Bill could not be settled.")
    else:
        # Manual entry fallback (preserve previous behavior)
        logging.info("Enter your charges. Type 'done' for description to finish.")
        items = []
        total = 0.0
        while True:
            description = input("Enter charge description (or 'done' to finish): ").strip()
            if description.lower() == 'done' or description == '':
                break
            try:
                amount = float(input("Enter amount for this charge: ").strip())
            except ValueError:
                logging.info("Invalid amount. Please try again.")
                continue
            items.append((description, amount))
            total += amount
        receipt = "\n----- Receipt -----\n"
        for desc, amt in items:
            receipt += f"{desc}: ${amt:.2f}\n"
        receipt += f"Subtotal: ${total:.2f}\n"
        tax = total * core.get_tax_rate()
        final_total = total + tax
        receipt += f"Tax (Tax Rate: {core.get_tax_rate()*100:.0f}%): ${tax:.2f}\n"
        receipt += f"Total Amount: ${final_total:.2f}\n"
        receipt += "-------------------\n"
        ui.box("Receipt", receipt)
## =========================
# Invoice Printing
## =========================
def list_invoices_for_room(room_number):
    """All stored invoices for a room, newest first."""
    if not room_number:
        return []
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT InvoiceID, RoomNumber, InvoiceDate, TotalAmount, AmountPaid "
                "FROM Invoices WHERE RoomNumber = ? ORDER BY InvoiceID DESC",
                (room_number,),
            )
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"Error listing invoices: {e}")
        return []


def print_invoice(invoice_id):
    """Print a stored invoice with a full itemized breakdown, including items that were
    paid before check-out. Also saves a plain-text copy to exports/ for real printing."""
    try:
        invoice_id = int(invoice_id)
    except (TypeError, ValueError):
        logging.info("Invalid invoice number.")
        return False
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "SELECT i.InvoiceID, i.RoomNumber, i.InvoiceDate, i.Subtotal, i.DiscountCodeAmount, "
                "i.TierDiscountAmount, i.TaxAmount, i.TotalAmount, i.PointsRedeemed, i.RedemptionValue, i.AmountPaid, "
                "i.RoomSubtotal, i.RoomTaxAmount, i.RoomTotal, i.FnbSubtotal, "
                "i.FnbDiscountCodeAmount, i.FnbTierDiscountAmount, i.FnbTaxAmount, "
                "(SELECT TOP 1 r.FirstName + ' ' + r.LastName FROM Reservations r "
                "  WHERE r.RoomNumber = i.RoomNumber ORDER BY r.CheckInDate DESC) AS GuestName "
                + (", i.PrepaidAmount" if _invoices_have_prepaid_column() else "")
                + " FROM Invoices i WHERE i.InvoiceID = ?",
                (invoice_id,),
            )
            inv = cursor.fetchone()
            if not inv:
                logging.info("Invoice not found.")
                return False
            cursor.execute(
                "SELECT t.ID, t.Quantity, t.UnitPrice, t.Amount, t.PaidEarlier, t.CreatedAt, "
                "  COALESCE(t.ChargeGroup, 'F&B') AS ChargeGroup, "
                "  COALESCE(it.Name, t.Description, "
                "    CASE WHEN t.ItemID IS NULL THEN 'Charge' "
                "         ELSE 'Item ' + CAST(t.ItemID AS varchar(20)) END) AS ItemName "
                "FROM Transactions t LEFT JOIN Items it ON it.ItemID = t.ItemID "
                "WHERE t.InvoiceID = ? ORDER BY t.CreatedAt, t.ID",
                (invoice_id,),
            )
            items = cursor.fetchall()
            # Payment-method breakdown: the booking desk's money, joined back on the
            # stay. Applied rows name this exact invoice; unapplied rows that share
            # the most recent check-in for the room are the fallback so a front-desk
            # deposit still shows up on the folio even when it was never marked applied.
            cursor.execute(
                "SELECT Kind, Amount, CardLast4, PaidAt, Notes, AppliedToInvoiceID "
                "FROM ReservationPayments WHERE RoomNumber = ? AND AppliedToInvoiceID = ? "
                "ORDER BY PaidAt",
                (inv.RoomNumber, invoice_id),
            )
            payment_rows = cursor.fetchall()
            if not payment_rows:
                cursor.execute(
                    "SELECT TOP 1 StayCheckIn FROM ReservationPayments "
                    "WHERE RoomNumber = ? ORDER BY StayCheckIn DESC",
                    (inv.RoomNumber,),
                )
                stay = cursor.fetchone()
                if stay:
                    cursor.execute(
                        "SELECT Kind, Amount, CardLast4, PaidAt, Notes, AppliedToInvoiceID "
                        "FROM ReservationPayments WHERE RoomNumber = ? AND StayCheckIn = ? "
                        "ORDER BY PaidAt",
                        (inv.RoomNumber, stay[0]),
                    )
                    payment_rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading invoice {invoice_id}: {e}")
        return False

    guest = (inv.GuestName or "").strip()
    header = (
        f"INVOICE #{inv.InvoiceID}\n"
        f"Room: {inv.RoomNumber}     Date: {inv.InvoiceDate}\n"
        + (f"Guest: {guest}\n" if guest else "")
    )
    ui.box("Invoice", header.strip())

    if payment_rows:
        ui.show_table(
            f"Invoice #{inv.InvoiceID} - Payments",
            ["Kind", "Amount", "Method", "Paid At", "Notes"],
            [
                (
                    p.Kind,
                    f"${float(p.Amount):,.2f}",
                    f"Card ending {p.CardLast4}" if p.CardLast4 else "Recorded payment",
                    p.PaidAt,
                    p.Notes or "-",
                )
                for p in payment_rows
            ],
        )

    def _line_rows(rows):
        out = []
        for t in rows:
            tag = "Card (pay now)" if t.PaidEarlier else "Card (check-out)"
            out.append((
                t.ID,
                t.ItemName,
                t.Quantity or 1,
                f"${float(t.UnitPrice or 0):.2f}",
                f"${float(t.Amount):.2f}",
                tag,
            ))
        return out

    # Room charges and food & beverage are shown as separate groups: the room charge
    # is never discounted, so a single combined table would imply a discount applied to it.
    room_items = [t for t in items if (t.ChargeGroup or core.CHARGE_GROUP_FNB) == core.CHARGE_GROUP_ROOM]
    fnb_items = [t for t in items if (t.ChargeGroup or core.CHARGE_GROUP_FNB) != core.CHARGE_GROUP_ROOM]
    if room_items:
        ui.show_table(
            f"Invoice #{inv.InvoiceID} - Room Charges",
            ["Tx #", "Item", "Nights", "Nightly Rate", "Amount", "Payment"],
            _line_rows(room_items),
        )
    if fnb_items:
        ui.show_table(
            f"Invoice #{inv.InvoiceID} - Food & Beverage",
            ["Tx #", "Item", "Qty", "Unit Price", "Amount", "Payment"],
            _line_rows(fnb_items),
        )
    if not items:
        ui.info("This invoice has no itemized line items.")
    totals = [
        ("Room Charges (subtotal)", f"${float(getattr(inv, 'RoomSubtotal', 0) or 0):.2f}"),
        ("Room Charges (tax)", f"${float(getattr(inv, 'RoomTaxAmount', 0) or 0):.2f}"),
        ("Room Charges (total)", f"${float(getattr(inv, 'RoomTotal', 0) or 0):.2f}"),
        ("F&B (subtotal)", f"${float(getattr(inv, 'FnbSubtotal', 0) or 0):.2f}"),
        ("F&B discount code", f"-${float(getattr(inv, 'FnbDiscountCodeAmount', 0) or 0):.2f}"),
        ("F&B loyalty tier discount", f"-${float(getattr(inv, 'FnbTierDiscountAmount', 0) or 0):.2f}"),
        ("F&B (tax)", f"${float(getattr(inv, 'FnbTaxAmount', 0) or 0):.2f}"),
        ("Subtotal (all)", f"${float(inv.Subtotal):.2f}"),
        ("Discount Code", f"-${float(inv.DiscountCodeAmount):.2f}"),
        ("Loyalty Tier Discount", f"-${float(inv.TierDiscountAmount):.2f}"),
        ("Tax", f"${float(inv.TaxAmount):.2f}"),
        ("Total", f"${float(inv.TotalAmount):.2f}"),
        ("Points Redeemed", str(inv.PointsRedeemed)),
        ("Redemption Value", f"-${float(inv.RedemptionValue):.2f}"),
        ("Prepaid at Booking", f"-${float(getattr(inv, 'PrepaidAmount', 0) or 0):.2f}"),
        ("Amount Paid", f"${float(inv.AmountPaid):.2f}"),
    ]
    ui.show_table(f"Invoice #{inv.InvoiceID} - Totals", ["Item", "Value"], totals)
    logging.info("Room charges are charged at face value: discount codes and loyalty tier "
                 "discounts apply to the Food & Beverage group only.")
    if any(getattr(t, "PaidEarlier", 0) for t in items):
        logging.info("Note: 'Card (pay now)' items were charged when the order was placed and are not part of the check-out Subtotal/Tax above.")

    try:
        save_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"invoice_{invoice_id}.txt")
        lines = ["=" * 64, f"INVOICE #{inv.InvoiceID}", f"Room: {inv.RoomNumber}", f"Date: {inv.InvoiceDate}"]
        if guest:
            lines.append(f"Guest: {guest}")
        lines.append("=" * 64)
        header_line = f"{'Tx #':<6}{'Item':<30}{'Qty':>4}{'Unit':>10}{'Amount':>10}  {'Payment':<18}"
        for group_title, group_rows in (("ROOM CHARGES", room_items), ("FOOD & BEVERAGE", fnb_items)):
            if not group_rows:
                continue
            lines.append("")
            lines.append(group_title)
            lines.append(header_line)
            lines.append("-" * 64)
            for t in group_rows:
                tag = "CARD (PAY NOW)" if t.PaidEarlier else "CARD (CHECK-OUT)"
                lines.append(
                    f"{t.ID:<6}{t.ItemName:<30}{t.Quantity or 1:>4}"
                    f"{float(t.UnitPrice or 0):>10.2f}{float(t.Amount):>10.2f}  {tag:<18}"
                )
        lines.append("-" * 64)
        for label, value in totals:
            lines.append(f"{label:<30}{value:>20}")
        lines.append("")
        lines.append("Note: Room charges are charged at face value. Discount codes and")
        lines.append("loyalty tier discounts apply to the Food & Beverage group only.")
        if any(getattr(t, "PaidEarlier", 0) for t in items):
            lines.append("")
            lines.append("Note: CARD (PAY NOW) lines were charged when the order was placed")
            lines.append("and are not part of the check-out Subtotal/Tax shown above.")
        lines.append("=" * 64)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logging.info(f"Invoice saved to: {path}")
    except Exception as e:
        logging.error(f"Error saving invoice text file: {e}")
    return True


def invoices_menu():
    """Admin/manager: browse and print stored invoices."""
    while True:
        ui.pause()
        ui.show_menu("Invoices & Printing", [
            "1. Print Invoice by Number",
            "2. Find Invoices by Room",
            "3. Void an Invoice",
            "4. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            raw = input("Enter invoice number: ").strip()
            if raw.isdigit():
                print_invoice(int(raw))
            else:
                logging.info("Invalid invoice number.")
        elif choice == '2':
            room_number = input("Enter room number (floor + 3-digit code): ").strip()
            invoices = list_invoices_for_room(room_number)
            if not invoices:
                logging.info("No invoices found for that room.")
                continue
            ui.show_table(
                f"Invoices for Room {room_number}",
                ["Invoice #", "Room", "Date", "Total", "Paid"],
                [(inv.InvoiceID, inv.RoomNumber, inv.InvoiceDate,
                  f"${float(inv.TotalAmount):.2f}", f"${float(inv.AmountPaid):.2f}") for inv in invoices],
            )
            raw = input("Enter invoice number to print (blank to cancel): ").strip()
            if raw.isdigit():
                print_invoice(int(raw))
        elif choice == '3':
            raw = input("Enter invoice number to void: ").strip()
            if raw.isdigit():
                void_invoice(int(raw))
            else:
                logging.info("Invalid invoice number.")
        elif choice == '4':
            break
        else:
            logging.info("Invalid choice. Please try again.")


def void_invoice(invoice_id):
    """Void a stored invoice: it stays for the audit trail, but stops counting.

    Sets Invoices.VoidedAt/VoidedBy/VoidReason, and every revenue reader filters on
    VoidedAt IS NULL, so a voided invoice is excluded from the revenue report and ADR
    without rewriting history. Rooms and guests keep their rows; only the reportable
    totals change. Gated like any destructive admin action: master override plus an
    exact typed confirmation. Requires a reason, because a void with no reason is an
    audit gap, not a fix.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT InvoiceID, RoomNumber, InvoiceDate, TotalAmount, AmountPaid, VoidedAt "
                "FROM Invoices WHERE InvoiceID = ?",
                (invoice_id,),
            )
            inv = cursor.fetchone()
            if not inv:
                logging.info("Invoice not found.")
                return
            if inv.VoidedAt is not None:
                logging.info(f"Invoice {invoice_id} was already voided at {inv.VoidedAt}.")
                return
            logging.info(f"Invoice {inv.InvoiceID} -- room {inv.RoomNumber}, {inv.InvoiceDate}, "
                         f"total ${float(inv.TotalAmount):,.2f}, paid ${float(inv.AmountPaid):,.2f}.")
            reason = input("Reason for voiding (required): ").strip()
            if not reason:
                logging.info("A reason is required; nothing was voided.")
                return
            if not ui.ask_confirmation("Void this invoice?", default="n"):
                logging.info("Cancelled.")
                return
            if not core.require_master_override():
                return
            typed = input("Type VOID INVOICE to confirm: ").strip()
            if typed != "VOID INVOICE":
                logging.info("Confirmation text did not match. Nothing was voided.")
                return
            cursor.execute(
                "UPDATE Invoices SET VoidedAt = GETDATE(), VoidedBy = ?, VoidReason = ? "
                "WHERE InvoiceID = ? AND VoidedAt IS NULL",
                (session.CURRENT_USER, reason, invoice_id),
            )
            if cursor.rowcount == 0:
                logging.info(f"Invoice {invoice_id} was voided by someone else just now.")
                return
            conn.commit()
            core.log_audit("UPDATE", "Invoice", invoice_id, f"Voided: {reason}",
                      old_value="active", new_value="voided")
            logging.info(f"Invoice {invoice_id} voided. It is excluded from revenue reports.")
            # The void stops the invoice counting; the refund returns the money. The two
            # are independent choices a clerk can get wrong if offered as one flow, so
            # they stay separate prompts -- a void with no refund when the guest was
            # charged is a chargeback-shaped hole, and a refund with no void double-pays.
            try:
                paid = float(inv.AmountPaid)
            except (TypeError, ValueError):
                paid = 0.0
            if paid > 0 and ui.ask_confirmation(
                    f"Also post a refund row for the ${paid:,.2f} paid on this invoice?",
                    default="n"):
                refund_invoice(invoice_id, reason=reason)
    except Exception as e:
        logging.error(f"Error voiding invoice {invoice_id}: {e}")


def refund_invoice(invoice_id, reason=None):
    """Post a signed 'Refund' row on the stay the voided invoice was issued for.

    Caps the refund at what was actually paid (AmountPaid) minus any refunds already
    posted for the same invoice, so a voided-then-refunded-then-refunded-again loop
    cannot pay out more than the guest gave. The row is signed negative under the
    original booking reference, and stamped with AppliedToInvoiceID so it nets against
    the voided invoice in the stay's payment history.

    Requires the invoice to be voided first AND a reason: an unexplained refund is
    the same audit gap as an unexplained void. ReservationPayments' Kind CHECK only
    allows the documented values, and 'Refund' is one of them.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RoomNumber, InvoiceDate, TotalAmount, AmountPaid, VoidedAt "
                "FROM Invoices WHERE InvoiceID = ?",
                (invoice_id,),
            )
            inv = cursor.fetchone()
            if not inv:
                logging.info("Invoice not found.")
                return False
            if inv.VoidedAt is None:
                logging.info("Void the invoice before posting a refund against it.")
                return False
            try:
                paid = float(inv.AmountPaid)
            except (TypeError, ValueError):
                logging.info("Invoice has no usable paid total; nothing to refund.")
                return False
            cursor.execute(
                "SELECT COALESCE(SUM(Amount), 0) FROM ReservationPayments "
                "WHERE AppliedToInvoiceID = ? AND Kind = ?",
                (invoice_id, booking_ledger.PAYMENT_KIND_REFUND),
            )
            prior = float(cursor.fetchone()[0] or 0.0)
            refundable = round(paid + prior, 2)  # prior is signed negative
            if refundable <= 0:
                logging.info(f"Invoice {invoice_id} has already been fully refunded.")
                return False
            if reason is None:
                reason = input("Refund reason (required): ").strip()
            if not reason:
                logging.info("A reason is required; no refund posted.")
                return False
            cursor.execute(
                "SELECT TOP 1 RoomNumber, BookingRef, StayCheckIn FROM ReservationPayments "
                "WHERE AppliedToInvoiceID = ?",
                (invoice_id,),
            )
            link = cursor.fetchone()
            if not link:
                cursor.execute(
                    "SELECT TOP 1 p.RoomNumber, p.BookingRef, p.StayCheckIn "
                    "FROM ReservationPayments p "
                    "JOIN Reservations r ON r.RoomNumber = p.RoomNumber "
                    "WHERE p.RoomNumber = ? AND p.StayCheckIn <= ? AND r.CheckOutDate >= ? "
                    "ORDER BY p.StayCheckIn DESC",
                    (inv.RoomNumber, inv.InvoiceDate, inv.InvoiceDate),
                )
                link = cursor.fetchone()
            if not link:
                logging.info("Cannot identify the original booking for this invoice, "
                             "so no refund row can be attributed. Void only, for now.")
                return False
            payment_id = booking_ledger.record_booking_payment(
                link.RoomNumber, link.BookingRef, link.StayCheckIn,
                booking_ledger.PAYMENT_KIND_REFUND, -round(refundable, 2),
                notes=f"Refund for voided invoice {invoice_id}: {reason}",
                conn=conn,
            )
            if payment_id is None:
                return False
            cursor.execute(
                "UPDATE ReservationPayments SET AppliedToInvoiceID = ? WHERE PaymentID = ?",
                (invoice_id, payment_id),
            )
            conn.commit()
            core.log_audit("CREATE", "ReservationPayment", str(payment_id),
                      f"Refund of ${refundable:.2f} for voided invoice {invoice_id}: {reason}")
            logging.info(f"Refund of ${refundable:,.2f} posted against invoice {invoice_id} "
                         f"(payment {payment_id}).")
            return True
    except Exception as e:
        logging.error(f"Error refunding invoice {invoice_id}: {e}")
        return False


def settlement_outstanding(room_number, check_in=None, check_out=None, customer_id=None):
    """What still has to happen before this stay's settlement counts as finished.

    Derived from the database rather than remembered, because the process that failed is
    gone by the time anyone asks: a guest who has already walked out leaves nothing in
    memory to interrogate, and that is exactly the case where a live key card matters.
    Ordered the way `check_out()` runs its steps, so the result reads as remaining work
    rather than as a list of symptoms.

    Conservative on purpose. Every entry must be a fact that is unambiguously wrong for a
    finished settlement -- a predicate that cries wolf on healthy rooms is one nobody
    reads. Each check is therefore guarded by the thing that makes it meaningful: the
    stay-points check needs loyalty enabled and a real customer, and the invoice check only
    fires when there is nothing at all to have billed against.

    Returns a list of short human-readable strings; empty means the settlement looks done.
    """
    outstanding = []
    if not room_number:
        return outstanding
    try:
        with db.get_connection() as conn:
            if conn is None:
                return outstanding
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM Transactions WHERE RoomNumber = ? AND IsBilled = 0",
                (room_number,))
            unbilled = int(cursor.fetchone()[0] or 0)
            cursor.execute("SELECT COUNT(*) FROM Invoices WHERE RoomNumber = ?", (room_number,))
            invoices = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                "SELECT COUNT(*) FROM KeyCards WHERE RoomNumber = ? AND Status = 'Active'",
                (room_number,))
            live_cards = int(cursor.fetchone()[0] or 0)
            cursor.execute("SELECT Status FROM Rooms WHERE RoomNumber = ?", (room_number,))
            row = cursor.fetchone()
            room_status = row[0] if row else None

            stay_awarded = None
            if core.get_loyalty_enabled() and customer_id and check_in and check_out:
                cursor.execute(
                    "SELECT COUNT(*) FROM LoyaltyTransactions WHERE CustomerID = ? "
                    "AND SourceID = ?",
                    (customer_id, f"stay:{customer_id}:{room_number}:{check_in}"))
                stay_awarded = int(cursor.fetchone()[0] or 0)
    except Exception as e:
        logging.error(f"Error reading settlement state for room {room_number}: {e}")
        return outstanding

    if invoices == 0:
        outstanding.append("no invoice was ever issued, so the bill was never settled")
    if unbilled:
        outstanding.append(f"{unbilled} charge(s) are still unbilled")
    if live_cards:
        outstanding.append(f"{live_cards} key card(s) are still active -- the guest can "
                           f"still open the door")
    if room_status == "Occupied":
        outstanding.append("the room is still flagged Occupied, so housekeeping has not "
                           "been told")
    if stay_awarded == 0:
        outstanding.append("stay points have not been awarded for this stay")
    return outstanding


def announce_settlement_outstanding(room_number, reservation=None):
    """Tell the clerk exactly what a re-run of check-out still has to do.

    `check_out()` used to report a failed settlement only as the exception that caused it,
    which named the error and nothing about the state it left behind. The clerk was left to
    infer whether the guest was still in the room, still holding a working key card, and
    still owed money -- and `check_out()` returns None on success and on failure alike, so
    nothing downstream could tell them apart either.
    """
    try:
        items = settlement_outstanding(
            room_number,
            getattr(reservation, "CheckInDate", None),
            getattr(reservation, "CheckOutDate", None),
            getattr(reservation, "CustomerID", None))
    except Exception as e:
        logging.debug(f"Could not report outstanding settlement work for {room_number}: {e}")
        return
    if not items:
        logging.info("Nothing is outstanding for this room -- the settlement looks complete.")
        return
    logging.info("This check-out did NOT finish. Still to do:")
    for n, item in enumerate(items, 1):
        logging.info(f"  {n}. {item}")
    logging.info("Run check-out again for this room to finish it; the steps already done are "
                 "not repeated.")


def check_out():
    """Handle customer check-out without deleting the reservation.

    Identity comes from validate_room() -- last name, FIRST name and room number in one
    query -- exactly like every other guest-facing feature. The old version asked for a
    last name and a room number only, so anyone who knew a guest's surname and guessed or
    was told a room number could settle that guest's folio, read it, take their card
    details, and walk out. validate_room() is also bounded to three attempts and never
    lists other stays for a surname; see docs/BOOKING.md §1.
    """
    room_number, first_name = core.validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number, so "
                     "check-out cannot continue. Please ask the front desk for assistance.")
        return
    matched_reservation = None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CheckInDate, CheckOutDate, CustomerID, LastName FROM Reservations "
                "WHERE RoomNumber = ?",
                (room_number,),
            )
            matched_reservation = cursor.fetchone()
        if matched_reservation is None:
            logging.info("We could not find a reservation for that room. Please ask the "
                         "front desk for assistance.")
            return
        # Rooms status is real state: flag the room for housekeeping on check-out,
        # but only AFTER the bill is settled (a declined card leaves the guest in
        # the room and must not flip it to 'Dirty'). Bill is mandatory and payment
        # is required before any loyalty points are awarded. Deferred order points
        # are awarded inside bill_room_transactions() on successful payment; stay
        # points are awarded only after the bill is settled.
        #
        # The nightly room charge is posted first so it lands on the same folio and
        # invoice as any room service. It stays unbilled until payment succeeds, so
        # a declined card can be retried without re-posting the charge. It is billed at
        # the rate captured on the reservation, not the current one.
        post_room_charge(room_number, matched_reservation.CheckInDate, matched_reservation.CheckOutDate)
        if bill_room_transactions(room_number):
            rooms.set_room_status(room_number, "Dirty")
            keycards.revoke_active_key_cards(room_number, "stayed")
            # Pass the CustomerID we already matched rather than letting the
            # award re-resolve it from the room. The resolver works today because
            # check-out leaves the reservation in place, but tying the award to
            # that would make the loyalty of a stay quietly depend on a row
            # check-out happens not to touch.
            loyalty.award_stay_points(room_number, matched_reservation.CheckInDate,
                              matched_reservation.CheckOutDate,
                              customer_id=matched_reservation.CustomerID)
            core.log_audit("UPDATE", "Reservation", room_number,
                      f"Check-out completed for {first_name} {matched_reservation.LastName}")
            logging.info(f"Check-out complete for room {room_number}. Thanks for visiting {core.get_hotel_name()}, {first_name.capitalize()}! We hope to see you again soon!")
        else:
            logging.info("Payment declined. Please settle the bill before completing check-out.")
            announce_settlement_outstanding(room_number, matched_reservation)
    except Exception as e:
        logging.error(f"Error during check-out: {e}")
        # Name the state left behind, not just the error that caused it. A guest who has
        # already walked out leaves no in-memory trace to ask, so this is the only place
        # the residue is reported. See settlement_outstanding() above.
        announce_settlement_outstanding(room_number, matched_reservation)
