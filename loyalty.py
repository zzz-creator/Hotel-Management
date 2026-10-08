# type: ignore
"""loyalty: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from datetime import datetime
import db
import ui
import core
import rooms

__all__ = [
    'points_per_dollar_order_vs_room',
    'ensure_loyalty_tables',
    'customer_id_for_stay',
    'ensure_customer_loyalty_account',
    'get_points_by_customer',
    'add_points_to_customer',
    'redeem_points_by_customer',
    'LoyaltyRedemptionError',
    'redeem_points_for_invoice',
    'get_lifetime_points_by_customer',
    'create_loyalty_account_if_missing',
    'get_points_by_room',
    'add_points_by_room',
    'redeem_points_by_room',
    'get_lifetime_points_by_room',
    '_tiers_from_db',
    'get_tier_for_points',
    'get_tier_details_by_customer',
    'get_tier_details_by_room',
    'recompute_tier_by_customer',
    'recompute_tier',
    'recompute_all_tiers',
    'award_stay_points',
    'award_billed_order_points',
    'loyalty_admin_menu',
    '_pick_loyalty_customer',
    'admin_view_loyalty_accounts',
    'admin_view_loyalty_transactions',
    'admin_adjust_loyalty_points',
    'admin_manage_loyalty_tiers',
    'admin_recompute_all_tiers',
]




def points_per_dollar_order_vs_room(order_rate=None, per_night=None, nightly_rate=None):
    """Return (order_pts_per_dollar, room_pts_per_dollar) for the entry calibration.

    Pure, so the ordering can be asserted without a database. The room figure is the
    points a Standard-category night actually buys, at Bronze tier (multiplier 1.0):
    `points_per_night / the Standard rate`. A Deluxe or a higher tier buys more per
    dollar, so the Standard/Bronze figure is the floor the F&B rate has to stay under --
    which is the whole point. `nightly_rate` defaults to the seeded Standard rate, so a
    database that has re-priced Standard does not silently move the calibration.
    """
    order = float(order_rate if order_rate is not None else 0.5)
    per_night_value = int(per_night if per_night is not None else 100)
    rate = float(nightly_rate if nightly_rate is not None else core.DEFAULT_ROOM_TYPE_RATES["Standard"])
    room = (per_night_value / rate) if rate > 0 else 0.0
    return order, room


## =========================
# Loyalty Program Helpers
## =========================
def ensure_loyalty_tables():
    """Create loyalty tables if they don't exist."""
    if not core.get_loyalty_enabled():
        return
    with db.get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("IF OBJECT_ID('dbo.LoyaltyAccounts','U') IS NULL BEGIN CREATE TABLE LoyaltyAccounts (CustomerID INT PRIMARY KEY, RoomNumber NVARCHAR(50) NULL, Points INT NOT NULL DEFAULT 0, Tier NVARCHAR(50) NULL, LastUpdated DATETIME NULL) END")
            cursor.execute("IF OBJECT_ID('dbo.LoyaltyTransactions','U') IS NULL BEGIN CREATE TABLE LoyaltyTransactions (ID INT IDENTITY(1,1) PRIMARY KEY, CustomerID INT NULL, RoomNumber NVARCHAR(50), Delta INT, Reason NVARCHAR(255), CreatedAt DATETIME DEFAULT GETDATE(), SourceID NVARCHAR(100) NULL) END")
            cursor.execute("IF OBJECT_ID('dbo.LoyaltyTiers','U') IS NULL BEGIN CREATE TABLE LoyaltyTiers (TierName NVARCHAR(50) PRIMARY KEY, MinLifetimePoints INT NOT NULL DEFAULT 0, PointsMultiplier DECIMAL(5,2) NOT NULL DEFAULT 1.00, DiscountPercent DECIMAL(5,2) NOT NULL DEFAULT 0, Perks NVARCHAR(500) NOT NULL DEFAULT '') END")
            cursor.execute("SELECT COUNT(*) FROM LoyaltyTiers")
            if cursor.fetchone()[0] == 0:
                for tier in core.DEFAULT_TIERS:
                    cursor.execute("INSERT INTO LoyaltyTiers (TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks) VALUES (?, ?, ?, ?, ?)", tier)
            conn.commit()
        except Exception as e:
            logging.error(f"Error ensuring loyalty tables: {e}")


