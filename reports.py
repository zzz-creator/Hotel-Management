# type: ignore
import csv
import logging
import os
import configparser
from datetime import datetime

import db
from db import get_connection

config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
config.read(config_path)
server = config.get('database', 'server', fallback='')
database = config.get('database', 'database', fallback='')
username = config.get('database', 'username', fallback='')
password = config.get('database', 'password', fallback='')
CONNECTION_STRING = (
    'DRIVER={ODBC Driver 17 for SQL Server};'
    f'SERVER={server};'
    f'DATABASE={database};'
    f'UID={username};'
    f'PWD={password}'
)
db.init(CONNECTION_STRING)

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")

# Stay windows are HALF-OPEN, [check-in, check-out): a guest who leaves on the 4th does
# not occupy the night of the 4th. Every occupancy-shaped report in this file uses
# `CheckInDate <= Night AND CheckOutDate > Night`, and the housekeeping board used to
# disagree with both of them by writing `>=` -- so the same room was reported in-house on
# the night it was vacated. There is no helper to import from main (it opens its
# own connection and would be a circular import), so the rule is stated once here and the
# pure definition lives in main.stays_overlap().
def business_date():
    """The hotel's current business date -- as of 5 October 2026, just the wall clock.

    Kept as a function (rather than inlined) because every "today" default in the
    reports funnels through it. Reports that should speak about another day take an
    explicit date or window, so a closed day can be re-run by passing its date.
    """
    return datetime.now().date()


def _coerce_date(value, pattern="%Y-%m-%d"):
    """Parse a CLI date string into a date, or None when it is absent or unparseable."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return datetime.strptime(str(value).strip(), pattern).date()
    except ValueError:
        logging.error("'%s' is not a %s date; ignoring it.", value, pattern)
        return None


def ensure_export_dir():
    if not os.path.exists(EXPORT_DIR):
        os.makedirs(EXPORT_DIR, exist_ok=True)
    return EXPORT_DIR


def _timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _format_value(value):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _query_to_rows(cursor, query, params=()):
    cursor.execute(query, params)
    headers = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        rows.append({headers[idx]: _format_value(value) for idx, value in enumerate(row)})
    return headers, rows


def _write_csv(filename, headers, rows):
    path = os.path.join(ensure_export_dir(), filename)
    with open(path, mode="w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(header, "") for header in headers])
    logging.info("CSV report generated: %s", path)
    return path


def _export_query(report_name, query, title, params=()):
    with get_connection() as conn:
        if conn is None:
            raise RuntimeError("Database connection failed.")
        cursor = conn.cursor()
        headers, rows = _query_to_rows(cursor, query, params)

    timestamp = _timestamp()
    csv_path = _write_csv(f"{report_name}_{timestamp}.csv", headers, rows)
    return csv_path


def export_transactions(export_format="csv"):
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    csv_path = _export_query(
        "transactions",
        "SELECT * FROM Transactions",
        "Transactions Report"
    )
    return [csv_path]


def export_reservations(export_format="csv"):
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    csv_path = _export_query(
        "reservations",
        "SELECT * FROM Reservations",
        "Reservations Report"
    )
    return [csv_path]


def export_loyalty_statements(export_format="csv", room_number=None, customer=None):
    """Loyalty balances and ledger, keyed on the GUEST (migration 019).

    `customer` filters to one guest, by CustomerID, email, or full name. `room_number`
    narrows to a single stay context, which is a different thing: it now selects the
    customer who occupied that room, and then reports that person's WHOLE history --
    every room they have stayed in. That is the point of 019; a room-scoped report would
    only ever show one fragment of a balance.
    """
    if customer or room_number:
        customer_id = None
        if customer:
            customer_id = _customer_id_for_reference(customer)
            if customer_id is None:
                raise ValueError(
                    f"No customer profile matches '{customer}'. Use a CustomerID, an email "
                    "address, or 'LastName FirstName'."
                )
        if customer_id is None and room_number:
            # Room -> the guest who held that stay. Reservations.RoomNumber is the PK,
            # so at most one live stay matches.
            customer_id = _customer_id_for_room(room_number)
            if customer_id is None:
                raise ValueError(
                    f"No customer profile is linked to room {room_number}, so there is no "
                    "loyalty statement to export. The stay predates migration 019 or has "
                    "not been linked to a profile."
                )
        query = (
            "SELECT LA.CustomerID, cp.LastName, cp.FirstName, cp.Email, "
            "  LA.RoomNumber AS LastRoom, LA.Points, LA.Tier, LA.LastUpdated, "
            "  LT.ID AS TransactionID, LT.RoomNumber AS EarnedInRoom, "
            "  LT.Delta, LT.Reason, LT.CreatedAt, LT.SourceID "
            "FROM LoyaltyAccounts LA "
            "LEFT JOIN CustomerProfiles cp ON cp.CustomerID = LA.CustomerID "
            "LEFT JOIN LoyaltyTransactions LT ON LT.CustomerID = LA.CustomerID "
            "WHERE LA.CustomerID = ? "
            "ORDER BY LT.CreatedAt DESC"
        )
        params = (customer_id,)
        label = customer or (room_number if not customer_id else customer_id)
        report_name = f"loyalty_statement_{_slug(label)}"
        title = f"Loyalty Statement: {label}"
    else:
        query = (
            "SELECT LA.CustomerID, cp.LastName, cp.FirstName, cp.Email, "
            "  LA.RoomNumber AS LastRoom, LA.Points, LA.Tier, LA.LastUpdated, "
            "  LT.ID AS TransactionID, LT.RoomNumber AS EarnedInRoom, "
            "  LT.Delta, LT.Reason, LT.CreatedAt, LT.SourceID "
            "FROM LoyaltyAccounts LA "
            "LEFT JOIN CustomerProfiles cp ON cp.CustomerID = LA.CustomerID "
            "LEFT JOIN LoyaltyTransactions LT ON LT.CustomerID = LA.CustomerID "
            "ORDER BY LA.CustomerID, LT.CreatedAt DESC"
        )
        params = ()
        report_name = "loyalty_statements"
        title = "Loyalty Statements Report"

    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    csv_path = _export_query(report_name, query, title, params)
    return [csv_path]


def _slug(value):
    """Filesystem-safe fragment for a report filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


