# type: ignore
"""items: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from . import db
from . import ui
from . import core
from . import loyalty
from . import rooms

__all__ = [
    'display_items',
    'get_quantity',
    'get_another_item',
    '_parse_pricing_rule',
    'add_item',
    'delete_item',
    'get_dynamic_price',
    'get_item_choice',
    'update_item',
    'view_items',
    'list_items',
    'manage_pricing_rules',
    'comp_item_to_room',
    'ITEM_NAME_MAX_LENGTH',
    'DEFAULT_SEED_ITEMS',
    '_insert_item',
    'seed_default_items',
    '_ask_item_name',
    '_ask_item_price',
    'add_custom_item',
]



def display_items():
    with db.get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT ItemID, Name FROM Items")  # Select only needed columns
            rows = cursor.fetchall()
            table_rows = []
            for row in rows:
                item_id = row[0]
                name = row[1]
                price = get_dynamic_price(item_id)  # Get the price dynamically
                if price is not None:
                    table_rows.append((item_id, name, f"${price:.2f}"))
                else:
                    table_rows.append((item_id, name, "price unavailable"))
            ui.show_table("Service/Item Price", ["ID", "Name", "Price"], table_rows)
        except Exception as e:
            logging.error(f"Error displaying items: {e}")
'''def get_item_choice():
    while True:
        try:
            display_items()
            choice = int(input("Which service/item do you want? "))
            conn = create_connection()
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM Items WHERE ItemID = ?", (choice,))
            item = cursor.fetchone()
            ()
            if item:
                return choice
            else:
                logging.info("Invalid choice. Please try again.")
        except ValueError:
            logging.info("Invalid input. Please enter a number.")
        except Exception as e:
            logging.error(f"Error getting item choice: {e}")'''


## =========================
# Reservation Management
## =========================
def get_quantity():
    while True:
        try:
            quantity = int(input("How many? "))
            if quantity > 0:
                return quantity
            else:
                logging.info("Quantity must be a positive number. Please try again.")
        except ValueError:
            logging.info("Invalid input. Please enter a number.")
        except Exception as e:
            logging.error(f"Error getting quantity: {e}")

def get_another_item():
    while True:
        again = input("Would you like to order another service/item? (Yes/No) ").strip().lower()
        if again in ['yes', 'no']:
            return again == 'yes'
        else:
            logging.info("Invalid input. Please enter 'Yes' or 'No'.")


def _parse_pricing_rule(raw):
    """Map what someone typed for a pricing rule onto what get_dynamic_price() reads.

    Returns 'Peak', 'OffPeak', or None (standard). Blank is the standard rule, not an error,
    which is why add_item() has always allowed it. Shared so the Admin Panel and the
    onboarding catalogue cannot drift on which spellings are accepted.
    """
    value = str(raw or "").strip().lower()
    if value in ('p', 'peak'):
        return 'Peak'
    if value in ('o', 'off', 'offpeak', 'off-peak'):
        return 'OffPeak'
    return None


def add_item():
    try:
        item_id = int(input("Enter item ID: ").strip())
        item_name = input("Enter item name: ").strip()
        item_price = float(input("Enter item price: ").strip())
        rule_raw = input("Enter pricing rule - (P)eak/(O)ffPeak/blank for standard: ").strip().lower()
        pricing_rule = _parse_pricing_rule(rule_raw)
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("INSERT INTO Items (ItemID, Name, Price, PricingRule) VALUES (?, ?, ?, ?)", (item_id, item_name, item_price, pricing_rule))
            conn.commit()
            core.log_audit("CREATE", "Item", item_id,
                      f"'{item_name}' @ {item_price}, rule {pricing_rule or 'standard'}")
            logging.info(f"Item '{item_name}' added successfully with ID {item_id}.")
    except Exception as e:
        logging.error(f"Error adding item: {e}")
    # connection closed by context manager

def delete_item():
    try:
        item_id = int(input("Enter item ID to delete: ").strip())
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Name FROM Items WHERE ItemID = ?", (item_id,))
            existing = cursor.fetchone()
            cursor.execute("DELETE FROM Items WHERE ItemID = ?", (item_id,))
            conn.commit()
            core.log_audit("DELETE", "Item", item_id, f"Deleted {existing[0] if existing else 'unknown item'}")
            logging.info(f"Item with ID {item_id} deleted successfully.")
    except Exception as e:
        logging.error(f"Error deleting item: {e}")
    # connection closed by context manager

def get_dynamic_price(item_id):
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute("SELECT Price, PricingRule FROM Items WHERE ItemID = ?", (item_id,))
            result = cursor.fetchone()

        if result:
            base_price, pricing_rule = result
            base_price = float(base_price)  # Convert Decimal to float
            rule = str(pricing_rule).strip().lower() if pricing_rule else ''
            if rule == 'peak':
                return base_price * core.get_peak_factor()
            elif rule == 'offpeak':
                return base_price * core.get_offpeak_factor()
            else:
                return base_price
        else:
            logging.info("Item not found.")
            return None
    except Exception as e:
        logging.error(f"Error retrieving dynamic price: {e}")
        return None

def get_item_choice():
    while True:
        try:
            display_items()
            choice = int(input("Which service/item do you want? "))
            price = get_dynamic_price(choice)
            if price is not None:
                return choice, price
            else:
                logging.info("Invalid choice. Please try again.")
        except ValueError:
            logging.info("Invalid input. Please enter a number.")
        except Exception as e:
            logging.error(f"Error getting item choice: {e}")

def update_item():
    try:
        item_id = int(input("Enter the item ID to update: ").strip())
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Name, Price, PricingRule FROM Items WHERE ItemID = ?", (item_id,))
            item = cursor.fetchone()
        if not item:
            logging.info("Item not found.")
            return
        new_name = input(f"Enter the new item name (blank to keep '{item.Name}'): ").strip()
        if new_name == "":
            new_name = item.Name
        price_raw = input(f"Enter the new item price (blank to keep {item.Price}): ").strip()
        if price_raw == "":
            new_price = item.Price
        else:
            new_price = float(price_raw)
        current_rule = item.PricingRule if item.PricingRule else 'standard'
        rule_raw = input(f"Enter pricing rule - (P)eak/(O)ffPeak/(S)tandard (blank to keep '{current_rule}'): ").strip().lower()
        if rule_raw == "":
            new_rule = item.PricingRule
        elif rule_raw in ('p', 'peak'):
            new_rule = 'Peak'
        elif rule_raw in ('o', 'off', 'offpeak', 'off-peak'):
            new_rule = 'OffPeak'
        elif rule_raw in ('s', 'standard', 'none', 'clear', 'no rule'):
            new_rule = None
        else:
            logging.info("Invalid pricing rule. Setting standard (no rule).")
            new_rule = None
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("UPDATE Items SET Name = ?, Price = ?, PricingRule = ? WHERE ItemID = ?", (new_name, new_price, new_rule, item_id))
            conn.commit()
            core.log_audit("UPDATE", "Item", item_id,
                      f"'{new_name}' @ {new_price}, rule {new_rule or 'standard'}")
            logging.info(f"Item with ID {item_id} updated successfully.")
    except Exception as e:
        logging.error(f"Error updating item: {e}")

def list_items():
    """(ItemID, Name, Price, PricingRule) rows -- the catalogue the menu and the web read."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute("SELECT ItemID, Name, Price, PricingRule FROM Items")
            return [(r[0], r[1], r[2], r[3]) for r in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error loading items: {e}")
        return []


def view_items():
    """Display all items."""
    rows = list_items()
    ui.show_table("Items", ["ID", "Name", "Price", "Pricing Rule"],
                  [(r[0], r[1], f"${r[2]:.2f}", r[3]) for r in rows])


def manage_pricing_rules():
    """Admin: view and edit nightly room rates, peak/off-peak factors, tax, and loyalty rates."""
    while True:
        ui.pause()
        ui.show_menu("Pricing & Settings", [
            "1. View Current Settings",
            "2. Edit Peak / Off-Peak Price Factors",
            "3. Edit Tax Rate",
            "4. Edit Loyalty Points Rates",
            "5. Edit Room-Type Points Multipliers",
            "6. View / Edit Room Types & Nightly Rates",
            "7. Edit Booking Cancellation Policy",
            "8. Edit General Settings (name, lockout, guest attempts, loyalty on/off)",
            "9. Reset All Settings To Seeded Defaults",
            "10. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            order_rate, room_rate = loyalty.points_per_dollar_order_vs_room(
                core.get_loyalty_accrual_points_per_unit(), core.get_loyalty_points_per_night())
            rows = [
                ("Business date (the app's 'today')", str(core.business_date())),
                ("Peak price factor", f"{core.get_peak_factor():.2f}"),
                ("Off-peak price factor", f"{core.get_offpeak_factor():.2f}"),
                ("Tax rate", f"{core.get_tax_rate():.2f}"),
                ("Loyalty order accrual (points per $1 of F&B)", f"{order_rate:g}"),
                ("Loyalty stay accrual (points per night)", core.get_loyalty_points_per_night()),
                ("Loyalty redemption (points per $1 credit)", core.get_loyalty_redemption_points_per_currency_unit()),
                ("Booking free-cancellation (days before check-in)", core.get_booking_refund_cutoff_days()),
                ("Hotel name", core.get_hotel_name()),
                ("Loyalty programme", "on" if core.get_loyalty_enabled() else "off"),
                ("Admin lockout after N failed attempts", core.get_lockout_threshold()),
                ("Admin lockout length (minutes)", core.get_lockout_duration()),
                ("Guest login attempts before stop", core.get_customer_login_max_attempts()),
            ]
            ui.show_table("Current Settings", ["Setting", "Value"], rows)
            ui.show_table("Room Types & Nightly Rates", ["Room Type", "Nightly Rate"],
                          [(rt, f"${rate:,.2f}") for rt, rate in rooms.get_room_types()])
            ui.show_table("Room-Type Points Multipliers", ["Room Type", "Multiplier"],
                          [(rt, f"x{mult:.2f}") for rt, mult in rooms.get_all_room_type_multipliers()])
            ui.info(f"Earn rate check: a Standard night buys {room_rate:.2f} points per $1, "
                    f"so $1 of room service at {order_rate:g} is the smaller prize. It should "
                    f"be -- the room is the thing being bought.")
        elif choice == '2':
            logging.info(f"Current peak factor: {core.get_peak_factor():.2f} (sale price = base x factor)")
            logging.info(f"Current off-peak factor: {core.get_offpeak_factor():.2f} (sale price = base x factor)")
            try:
                peak = float(input("Enter new peak factor (e.g. 1.20): ").strip())
                offpeak = float(input("Enter new off-peak factor (e.g. 0.90): ").strip())
            except ValueError:
                logging.info("Invalid factor. Please enter a number.")
                continue
            if peak <= 0 or offpeak <= 0:
                logging.info("Factors must be positive numbers.")
                continue
            core.set_setting('peak_factor', peak)
            core.set_setting('offpeak_factor', offpeak)
        elif choice == '3':
            logging.info(f"Current tax rate: {core.get_tax_rate()} ({core.get_tax_rate()*100:.0f}% tax on purchases).")
            try:
                tax = float(input("Enter new tax rate as a decimal (e.g. 0.13 for 13%): ").strip())
            except ValueError:
                logging.info("Invalid tax rate. Please enter a number.")
                continue
            if tax < 0:
                logging.info("Tax rate cannot be negative.")
                continue
            core.set_setting('tax_rate', tax)
        elif choice == '4':
            order_rate, room_rate = loyalty.points_per_dollar_order_vs_room(
                core.get_loyalty_accrual_points_per_unit(), core.get_loyalty_points_per_night())
            logging.info(f"Current: {core.get_loyalty_points_per_night()} pts per night, "
                         f"{order_rate:g} pts per $1 spent on orders, "
                         f"{core.get_loyalty_redemption_points_per_currency_unit()} pts per $1 credit.")
            logging.info(f"For calibration: a Standard night is worth {room_rate:.2f} pts per $1, "
                         f"so the order rate above should stay BELOW that. A guest who is "
                         f"billed more per dollar for a drink than for the room they slept in "
                         f"is a setting that has been mis-tuned.")
            try:
                per_night = int(input("Enter base points awarded per night (e.g. 100): ").strip())
                accrual = float(input("Enter points per $1 spent on orders (e.g. 0.5): ").strip())
                redemption = int(input("Enter points needed per $1 discount (e.g. 100): ").strip())
            except ValueError:
                logging.info("Invalid value. The per-night and redemption rates must be "
                             "whole numbers; the order accrual may be a fraction (e.g. 0.5).")
                continue
            if per_night < 0 or accrual < 0 or redemption <= 0:
                logging.info("Invalid values: per-night >= 0, order accrual >= 0, redemption > 0.")
                continue
            if accrual > room_rate:
                logging.info(f"{accrual:g} pts per $1 of room service is MORE than the {room_rate:.2f} "
                             f"pts per $1 the room itself earns. Refusing: a guest would earn "
                             f"more by ordering a coffee than by staying.")
                continue
            core.set_setting('loyalty_points_per_night', per_night)
            core.set_setting('loyalty_accrual_points_per_unit', accrual)
            core.set_setting('loyalty_redemption_points_per_currency_unit', redemption)
        elif choice == '5':
            ui.show_table("Room-Type Points Multipliers", ["Room Type", "Multiplier"],
                          [(rt, f"x{mult:.2f}") for rt, mult in rooms.get_all_room_type_multipliers()])
            room_type = input("Enter the room type to edit (blank to cancel): ").strip()
            if not room_type:
                continue
            if room_type not in core.DEFAULT_ROOM_TYPE_MULTIPLIERS:
                logging.info("Unknown room type. Use one of: " + ", ".join(core.DEFAULT_ROOM_TYPE_MULTIPLIERS.keys()))
                continue
            try:
                new_mult = float(input(f"New multiplier for {room_type} (current x{rooms.get_room_type_multiplier(room_type):.2f}): ").strip())
            except ValueError:
                logging.info("Invalid multiplier. Please enter a number.")
                continue
            if new_mult <= 0:
                logging.info("Multiplier must be positive.")
                continue
            core.set_setting(rooms._room_type_setting_key(room_type), new_mult)
        elif choice == '6':
            rooms.view_room_type_rates()
        elif choice == '7':
            current = core.get_booking_refund_cutoff_days()
            logging.info(f"Guests can cancel free up to {current} day(s) before check-in. "
                         f"Cancelling later keeps the deposit as a fee. 0 = no free window.")
            try:
                days = int(input("Enter the free-cancellation window in days: ").strip())
            except ValueError:
                logging.info("Invalid value. Please enter a whole number of days.")
                continue
            if days < 0:
                logging.info("The cancellation window cannot be negative.")
                continue
            core.set_setting('booking_refund_cutoff_days', days)
        elif choice == '8':
            core._edit_general_settings()
        elif choice == '9':
            core.reset_settings_to_defaults()
        elif choice == '10':
            break
        else:
            logging.info("Invalid choice. Please try again.")

   
def comp_item_to_room():
    """Staff: post a $0 'complimentary' line to the guest's folio, gated on their tier.

    Only posts when the stay's tier's Perks text actually advertises a comped item
    (breakfast / spa credit). The comp is a $0 F&B line -- visible on the folio so the
    guest sees the perk applied -- and it earns nothing: award_billed_order_points()
    pays on Amount x rate x multiplier, and Amount is zero, so a comp silently
    accruing points is impossible rather than filtered. No new Perks table; this is
    the scoped enforcement tier perks were promised but not honored.
    """
    if not core.get_loyalty_enabled():
        logging.info("Loyalty is off, so no tier perks apply.")
        return
    room_number, first_name = core.validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify the guest. No comp posted.")
        return
    details = loyalty.get_tier_details_by_room(room_number)
    if not details:
        logging.info("No loyalty account for this stay; nothing to comp.")
        return
    perks = (details.get("perks") or "").lower()
    logging.info(f"{details['tier']} perks on this stay: {details.get('perks') or 'none'}")
    comped = [p for p in ("complimentary breakfast", "spa credit") if p in perks]
    if not comped:
        logging.info("This tier has no comped items (breakfast or spa credit), so the "
                     "perk cannot be posted. The check stays at the full rate.")
        return
    logging.info("Comped perk(s) this stay qualifies for: " + ", ".join(comped))
    try:
        item_choice = input("ItemID of the complimentary item (blank to cancel): ").strip()
        if not item_choice:
            return
        item_id = int(item_choice)
        quantity = int(input("Quantity: ").strip() or "1")
    except ValueError:
        logging.info("Invalid item or quantity.")
        return
    if quantity <= 0:
        logging.info("Quantity must be positive.")
        return
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Name FROM Items WHERE ItemID = ?", (item_id,))
            row = cursor.fetchone()
            if not row:
                logging.info(f"Item {item_id} not found.")
                return
            cursor.execute(
                "INSERT INTO Transactions (RoomNumber, ItemID, Quantity, UnitPrice, Amount, IsBilled, ChargeGroup, Description) "
                "VALUES (?, ?, ?, 0, 0, 0, 'F&B', ?)",
                (room_number, item_id, quantity, f"Comp: {row[0]} ({details['tier']} perk)"),
            )
            conn.commit()
            core.log_audit("CREATE", "Transaction", str(item_id),
                      f"Complimentary comp posted to room {room_number}: {row[0]} x{quantity} ({details['tier']} perk)")
            logging.info(f"Posted $0 complimentary line for {row[0]} x{quantity} to room {room_number}.")
    except Exception as e:
        logging.error(f"Error posting comp: {e}")

## =========================
# First-Run Onboarding
## =========================
# docs/ONBOARDING.md is the staff runbook; this section is the same runbook, executable.
# They have to agree -- the wizard automates the steps that could be automated, and the
# doc still documents the ones that could not (applying database.sql, the SQL Server
# itself, anything to do with config.ini).

# Starter catalogue offered by the wizard. `Items.ItemID` is a plain int primary key and
# not an identity column, so these ids are only a starting suggestion -- seed_default_items
# walks them past anything already present.
#
# PricingRule is Peak/OffPeak/blank, applied by get_dynamic_price(). ChargeGroup is NOT set
# here and must not be: it is written on the Transactions row when the charge is billed
# ('Room' for the room charge, 'F&B' for anything ordered), and getting it wrong
# double-counts loyalty points.
# Items.Name is nvarchar(100). Prompting for something longer would hand the operator a
# pyodbc truncation error rather than a message, so the bound is checked before the INSERT.
ITEM_NAME_MAX_LENGTH = 100

# Starter catalogue. The ItemIDs are only *suggestions* -- seed_default_items() continues
# from MAX(ItemID)+1 -- so they just need to be distinct and stable within this tuple.
#
# Deliberately NOT seeded here: a "Room Charge" item. The app posts the room charge itself
# at check-out as a Transactions row with ItemID = NULL and ChargeGroup = 'Room', guarded
# against double-posting (docs/SCHEMA.md, "Items"). An orderable "Room Charge" item would be
# a double-charge footgun: record_transaction_for_room() defaults ChargeGroup to 'F&B', so
# adding it through Order Management posts an F&B line on top of the automatic room charge
# AND accrues loyalty points on it. Every row in this tuple is genuinely sellable, which is
# also what makes "COUNT(*) > 0" a correct readiness test for a catalogue.
DEFAULT_SEED_ITEMS = (
    (1, "Breakfast Buffet", 18.00, "Peak"),
    (2, "Room Service Dinner", 32.00, None),
    (3, "Coffee & Tea", 6.50, None),
    (4, "Minibar Restock", 15.00, None),
    (5, "Laundry Service", 22.00, None),
    (6, "Airport Transfer", 55.00, "Peak"),
)


def _insert_item(cursor, item_id, name, price, pricing_rule):
    """One INSERT for Items, shared so the two onboarding paths cannot disagree on shape.

    Takes an open cursor rather than opening its own connection so a whole catalogue can be
    written in a single transaction. Never commits -- the caller decides, so a catalogue
    that fails halfway does not leave half of it applied.
    """
    cursor.execute(
        "INSERT INTO Items (ItemID, Name, Price, PricingRule) VALUES (?, ?, ?, ?)",
        (int(item_id), str(name), round(float(price), 2), pricing_rule))


def seed_default_items():
    """Insert the starter catalogue. Returns how many rows were actually added.

    Starts from MAX(ItemID)+1 rather than from 1, which is what keeps a re-run against a
    hotel that already has hand-added items from dying on a duplicate key. Skips by NAME,
    so an item the operator already added under the same name is left exactly as it is even
    when our id would have been free. Returns 0 when there was nothing to do and also on
    failure; check the log if you asked for items and got none.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute("SELECT Name FROM Items")
            existing_names = {str(r[0]).strip().lower() for r in cursor.fetchall() if r[0]}
            cursor.execute("SELECT ISNULL(MAX(ItemID), 0) FROM Items")
            row = cursor.fetchone()
            next_id = (int(row[0]) if row and row[0] is not None else 0) + 1
            added = 0
            for _suggested_id, name, price, rule in DEFAULT_SEED_ITEMS:
                if name.strip().lower() in existing_names:
                    continue
                _insert_item(cursor, next_id, name, price, rule)
                next_id += 1
                added += 1
            if added:
                conn.commit()
                core.log_audit("CREATE", "Item", "starter-catalogue",
                          f"Added {added} item(s) during first-run onboarding")
                logging.info(f"Added {added} item(s) to the catalogue.")
            return added
    except Exception as e:
        logging.error(f"Error seeding items: {e}")
        return 0


def _ask_item_name(existing_names):
    """Prompt for an item name until one is usable. Returns (name, None) or (None, None).

    Two things make this stricter than add_item()'s prompt. A blank name cannot be offered in
    an order menu, and Name is nvarchar(100) -- a longer value is a pyodbc "string or binary
    data would be truncated" error rather than a message, which is exactly the traceback
    AGENTS.md section 3 says a mistyped prompt must never produce.

    A name that already exists only warns. There is no UNIQUE constraint on Name and
    "Espresso" / "Espresso (large)" is a legitimate pair, so this asks rather than refuses.
    """
    while True:
        raw = input("Item name: ").strip()
        if not raw:
            logging.info("An item needs a name -- it is what shows up on the guest's folio.")
            continue
        if len(raw) > ITEM_NAME_MAX_LENGTH:
            logging.info("That name is %d characters; the database holds %d. Shorten it.",
                         len(raw), ITEM_NAME_MAX_LENGTH)
            continue
        if raw.lower() in existing_names:
            logging.info("An item called '%s' already exists.", raw)
            if not ui.ask_confirmation("Add it as a separate item anyway?", default="n"):
                continue
        return raw, None


def _ask_item_price():
    """Prompt for a price until one parses. Returns a float rounded to cents, or None to quit.

    decimal(10,2) is the column, so the value is rounded to 2dp rather than letting the
    driver round or reject it. A parse failure re-asks instead of raising, for the same
    reason as the name.
    """
    while True:
        raw = input("Price (blank to stop adding items): ").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            logging.info("'%s' is not a number. Enter a price like 18.50", raw)
            continue
        if value < 0:
            logging.info("A price cannot be negative. Enter 0 for a free item.")
            continue
        if value > 99999999.99:
            logging.info("That price is beyond what the database can store.")
            continue
        return round(value, 2)


def add_custom_item(existing_names=None):
    """Add one operator-defined item to the catalogue. Returns True if a row was written.

    Assigns ItemID as MAX(ItemID)+1 rather than asking. Items.ItemID is a plain int PK, not
    an identity column, so a typed id can collide with a row that already exists and the
    operator gets a constraint-violation traceback instead of an item; auto-assigning makes
    that failure mode unreachable and keeps ids contiguous. Someone who needs a deliberate
    numbering scheme can still use Admin Panel -> 13, which does take an id.

    One item per call, so the caller owns the loop and the "I am finished" decision.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            if existing_names is None:
                cursor.execute("SELECT Name FROM Items")
                existing_names = {str(r[0]).strip().lower()
                                  for r in cursor.fetchall() if r[0]}
            name, _ = _ask_item_name(existing_names)
            if not name:
                return False
            price = _ask_item_price()
            if price is None:
                logging.info("No price given, so no item was added.")
                return False
            rule_raw = input("Pricing rule - (P)eak/(O)ffPeak/blank for standard: ")
            rule = _parse_pricing_rule(rule_raw)
            cursor.execute("SELECT ISNULL(MAX(ItemID), 0) FROM Items")
            row = cursor.fetchone()
            item_id = (int(row[0]) if row and row[0] is not None else 0) + 1
            _insert_item(cursor, item_id, name, price, rule)
            conn.commit()
            core.log_audit("CREATE", "Item", item_id,
                      f"'{name}' @ {price}, rule {rule or 'standard'} (onboarding)")
            logging.info(f"Added '{name}' as item {item_id}.")
            return True
    except KeyboardInterrupt:
        logging.info("Cancelled. No item was added.")
        return False
    except Exception as e:
        logging.error(f"Error adding item: {e}")
        return False