def customer_id_for_stay(room_number, check_in=None):
    """Resolve the CustomerID of the guest occupying (or who occupied) a stay.

    This is the bridge that lets the rest of the app keep asking "what does this room
    have?" while the loyalty balance itself belongs to a person. Returns None when the
    stay has no linked profile, which is the normal state for a pre-019 reservation.
    """
    if not room_number:
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            if check_in:
                cursor.execute(
                    "SELECT TOP 1 CustomerID FROM Reservations "
                    "WHERE RoomNumber = ? AND CheckInDate = ?",
                    (room_number, check_in))
            else:
                cursor.execute(
                    "SELECT TOP 1 CustomerID FROM Reservations WHERE RoomNumber = ? "
                    "ORDER BY CheckInDate DESC",
                    (room_number,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logging.debug(f"No customer profile linked to room {room_number}: {e}")
        return None


def ensure_customer_loyalty_account(customer_id) -> bool:
    """Ensure a loyalty account exists for this customer. No-op without migration 019."""
    if not core.get_loyalty_enabled() or not customer_id:
        return False
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT Points FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO LoyaltyAccounts (CustomerID, Points, LastUpdated) VALUES (?, ?, ?)",
                    (customer_id, 0, datetime.now()))
                conn.commit()
            return True
        except Exception as e:
            logging.debug(f"Loyalty account not available (needs migration 019?): {e}")
            return False


def get_points_by_customer(customer_id) -> int:
    """Spendable loyalty balance for a CUSTOMER, across every room they have stayed in."""
    if not core.get_loyalty_enabled() or not customer_id:
        return 0
    with db.get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT Points FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            logging.debug(f"Error getting loyalty points: {e}")
            return 0


def add_points_to_customer(customer_id, delta_points, reason='adjust', source_id=None,
                           room_number=None) -> bool:
    """Move a signed delta on a customer's balance and write one ledger row."""
    if not core.get_loyalty_enabled() or not customer_id or not delta_points:
        return False
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            ensure_customer_loyalty_account(customer_id)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE LoyaltyAccounts SET Points = Points + ?, RoomNumber = ?, LastUpdated = ? "
                "WHERE CustomerID = ?",
                (delta_points, room_number, datetime.now(), customer_id))
            cursor.execute(
                "INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason, SourceID) "
                "VALUES (?, ?, ?, ?, ?)",
                (customer_id, room_number, delta_points, reason, source_id))
            conn.commit()
            recompute_tier_by_customer(customer_id)
            logging.info(f"Added {delta_points} points to customer {customer_id} ({reason}).")
            return True
        except Exception as e:
            logging.debug(f"Error adding loyalty points: {e}")
            return False


def redeem_points_by_customer(customer_id, points, reason='redeem', room_number=None) -> bool:
    if not core.get_loyalty_enabled() or not customer_id or points <= 0:
        return False
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            if get_points_by_customer(customer_id) < points:
                logging.info("Insufficient loyalty points.")
                return False
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE LoyaltyAccounts SET Points = Points - ?, LastUpdated = ? WHERE CustomerID = ?",
                (points, datetime.now(), customer_id))
            cursor.execute(
                "INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason) VALUES (?, ?, ?, ?)",
                (customer_id, room_number, -points, reason))
            conn.commit()
            logging.info(f"Redeemed {points} points from customer {customer_id} ({reason}).")
            return True
        except Exception as e:
            logging.debug(f"Error redeeming loyalty points: {e}")
            return False


class LoyaltyRedemptionError(Exception):
    """A point redemption cannot be honoured, so the bill that promised it cannot stand.

    Raised rather than returned as False because the invoice records the discount it
    applied. A False here would let that invoice commit claiming a discount nobody took,
    which is a silent revenue leak; raising rolls the invoice back, leaving the guest
    unbilled so the attempt can simply be repeated. See docs/DEVIATIONS.md 9.
    """