def _customer_id_for_reference(customer):
    """Resolve a CustomerID from a CustomerID, email, or 'LastName FirstName'.

    An exact CustomerID wins outright. Email and full name can each match more than one
    profile, so those fall back to a single unambiguous match and otherwise report
    ambiguity rather than silently exporting the wrong guest's history.
    """
    text = str(customer).strip()
    with get_connection() as conn:
        if conn is None:
            return None
        cursor = conn.cursor()
        if text.isdigit():
            cursor.execute(
                "SELECT CustomerID FROM CustomerProfiles WHERE CustomerID = ?", (int(text),))
            row = cursor.fetchone()
            if row:
                return int(row[0])
        cursor.execute(
            "SELECT CustomerID FROM CustomerProfiles "
            "WHERE Email = ? OR (LastName + ' ' + FirstName = ?) "
            "ORDER BY CustomerID",
            (text, text))
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(
                f"'{customer}' matches {len(rows)} customer profiles. Use the CustomerID "
                "or the email address to identify one guest."
            )
        return int(rows[0][0])


def _customer_id_for_room(room_number):
    """Resolve a room to the CustomerID of the guest on that stay (None if unlinked)."""
    with get_connection() as conn:
        if conn is None:
            return None
        cursor = conn.cursor()
        cursor.execute(
            "SELECT TOP 1 CustomerID FROM Reservations WHERE RoomNumber = ? "
            "ORDER BY CheckInDate DESC", (room_number,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None


def export_invoices(export_format="csv"):
    """Settled check-out bills with the Room / F&B split (migrations 011-013)."""
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    query = (
        "SELECT i.InvoiceID, i.RoomNumber, i.InvoiceDate, "
        "  i.RoomSubtotal, i.RoomTaxAmount, i.RoomTotal, "
        "  i.FnbSubtotal, i.FnbDiscountCodeAmount, i.FnbTierDiscountAmount, i.FnbTaxAmount, "
        "  i.Subtotal, i.DiscountCodeAmount, i.TierDiscountAmount, i.TaxAmount, "
        "  i.TotalAmount, i.PointsRedeemed, i.RedemptionValue, i.AmountPaid, "
        "  i.VoidedAt, i.VoidedBy, i.VoidReason, "
        "(SELECT COUNT(*) FROM Transactions t WHERE t.InvoiceID = i.InvoiceID) AS LineItems "
        "FROM Invoices i ORDER BY i.InvoiceID DESC"
    )
    return [_export_query("invoices", query, "Invoices Report")]


def export_revenue(export_format="csv", start_date=None, end_date=None):
    """Revenue by day, split into room and F&B, from the invoice snapshot."""
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    query = (
        "SELECT CAST(i.InvoiceDate AS date) AS InvoiceDate, COUNT(*) AS Invoices, "
        "  SUM(i.RoomSubtotal) AS RoomSubtotal, SUM(i.RoomTaxAmount) AS RoomTax, "
        "  SUM(i.RoomTotal) AS RoomTotal, "
        "  SUM(i.FnbSubtotal) AS FnbSubtotal, "
        "  SUM(i.FnbDiscountCodeAmount + i.FnbTierDiscountAmount) AS FnbDiscounts, "
        "  SUM(i.FnbTaxAmount) AS FnbTax, "
        "  SUM(i.TaxAmount) AS TotalTax, SUM(i.TotalAmount) AS GrossTotal, "
        "  SUM(i.RedemptionValue) AS PointsRedemptionValue, "
        "  SUM(i.AmountPaid) AS AmountPaid "
        "FROM Invoices i WHERE 1 = 1"
        " AND i.VoidedAt IS NULL"
    )
    params = []
    if start_date:
        query += " AND i.InvoiceDate >= ?"
        params.append(start_date)
    if end_date:
        query += " AND i.InvoiceDate < DATEADD(day, 1, ?)"
        params.append(end_date)
    query += " GROUP BY CAST(i.InvoiceDate AS date) ORDER BY InvoiceDate"
    return [_export_query("revenue", query, "Revenue Report", tuple(params))]


def export_occupancy(export_format="csv", start_date=None, end_date=None):
    """Nightly occupancy with ADR and RevPAR, over a re-runnable date window.

    ADR (Average Daily Rate) is room revenue divided by rooms sold that night; RevPAR is
    room revenue divided by the whole available inventory. Revenue is attributed to the
    night the invoice was issued, so these are settlement-day figures rather than a
    strict per-night accrual.

    The window defaults to the span of stays actually on record, which is what makes this
    a report over history. Pass `start_date`/`end_date` to re-run one window -- the same
    numbers come back, because nothing here reads the wall clock. `end_date` defaults to
    the business date, so "last Tuesday" is a date you can type rather than a day that
    has to still be today.

    Occupancy is half-open (`CheckInDate <= Night AND CheckOutDate > Night`): a guest
    departing on the 4th is not counted as occupying the night of the 4th. The
    housekeeping board uses the same rule, which is what stops the two reports from
    disagreeing about the same rows.
    """
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    first = _coerce_date(start_date)
    last = _coerce_date(end_date) or business_date()
    query = (
        "WITH Nums AS ("
        "  SELECT TOP 800 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n FROM sys.all_objects"
        "), Span AS ("
        "  SELECT MIN(CAST(CheckInDate AS date)) AS FirstNight, "
        "         MAX(CAST(CheckOutDate AS date)) AS LastNight FROM Reservations"
        "), Calendar AS ("
        "  SELECT DATEADD(day, n.n - 1, s.FirstNight) AS Night "
        "  FROM Nums n CROSS JOIN Span s "
        "  WHERE s.FirstNight IS NOT NULL "
        "    AND DATEADD(day, n.n - 1, s.FirstNight) <= s.LastNight"
        "), Capacity AS ("
        "  SELECT COUNT(*) AS TotalRooms FROM Rooms"
        "), Sold AS ("
        "  SELECT c.Night, COUNT(r.RoomNumber) AS RoomsSold "
        "  FROM Calendar c "
        "  LEFT JOIN Reservations r ON r.CheckInDate <= c.Night AND r.CheckOutDate > c.Night "
        "  GROUP BY c.Night"
        "), Revenue AS ("
        "  SELECT CAST(InvoiceDate AS date) AS Night, SUM(RoomSubtotal) AS RoomRevenue "
        "  FROM Invoices WHERE VoidedAt IS NULL GROUP BY CAST(InvoiceDate AS date)"
        ") "
        "SELECT s.Night, s.RoomsSold, cap.TotalRooms, "
        "  CASE WHEN cap.TotalRooms = 0 THEN 0 "
        "       ELSE ROUND(100.0 * s.RoomsSold / cap.TotalRooms, 2) END AS OccupancyPct, "
        "  COALESCE(rv.RoomRevenue, 0) AS RoomRevenue, "
        "  CASE WHEN s.RoomsSold = 0 THEN 0 "
        "       ELSE ROUND(COALESCE(rv.RoomRevenue, 0) / s.RoomsSold, 2) END AS ADR, "
        "  CASE WHEN cap.TotalRooms = 0 THEN 0 "
        "       ELSE ROUND(COALESCE(rv.RoomRevenue, 0) / cap.TotalRooms, 2) END AS RevPAR "
        "FROM Sold s "
        "CROSS JOIN Capacity cap "
        "LEFT JOIN Revenue rv ON rv.Night = s.Night "
        "WHERE (? IS NULL OR s.Night >= ?) AND s.Night <= ? "
        "ORDER BY s.Night"
    )
    params = (first, first, last)
    return [_export_query("occupancy", query, "Occupancy Report", params)]


def export_housekeeping(export_format="csv", floor=None, on_date=None):
    """Room status board for a chosen day, optionally limited to one floor.

    `on_date` defaults to the business date, and takes an explicit date so the board for
    a day that has already closed can be exported again. Nothing here reads the wall
    clock, so two runs of the same date give the same file.

    "Current guest" is the stay occupying the room that night, using the same half-open
    window as the occupancy report. This used to be `CheckOutDate >= CAST(GETDATE() AS
    date)`, which counted a guest as still in house on the night they left -- the direct
    cause of the housekeeping and occupancy reports disagreeing about the same table.
    Note the limit this carries: only the room's LIVE stay is shown, because
    `Reservations.RoomNumber` is the primary key, so a re-let room's previous occupant
    has been moved to `ReservationArchive` -- see docs/DEVIATIONS.md.
    """
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    for_date = _coerce_date(on_date) or business_date()
    query = (
        "SELECT r.RoomNumber, r.RoomType, COALESCE(r.Status, 'Available') AS Status, "
        "  cur.FirstName, cur.LastName, cur.CheckInDate, cur.CheckOutDate, "
        "  r.Description "
        "FROM Rooms r "
        "OUTER APPLY ("
        "  SELECT TOP 1 x.FirstName, x.LastName, x.CheckInDate, x.CheckOutDate "
        "  FROM Reservations x "
        "  WHERE x.RoomNumber = r.RoomNumber "
        "    AND x.CheckInDate <= ? AND x.CheckOutDate > ? "
        "  ORDER BY x.CheckInDate DESC"
        ") cur "
        "WHERE 1 = 1"
    )
    params = [for_date, for_date]
    if floor:
        query += " AND LEFT(r.RoomNumber, LEN(r.RoomNumber) - 3) = ?"
        params.append(str(floor))
    query += " ORDER BY r.RoomNumber"
    return [_export_query("housekeeping", query, "Housekeeping Report", tuple(params))]



def export_audit_log(export_format="csv", limit=5000):
    """Recent audit trail rows (migration 016)."""
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    query = (
        "SELECT TOP (?) AuditID, CreatedAt, Username, Action, EntityType, EntityID, Details "
        "FROM AuditLog ORDER BY AuditID DESC"
    )
    return [_export_query("audit_log", query, "Audit Log Report", (int(limit),))]


def export_guest_satisfaction(export_format="csv"):
    """Concierge request turnaround and stay feedback ratings (migration 015)."""
    if export_format != "csv":
        raise ValueError("Only csv export is supported.")
    query = (
        "SELECT 'Feedback' AS Source, f.FeedbackID AS ID, f.RoomNumber, "
        "  f.LastName + ' ' + f.FirstName AS Guest, "
        "  CAST(f.Rating AS varchar(10)) AS RatingOrStatus, "
        "  f.Comments AS Detail, f.CreatedAt "
        "FROM Feedback f "
        "UNION ALL "
        "SELECT 'Concierge', c.RequestID, c.RoomNumber, "
        "  c.LastName + ' ' + c.FirstName, c.Status, c.Message, c.CreatedAt "
        "FROM ConciergeRequests c "
        "ORDER BY CreatedAt DESC"
    )
    return [_export_query("guest_satisfaction", query, "Guest Satisfaction Report")]


REPORTS = {
    "transactions": export_transactions,
    "reservations": export_reservations,
    "loyalty": export_loyalty_statements,
    "invoices": export_invoices,
    "revenue": export_revenue,
    "occupancy": export_occupancy,
    "housekeeping": export_housekeeping,
    "audit": export_audit_log,
    "guest_satisfaction": export_guest_satisfaction,
}


def run_cli():
    import argparse

    parser = argparse.ArgumentParser(description="Hotel Management Reporting CLI")
    parser.add_argument("--report", choices=sorted(REPORTS), required=True,
                        help="Report type to export")
    parser.add_argument("--format", choices=["csv"], default="csv",
                        help="Export format (only csv is supported)")
    parser.add_argument("--room", help="Room number: reports the whole history of the guest who held that stay")
    parser.add_argument("--customer", help="Customer ID, email, or 'LastName FirstName' for loyalty statements")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD) for the revenue and occupancy reports")
    parser.add_argument("--end", help="End date (YYYY-MM-DD) for the revenue and occupancy reports")
    parser.add_argument("--floor", help="Floor number to limit the housekeeping report")
    parser.add_argument("--date", help="Board date (YYYY-MM-DD) for the housekeeping report; "
                                       "defaults to the business date")
    args = parser.parse_args()

    if args.report == "loyalty":
        paths = export_loyalty_statements(args.format, args.room, args.customer)
    elif args.report == "revenue":
        paths = export_revenue(args.format, args.start, args.end)
    elif args.report == "occupancy":
        paths = export_occupancy(args.format, args.start, args.end)
    elif args.report == "housekeeping":
        paths = export_housekeeping(args.format, args.floor, args.date)
    else:
        paths = REPORTS[args.report](args.format)

    for path in paths:
        logging.info("Export completed: %s", path)


if __name__ == "__main__":
    run_cli()