def redeem_points_for_invoice(customer_id, points, room_number, reason, conn,
                             source_id=None):
    """Deduct redeemed points inside the caller's transaction, beside its invoice insert.

    The invoice snapshots the discount it granted (`PointsRedeemed`/`RedemptionValue`), so
    that claim and the guest's balance have to land together or not at all. This is the
    same reason `apply_booking_credit()` runs where it does. Redemption used to be committed
    up front instead, which meant a declined card cost the guest their points with nothing
    billed in exchange and no way to detect the loss afterwards -- `SourceID` was NULL, so
    nothing in the ledger identified the transaction.

    `source_id` makes the deduction self-checking, following the convention
    `award_stay_points()` uses for its own idempotency guard.

    Does not commit: the caller owns the transaction and must not commit a partial bill.
    """
    if points <= 0 or not customer_id:
        return False
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ISNULL(Points, 0) FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
    row = cursor.fetchone()
    balance = int(row[0]) if row and row[0] is not None else 0
    if balance < points:
        raise LoyaltyRedemptionError(
            f"cannot redeem {points} points from customer {customer_id}: balance is {balance}")
    if source_id:
        cursor.execute(
            "SELECT 1 FROM LoyaltyTransactions WHERE CustomerID = ? AND SourceID = ?",
            (customer_id, source_id))
        if cursor.fetchone():
            logging.info(f"Points already redeemed under {source_id}; not deducting twice.")
            return False
    cursor.execute(
        "UPDATE LoyaltyAccounts SET Points = Points - ?, LastUpdated = ? WHERE CustomerID = ?",
        (points, datetime.now(), customer_id))
    cursor.execute(
        "INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason, SourceID) "
        "VALUES (?, ?, ?, ?, ?)",
        (customer_id, room_number, -points, reason, source_id))
    logging.info(f"Redeemed {points} points from customer {customer_id} ({reason}).")
    return True


def get_lifetime_points_by_customer(customer_id) -> int:
    """Lifetime points ever earned by a customer (sum of positive ledger deltas)."""
    if not core.get_loyalty_enabled() or not customer_id:
        return 0
    with db.get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(Delta), 0) FROM LoyaltyTransactions "
                "WHERE CustomerID = ? AND Delta > 0", (customer_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            logging.debug(f"Error getting lifetime loyalty points: {e}")
            return 0


# ---------------------------------------------------------------------------
# Room-shaped delegates.
#
# Loyalty is stored per CUSTOMER, but much of the app naturally talks about the stay
# that is being checked in, billed or checked out. These keep those call sites reading
# naturally while the balance follows the person, which is the whole point of 019.
# ---------------------------------------------------------------------------

def create_loyalty_account_if_missing(room_number: str, check_in=None) -> bool:
    """Ensure the guest in this room has a loyalty account."""
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return False
    return ensure_customer_loyalty_account(customer_id)


def get_points_by_room(room_number: str, check_in=None) -> int:
    return get_points_by_customer(customer_id_for_stay(room_number, check_in))


def add_points_by_room(room_number: str, delta_points: int, reason: str = 'adjust',
                       source_id: str = None, check_in=None) -> bool:
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return False
    return add_points_to_customer(customer_id, delta_points, reason, source_id, room_number)


def redeem_points_by_room(room_number: str, points: int, reason: str = 'redeem',
                          check_in=None) -> bool:
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return False
    return redeem_points_by_customer(customer_id, points, reason, room_number)


def get_lifetime_points_by_room(room_number: str, check_in=None) -> int:
    return get_lifetime_points_by_customer(customer_id_for_stay(room_number, check_in))


def _tiers_from_db():
    """All tiers sorted by MinLifetimePoints ascending. Empty list if the table is missing."""
    with db.get_connection() as conn:
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks FROM LoyaltyTiers ORDER BY MinLifetimePoints")
            rows = cursor.fetchall()
            return [(r.TierName, int(r.MinLifetimePoints), float(r.PointsMultiplier), float(r.DiscountPercent), r.Perks or "") for r in rows]
        except Exception as e:
            logging.error(f"Error reading loyalty tiers: {e}")
            return []


def get_tier_for_points(lifetime_points: int) -> str:
    """Return the tier name for a given lifetime points total."""
    tiers = _tiers_from_db() or core.DEFAULT_TIERS
    current = tiers[0][0]
    for name, min_pts, _mult, _disc, _perks in tiers:
        if lifetime_points >= int(min_pts):
            current = name
        else:
            break
    return current


def get_tier_details_by_customer(customer_id):
    """Return tier details (name, multiplier, discount %, perks, lifetime points) for a customer."""
    if not core.get_loyalty_enabled() or not customer_id:
        return None
    lifetime = get_lifetime_points_by_customer(customer_id)
    tiers = _tiers_from_db() or core.DEFAULT_TIERS
    tier_name = get_tier_for_points(lifetime)
    for name, min_pts, mult, disc, perks in tiers:
        if name == tier_name:
            return {
                "customer_id": customer_id,
                "tier": tier_name,
                "min_lifetime_points": int(min_pts),
                "lifetime_points": lifetime,
                "points_multiplier": float(mult),
                "discount_percent": float(disc),
                "perks": perks,
            }
    return {
        "customer_id": customer_id,
        "tier": tier_name,
        "min_lifetime_points": 0,
        "lifetime_points": lifetime,
        "points_multiplier": 1.0,
        "discount_percent": 0.0,
        "perks": "",
    }


def get_tier_details_by_room(room_number: str, check_in=None):
    """Tier of the guest in this room. Delegates, so the balance follows the person."""
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return None
    details = get_tier_details_by_customer(customer_id)
    if details is not None:
        # `room` is kept in the dict because callers label their output with it.
        details["room"] = room_number
    return details


def recompute_tier_by_customer(customer_id) -> str:
    """Recalculate and persist a customer's tier from lifetime points."""
    if not core.get_loyalty_enabled() or not customer_id:
        return ""
    tier = get_tier_for_points(get_lifetime_points_by_customer(customer_id))
    with db.get_connection() as conn:
        if conn is None:
            return tier
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE LoyaltyAccounts SET Tier = ?, LastUpdated = ? WHERE CustomerID = ?",
                (tier, datetime.now(), customer_id))
            conn.commit()
            return tier
        except Exception as e:
            logging.debug(f"Error recomputing loyalty tier: {e}")
            return tier


def recompute_tier(room_number: str, check_in=None) -> str:
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return ""
    return recompute_tier_by_customer(customer_id)


def recompute_all_tiers() -> int:
    """Recompute tiers for all loyalty accounts. Returns the number of accounts processed."""
    if not core.get_loyalty_enabled():
        return 0
    with db.get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT CustomerID FROM LoyaltyAccounts")
            customers = [r[0] for r in cursor.fetchall()]
            for customer_id in customers:
                recompute_tier_by_customer(customer_id)
            return len(customers)
        except Exception as e:
            logging.debug(f"Error recomputing loyalty tiers: {e}")
            return 0


def award_stay_points(room_number, check_in, check_out, customer_id=None):
    """Award loyalty points for a completed stay, to the GUEST (not the room).

    points = nights x loyalty_points_per_night x room-category multiplier x tier multiplier.
    Idempotent per stay via SourceID, so running check-out twice cannot double-award.
    Returns the number of points awarded (0 if none).
    """
    if not core.get_loyalty_enabled() or not room_number or not check_in or not check_out:
        return 0
    if customer_id is None:
        customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        # A stay with no linked profile cannot hold a balance, and crediting "the room"
        # would hand the points to the next guest. Skipped deliberately, not silently.
        logging.debug(f"Stay {room_number}/{check_in} has no customer profile; no points awarded.")
        return 0
    nights = core.stay_nights(check_in, check_out)
    if nights <= 0:
        return 0

    # CustomerID is in the SourceID on purpose: two different guests can occupy the same
    # room on the same dates across a re-let, and a room-only key would let the second
    # guest's award be rejected as a duplicate of the first.
    source_id = f"stay:{customer_id}:{room_number}:{check_in}"
    with db.get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM LoyaltyTransactions WHERE CustomerID = ? AND SourceID = ?",
                (customer_id, source_id))
            if cursor.fetchone():
                logging.info(f"Stay points already awarded for this stay ({source_id}).")
                return 0
        except Exception as e:
            logging.error(f"Error checking existing stay award: {e}")
            return 0

    room_type = rooms.get_room_type(room_number)
    category_mult = rooms.get_room_type_multiplier(room_type)
    tier = get_tier_details_by_customer(customer_id)
    tier_mult = tier["points_multiplier"] if tier else 1.0
    points = int(round(nights * core.get_loyalty_points_per_night() * category_mult * tier_mult))
    if points <= 0:
        return 0
    if add_points_to_customer(customer_id, points, reason='stay', source_id=source_id,
                              room_number=room_number):
        logging.info(
            f"Awarded {points} stay points to customer {customer_id} for room {room_number}: "
            f"{nights} night(s) x {core.get_loyalty_points_per_night()}/night x {room_type} x{category_mult:.2f} x tier x{tier_mult:.2f}."
        )
        return points
    return 0


def award_billed_order_points(room_number, tx_ids, customer_id=None):
    """Award order-accrual loyalty points once a set of transactions has been paid.

    points = SUM(Amount of the paid transactions) x accrual rate x tier multiplier
    (pre-discount basis). Idempotent per transaction set via
    SourceID 'order_pay:{customer}:{room}:{sorted tx ids}'. Returns points awarded (0 if none).

    Only F&B lines accrue. The nightly room charge is excluded (it is in the same
    folio) because the stay is already rewarded by award_stay_points() for its nights
    -- counting both would pay the guest twice for a single stay.

    The accrual rate is a FLOAT and is deliberately lower than the room's own earn rate.
    At 3 pts/$ it was three and a half times what a Standard night's 100 points on a
    $120 room bought, so the most profitable way for a guest to earn was to order room
    service and the room was worth less than a coffee. `points_per_dollar_order_vs_room()`
    is the calibration and tests/test_billing_math.py fails if the two are reordered.
    """
    if not core.get_loyalty_enabled() or not room_number or not tx_ids:
        return 0
    tx_list = [int(t) for t in tx_ids if t is not None]
    if not tx_list:
        return 0
    if customer_id is None:
        customer_id = customer_id_for_stay(room_number)
    if not customer_id:
        return 0
    source_id = "order_pay:{0}:{1}:{2}".format(customer_id, room_number,
                                               "|".join(str(t) for t in sorted(tx_list)))
    try:
        with db.get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM LoyaltyTransactions WHERE CustomerID = ? AND SourceID = ?", (customer_id, source_id))
            if cursor.fetchone():
                return 0
            placeholders = ",".join("?" for _ in tx_list)
            cursor.execute(
                f"SELECT COALESCE(SUM(Amount), 0) FROM Transactions "
                f"WHERE ID IN ({placeholders}) AND ChargeGroup = ?",
                tuple(tx_list) + (core.CHARGE_GROUP_FNB,),
            )
            total = float(cursor.fetchone()[0] or 0.0)
    except Exception as e:
        logging.error(f"Error checking/awarding order points: {e}")
        return 0
    tier = get_tier_details_by_customer(customer_id)
    multiplier = tier["points_multiplier"] if tier else 1.0
    accrual_rate = core.get_loyalty_accrual_points_per_unit()
    points = int(total * accrual_rate * multiplier)
    if points <= 0:
        return 0
    if add_points_to_customer(customer_id, points, reason='order', source_id=source_id,
                              room_number=room_number):
        logging.info(f"Awarded {points} order points to customer {customer_id} "
                     f"({total:.2f} x {accrual_rate:g} pts/$ x tier x{multiplier:.2f}).")
        return points
    return 0


def loyalty_admin_menu():
    """Admin: loyalty management submenu."""
    while True:
        ui.pause()
        ui.show_menu("Loyalty Management", [
            "1. View Loyalty Accounts",
            "2. Adjust Points for a Room",
            "3. View Loyalty Transactions",
            "4. Manage Loyalty Tiers",
            "5. Recalculate All Tiers",
            "6. Back to Admin Panel",
        ])
        sub = input("Enter your choice: ").strip()
        if sub == '1':
            admin_view_loyalty_accounts()
        elif sub == '2':
            admin_adjust_loyalty_points()
        elif sub == '3':
            admin_view_loyalty_transactions()
        elif sub == '4':
            admin_manage_loyalty_tiers()
        elif sub == '5':
            admin_recompute_all_tiers()
        elif sub == '6':
            break
        else:
            logging.info("Invalid choice.")


def _pick_loyalty_customer(prompt="Enter the guest's name or email: "):
    """Admin helper: resolve a guest to a CustomerID for a loyalty action.

    Loyalty belongs to a person, so every admin loyalty action identifies the guest.
    Accepts a full name or an email so staff do not have to remember which spelling
    the guest registered under. Returns the CustomerID, or None if not found.
    """
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled.")
        return None
    query = input(prompt).strip()
    if not query:
        logging.info("No guest specified.")
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT TOP 10 CustomerID, FirstName, LastName, Email FROM CustomerProfiles "
                "WHERE Email = ? OR LastName + ' ' + FirstName = ? "
                "OR (LastName = ? AND FirstName = ?) ORDER BY CustomerID DESC",
                (query, query, query, query),
            )
            matches = cursor.fetchall()
            if not matches:
                # A surname alone must not be enough to pick a person: report a miss
                # rather than guessing, or a clerk could adjust the wrong guest.
                logging.info(f"No customer profile found for '{query}'.")
                return None
            if len(matches) > 1:
                ui.info("More than one guest matches. Pick the right one:")
                rows = [(r.CustomerID, f"{r.LastName}, {r.FirstName}", r.Email or "") for r in matches]
                ui.show_table("Matches", ["ID", "Guest", "Email"], rows)
                try:
                    chosen = int(input("Customer ID: ").strip())
                except ValueError:
                    logging.info("Invalid selection.")
                    return None
                match = next((r for r in matches if r.CustomerID == chosen), None)
                if match is None:
                    logging.info("That customer ID is not one of the matches.")
                    return None
                return int(match.CustomerID)
            return int(matches[0].CustomerID)
    except Exception as e:
        logging.error(f"Error finding customer for loyalty action: {e}")
        return None


def admin_view_loyalty_accounts():
    """Admin: list loyalty accounts and point balances, by GUEST."""
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled.")
        return
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT la.CustomerID, cp.LastName, cp.FirstName, cp.Email, la.RoomNumber, "
                "la.Points, la.Tier, la.LastUpdated "
                "FROM LoyaltyAccounts la "
                "LEFT JOIN CustomerProfiles cp ON cp.CustomerID = la.CustomerID "
                "ORDER BY la.Points DESC"
            )
            rows = cursor.fetchall()
            if rows:
                table_rows = [
                    (r.CustomerID, f"{r.LastName or '?'}, {r.FirstName or '?'}",
                     r.Email or "-", r.RoomNumber or "-", r.Points, r.Tier, r.LastUpdated)
                    for r in rows
                ]
                ui.show_table(
                    "Loyalty Accounts",
                    ["Cust ID", "Guest", "Email", "Last Room", "Points", "Tier", "Last Updated"],
                    table_rows,
                )
            else:
                ui.info("No loyalty accounts found.")
    except Exception as e:
        logging.error(f"Error viewing loyalty accounts: {e}")


def admin_view_loyalty_transactions():
    """Admin: view loyalty transactions."""
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled.")
        return
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT lt.ID, lt.CustomerID, cp.LastName, cp.FirstName, lt.RoomNumber, "
                "lt.Delta, lt.Reason, lt.CreatedAt, lt.SourceID "
                "FROM LoyaltyTransactions lt "
                "LEFT JOIN CustomerProfiles cp ON cp.CustomerID = lt.CustomerID "
                "ORDER BY lt.CreatedAt DESC"
            )
            rows = cursor.fetchall()
            if rows:
                table_rows = [
                    (r.ID, r.CustomerID,
                     f"{r.LastName or '?'}, {r.FirstName or '?'}", r.RoomNumber or "-",
                     r.Delta, r.Reason, r.CreatedAt, r.SourceID)
                    for r in rows
                ]
                ui.show_table(
                    "Loyalty Transactions",
                    ["ID", "Cust ID", "Guest", "Room", "Delta", "Reason", "At", "Source"],
                    table_rows,
                )
            else:
                ui.info("No loyalty transactions found.")
    except Exception as e:
        logging.error(f"Error viewing loyalty transactions: {e}")


def admin_adjust_loyalty_points():
    """Admin: add or remove points for a GUEST."""
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled.")
        return
    customer_id = _pick_loyalty_customer()
    if not customer_id:
        return
    try:
        current = get_points_by_customer(customer_id)
        logging.info(f"Current points for customer {customer_id}: {current}")
        try:
            delta = int(input("Enter points to add (positive) or remove (negative): ").strip())
        except ValueError:
            logging.info("Invalid points value.")
            return
        if delta == 0:
            logging.info("No change requested.")
            return
        if delta > 0:
            add_points_to_customer(customer_id, delta, reason='admin_adjust')
            core.log_audit("ADJUST", "LoyaltyAccount", f"customer:{customer_id}", f"+{delta} points (admin adjust)")
            logging.info(f"Added {delta} points to customer {customer_id}.")
        else:
            # Removing points - ensure we don't go negative
            remove = min(current, abs(delta))
            if remove <= 0:
                logging.info("No points to remove.")
                return
            success = redeem_points_by_customer(customer_id, remove, reason='admin_remove')
            if success:
                core.log_audit("ADJUST", "LoyaltyAccount", f"customer:{customer_id}", f"-{remove} points (admin remove)")
                logging.info(f"Removed {remove} points from customer {customer_id}.")
            else:
                logging.info("Failed to remove points.")
    except Exception as e:
                logging.error(f"Error adjusting loyalty points: {e}")


def admin_manage_loyalty_tiers():
    """Admin: view and edit loyalty tier thresholds/perks."""
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled.")
        return
    while True:
        tiers = _tiers_from_db() or core.DEFAULT_TIERS
        ui.show_table("Loyalty Tiers", ["Tier", "Min Lifetime Points", "Points x", "Discount %", "Perks"],
                      [(t[0], t[1], f"x{t[2]:.2f}", f"{t[3]:.0f}%", t[4]) for t in tiers])
        ui.pause()
        ui.show_menu("Tier Management", [
            "1. Edit Tier Discount Percentage",
            "2. Edit Tier Points Multiplier",
            "3. Edit Tier Minimum Points",
            "4. Back to Loyalty Management",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            name = input("Enter tier name to edit (e.g. Silver, Gold, Platinum): ").strip()
            match = None
            for t in tiers:
                if t[0].lower() == name.lower():
                    match = t
                    break
            if match is None:
                logging.info("Tier not found.")
                continue
            try:
                new_discount = float(input(f"New discount percent for {match[0]} (current {match[3]:.0f}%): ").strip())
            except ValueError:
                logging.info("Invalid number.")
                continue
            new_discount = max(0.0, min(100.0, new_discount))
            with db.get_connection() as conn:
                if conn is None:
                    continue
                try:
                    conn.cursor().execute("UPDATE LoyaltyTiers SET DiscountPercent = ? WHERE TierName = ?", (new_discount, match[0]))
                    conn.commit()
                    logging.info(f"Updated {match[0]} discount to {new_discount:.0f}%.")
                    admin_recompute_all_tiers()
                except Exception as e:
                    logging.error(f"Error updating tier discount: {e}")
        elif choice == '2':
            name = input("Enter tier name to edit (e.g. Silver, Gold, Platinum): ").strip()
            match = None
            for t in tiers:
                if t[0].lower() == name.lower():
                    match = t
                    break
            if match is None:
                logging.info("Tier not found.")
                continue
            try:
                new_mult = float(input(f"New points multiplier for {match[0]} (current x{match[2]:.2f}): ").strip())
            except ValueError:
                logging.info("Invalid number.")
                continue
            new_mult = max(1.0, new_mult)
            with db.get_connection() as conn:
                if conn is None:
                    continue
                try:
                    conn.cursor().execute("UPDATE LoyaltyTiers SET PointsMultiplier = ? WHERE TierName = ?", (new_mult, match[0]))
                    conn.commit()
                    logging.info(f"Updated {match[0]} multiplier to x{new_mult:.2f}.")
                    admin_recompute_all_tiers()
                except Exception as e:
                    logging.error(f"Error updating tier multiplier: {e}")
        elif choice == '3':
            name = input("Enter tier name to edit (e.g. Silver, Gold, Platinum): ").strip()
            match = None
            idx = None
            for i, t in enumerate(tiers):
                if t[0].lower() == name.lower():
                    match = t
                    idx = i
                    break
            if match is None:
                logging.info("Tier not found.")
                continue
            try:
                new_min = int(input(f"New minimum lifetime points for {match[0]} (current {match[1]}): ").strip())
            except ValueError:
                logging.info("Invalid number.")
                continue
            if new_min < 0:
                logging.info("Minimum points cannot be negative.")
                continue
            lower = tiers[idx - 1][1] if idx > 0 else None
            upper = tiers[idx + 1][1] if idx + 1 < len(tiers) else None
            if (lower is not None and new_min <= lower) or (upper is not None and new_min >= upper):
                logging.info("Minimum points must keep tiers in ascending order.")
                continue
            with db.get_connection() as conn:
                if conn is None:
                    continue
                try:
                    conn.cursor().execute("UPDATE LoyaltyTiers SET MinLifetimePoints = ? WHERE TierName = ?", (new_min, match[0]))
                    conn.commit()
                    logging.info(f"Updated {match[0]} minimum to {new_min} points.")
                    admin_recompute_all_tiers()
                except Exception as e:
                    logging.error(f"Error updating tier minimum points: {e}")
        elif choice == '4':
            break
        else:
            logging.info("Invalid choice.")


def admin_recompute_all_tiers():
    """Admin: recompute every loyalty account's tier from lifetime points."""
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled.")
        return
    count = recompute_all_tiers()
    logging.info(f"Recalculated tiers for {count} loyalty account(s).")
