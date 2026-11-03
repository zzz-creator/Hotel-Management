# type: ignore
"""onboarding: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
from hotel import db
from hotel import ui
from hotel import core
from hotel import items
from hotel import loyalty
from hotel import rooms
from hotel import orders
from hotel import conference

__all__ = [
    'ONBOARDING_SETTING',
    'ROOM_TYPE_NAME_MAX_LENGTH',
    'onboarding_completed',
    'mark_onboarding_complete',
    'create_first_user',
    'setup_status',
    '_offer_hotel_settings',
    '_offer_room_rates',
    '_offer_staff_accounts',
    '_offer_reference_data',
    '_create_staff_account',
    '_setup_summary_rows',
    '_wizard_step_menu',
    'run_first_run_onboarding',
    '_count_tower_rooms',
    'onboarding_checklist',
    '_offer_item_catalogue',
    '_offer_room_layout',
]


# config.ini carries ONLY the database connection. Everything hotel-specific (name, tax,
# lockout, loyalty rates) lives in the HotelSettings table and is edited from the admin
# "Pricing & Settings" menu -- see docs/SCHEMA.md. _ensure_database_config()
# prompts for these values and writes config.ini when it is missing.
# The floor for "which day is it", used only when HotelSettings has no usable
# business_date row (before migration 023, or if an admin blanks it).
# The 'business_date' HotelSettings row (migration 023) is historical: the app no longer
# reads or writes it; "today" is the wall clock. See docs/DEVIATIONS.md §3 (rewritten 5 Oct 2026).
# HotelSettings key recording that the first-run wizard finished. Nothing seeds this row on
# purpose: ABSENCE is what "not onboarded yet" means, so a fresh database is correctly
# read as needing setup without anyone having had to remember to insert a '0' first.
ONBOARDING_SETTING = "onboarding_complete"


def onboarding_completed():
    """True when the first-run wizard has already finished on this database.

    Reads one HotelSettings row. A missing table (migration 023 not applied), a missing
    row, a blank value, and any failure at all all read as "not completed", which is the
    safe direction: the alternative is refusing to run setup because the settings table is
    unreadable, which would strand a fresh install that nobody can log into.
    """
    try:
        raw = core.get_setting(ONBOARDING_SETTING, None)
    except Exception as e:
        logging.debug(f"Onboarding marker unreadable ({type(e).__name__}: {e}); treating as incomplete.")
        return False
    return str(raw if raw is not None else "").strip().lower() in ("1", "true", "yes", "y")


def mark_onboarding_complete():
    """Record that the wizard finished. `set_setting()` writes the AuditLog row for us."""
    return core.set_setting(ONBOARDING_SETTING, "1")


def create_first_user(username, password, role="admin"):
    """Create a login directly, bypassing the Admin Panel.

    This is the step the wizard exists for. `Users` is seeded by nothing -- not by
    database.sql, not by any migration -- and the only in-app way to add one, `add_user()`,
    lives inside the Admin Panel, which needs a login to reach. So the first account used
    to be a hand-written INSERT.

    Refuses outright when ANY account already exists. That guard is the point of the
    function rather than a nicety: during first-run this is reachable without a login, and
    it must never quietly become "mint another admin on a live system". Use the Admin
    Panel for everyone after the first.

    Returns True on success, False if refused or on error. Passwords are stored and
    compared in plaintext on purpose (AGENTS.md section 3 -- teaching project).
    """
    username = str(username or "").strip()
    password = str(password if password is not None else "")
    if not username or not password:
        logging.info("A username and a password are both required.")
        return False
    if role not in ("admin", "staff", "manager", "master"):
        logging.info(f"'{role}' is not a role this app uses.")
        return False
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM Users")
            existing = cursor.fetchone()
            if existing and existing[0] is not None and int(existing[0]) > 0:
                logging.info("Accounts already exist. Add more from the Admin Panel, "
                             "not from setup.")
                return False
            # Same three columns as add_user(), so the two paths cannot drift apart.
            cursor.execute("INSERT INTO Users (Username, Password, Role) VALUES (?, ?, ?)",
                           (username, password, role))
            conn.commit()
            core.log_audit("CREATE", "User", username, f"Role {role} (first-run onboarding)")
            logging.info(f"Account '{username}' created with role '{role}'.")
            return True
    except Exception as e:
        logging.error(f"Error creating account: {e}")
        return False


def setup_status():
    """A readiness checklist for this hotel. Returns a list of (label, done, detail) rows.

    Derived from live data on purpose rather than read off the onboarding marker. The
    marker answers "has a human been through the wizard"; this answers the question the
    runbook actually cares about, which is "can the front desk take a booking a guest can
    pay for". Someone who inherited this database and never ran the wizard still needs the
    second question answered.

    Every probe stands alone and swallows its own failure, so an unapplied migration reads
    as one red row carrying the database's own error rather than a dead screen.
    """
    checks = []

    users = core._scalar_count("SELECT COUNT(*) FROM Users")
    if users is None:
        checks.append(("Staff accounts", False, "Users is unreachable -- migration 001 applied?"))
    elif users == 0:
        checks.append(("Staff accounts", False, "No logins exist yet"))
    else:
        checks.append(("Staff accounts", True, f"{users} account(s)"))

    # The master row is the only thing that makes require_master_override() work on a
    # fresh install -- with no master_secret anywhere else, the 'master' account IS the
    # override.
    master = core._scalar_count("SELECT COUNT(*) FROM Users WHERE Username = ?", ("master",))
    if master:
        checks.append(("Master override account", True, "'master' exists"))
    else:
        checks.append(("Master override account", False,
                       "no 'master' login -- the override will refuse every caller"))

    items = core._scalar_count("SELECT COUNT(*) FROM Items")
    if items is None:
        checks.append(("Item catalogue", False, "Items is unreachable -- migration 006 applied?"))
    elif items == 0:
        # Every Items row is a sellable line; the room charge is posted separately at
        # check-out with ItemID = NULL, so it is not missing from here and not counted.
        checks.append(("Item catalogue", False,
                       "empty -- nothing can be ordered until one sellable item exists"))
    else:
        checks.append(("Item catalogue", True, f"{items} sellable item(s)"))

    rooms = core._scalar_count("SELECT COUNT(*) FROM Rooms")
    if rooms is None:
        checks.append(("Rooms", False, "Rooms is unreachable -- migration 008 applied?"))
    elif rooms == 0:
        checks.append(("Rooms", False,
                       "empty -- the dashboard and housekeeping board have nothing to show"))
    else:
        checks.append(("Rooms", True, f"{rooms} room(s)"))

    # Conference rooms are optional for a hotel with no meeting space, so an empty
    # table is green-but-noted (like reference data), while an unapplied migration
    # (034) is red because the table itself is unreachable.
    conf_rooms = core._scalar_count("SELECT COUNT(*) FROM ConferenceRooms")
    if conf_rooms is None:
        checks.append(("Conference rooms", False,
                       "ConferenceRooms is unreachable -- migration 034 applied?"))
    elif conf_rooms == 0:
        checks.append(("Conference rooms", True,
                       "none configured (optional) -- add from Admin Panel -> Rooms & Housekeeping -> Conference Rooms"))
    else:
        unpriced = core._scalar_count(
            "SELECT COUNT(*) FROM ConferenceRooms WHERE ISNULL(HourlyRate, 0) <= 0")
        no_setup = core._scalar_count(
            "SELECT COUNT(*) FROM ConferenceRooms c WHERE NOT EXISTS "
            "(SELECT 1 FROM ConferenceRoomSetups s WHERE s.RoomID = c.RoomID)")
        if unpriced:
            checks.append(("Conference rooms", False,
                           f"{unpriced} of {conf_rooms} room(s) have no hourly rate"))
        elif no_setup:
            checks.append(("Conference rooms", False,
                           f"{no_setup} room(s) have no setup type"))
        else:
            checks.append(("Conference rooms", True,
                           f"{conf_rooms} room(s), all priced with setups"))

    # A rate of 0 (or NULL) is as unusable as a missing row: check-out would post a
    # zero-dollar room charge. Only ACTIVE categories matter -- a retired category with no
    # price is not something anyone can book.
    unpriced = core._scalar_count(
        "SELECT COUNT(*) FROM RoomTypes WHERE Active = 1 AND ISNULL(NightlyRate, 0) <= 0")
    active_rates = core._scalar_count("SELECT COUNT(*) FROM RoomTypes WHERE Active = 1")
    if unpriced is None or active_rates is None:
        checks.append(("Nightly rates", False, "RoomTypes is unreachable -- migration 013 applied?"))
    elif active_rates == 0:
        checks.append(("Nightly rates", False,
                       "no active room category -- nothing can be priced at check-out"))
    elif unpriced:
        checks.append(("Nightly rates", False,
                       f"{unpriced} of {active_rates} active category(s) have no rate -- "
                       "check-out would post a zero-dollar room charge"))
    else:
        checks.append(("Nightly rates", True, f"all {active_rates} active category(s) priced"))

    if core.get_loyalty_enabled():
        tiers = core._scalar_count("SELECT COUNT(*) FROM LoyaltyTiers")
        if tiers is None:
            checks.append(("Loyalty tiers", False, "LoyaltyTiers is unreachable"))
        elif tiers == 0:
            checks.append(("Loyalty tiers", False, "no tiers -- nothing to promote anyone into"))
        else:
            checks.append(("Loyalty tiers", True, f"{tiers} tier(s)"))

    # Item 2: every core HotelSettings row present and parseable. The getters fall
    # back silently, so a missing or garbage row looks identical to a valid one unless
    # someone reads the raw values -- which is exactly what this probe does.
    # Every HotelSettings row, not just the core defaults: a typo'd value under a
    # loyalty_mult_<slug> or a booking_* key silently falls back through
    # get_room_type_multiplier()/the _setting_* helpers, so the fallback can hide a
    # garbage row indefinitely. Missing core keys are red; unparseable values are red.
    all_rows = {}
    try:
        with db.get_connection() as conn:
            if conn is not None:
                cur = conn.cursor()
                cur.execute("SELECT SettingKey, SettingValue FROM HotelSettings")
                for r in cur.fetchall():
                    all_rows[str(r[0])] = r[1]
    except Exception:
        all_rows = {}

    bad = []
    # Parseable is not the same as usable: tax_rate = "13" parses and then charges 1300%,
    # and a zero nightly-points rate silently earns nothing. The getters fall back on a
    # blank or garbage value, so without a range check a plausible typo can sit here
    # indefinitely. Bounds mirror what _offer_hotel_settings() accepts.
    ranges = {
        'tax_rate': (lambda v: 0 <= v <= 1, "between 0 and 1 (0.13 = 13%)"),
        'peak_factor': (lambda v: v > 0, "greater than 0"),
        'offpeak_factor': (lambda v: v > 0, "greater than 0"),
        'loyalty_points_per_night': (lambda v: v >= 0, "0 or more"),
        'loyalty_accrual_points_per_unit': (lambda v: v >= 0, "0 or more"),
        'loyalty_redemption_points_per_currency_unit': (lambda v: v > 0, "greater than 0"),
        'lockout_threshold': (lambda v: v > 0, "greater than 0"),
        'lockout_duration': (lambda v: v > 0, "greater than 0"),
        'customer_login_max_attempts': (lambda v: v > 0, "greater than 0"),
        'booking_refund_cutoff_days': (lambda v: v >= 0, "0 or more"),
    }
    for key in core.DEFAULT_HOTEL_SETTINGS:
        raw = all_rows.get(key, None)
        if raw is None or str(raw).strip() == '':
            bad.append(f"{key} missing")
            continue
        text = str(raw).strip()
        if key == 'hotel_name':
            continue
        if key == 'loyalty_enabled':
            if text.lower() not in ('1', '0', 'true', 'false', 'yes', 'no', 'y', 'n', 'on', 'off'):
                bad.append(f"loyalty_enabled not a boolean: {raw!r}")
            continue
        try:
            value = float(text)
        except (TypeError, ValueError):
            bad.append(f"{key} not a number: {raw!r}")
            continue
        in_range = ranges.get(key)
        if in_range and not in_range[0](value):
            bad.append(f"{key} out of range ({in_range[1]}): {raw!r}")

    for key, raw in all_rows.items():
        if key in core.DEFAULT_HOTEL_SETTINGS or key == 'business_date':
            continue
        if raw is None or str(raw).strip() == '':
            continue
        try:
            float(str(raw))
        except (TypeError, ValueError):
            bad.append(f"{key} not a number: {raw!r}")

    # The F&B earn rate is calibrated against the room rate: points per dollar on an order
    # must stay below points per dollar on a Standard night, or a coffee earns more than a
    # stay. Same rule the Pricing & Settings menu enforces at entry time.
    if not bad and core.get_loyalty_enabled():
        try:
            order_rate = float(str(all_rows.get('loyalty_accrual_points_per_unit', 0)))
            per_night = int(float(str(all_rows.get('loyalty_points_per_night', 0))))
            _order, room_rate = loyalty.points_per_dollar_order_vs_room(order_rate, per_night)
            if order_rate > room_rate:
                bad.append(f"loyalty_accrual_points_per_unit ({order_rate:g}) is above the "
                           f"{room_rate:.2f} pts/$1 a Standard night earns")
        except (TypeError, ValueError):
            pass

    if bad:
        checks.append(("Hotel settings", False, "; ".join(bad)))
    else:
        checks.append(("Hotel settings", True, f"all {len(all_rows)} row(s) present and in range"))

    # Reference data is optional -- an empty amenities list does not stop the desk taking
    # a payment -- so an empty table stays green and reports "none configured". An
    # unapplied migration is different, and is red like every other unreachable table.
    amenities, promotions, discounts = _reference_data_counts()
    if None in (amenities, promotions, discounts):
        checks.append(("Reference data", False,
                       "Amenities/Promotions/Discounts unreachable -- migration 015 applied?"))
    elif not any((amenities, promotions, discounts)):
        checks.append(("Reference data", True,
                       "none configured (optional) -- add amenities, promotions or discount codes"))
    else:
        checks.append(("Reference data", True,
                       f"{amenities} amenit{'y' if amenities == 1 else 'ies'}, "
                       f"{promotions} promotion(s), {discounts} discount code(s)"))

    return checks


def _offer_hotel_settings(wizard=False):
    """Ask the core hotel settings, keeping the current value on a blank answer.

    Shared by the first-run wizard (as its settings step) and safe to call again
    every time: every prompt shows the value an empty answer would keep, so
    skipping through changes nothing, and set_setting() writes an audit row for
    each change that does land. Nothing here is required -- a database seeded
    from database.sql already carries workable defaults.
    """
    logging.info("Press Enter to keep the value in [brackets]. Anything you skip can be "
                 "changed later from Admin Panel → Discounts & Pricing → Manage Pricing & Settings.")

    def _ask(key, label, current, parse, valid):
        raw = input(f"{label} [{current}]: ").strip()
        if not raw:
            return current
        try:
            value = parse(raw)
        except (TypeError, ValueError):
            logging.info("That is not a number -- keeping %s.", current)
            return current
        if not valid(value):
            logging.info("Out of range -- keeping %s.", current)
            return current
        core.set_setting(key, value)
        return value

    _ask('hotel_name', "Hotel name", core.get_hotel_name(), str, lambda v: bool(v.strip()))
    _ask('tax_rate', "Tax rate as a decimal (0.13 = 13%)", f"{core.get_tax_rate():g}",
         float, lambda v: v >= 0)
    _ask('peak_factor', "Peak price factor (sale = base x factor)", f"{core.get_peak_factor():g}",
         float, lambda v: v > 0)
    _ask('offpeak_factor', "Off-peak price factor", f"{core.get_offpeak_factor():g}",
         float, lambda v: v > 0)

    old_accrual, old_per_night = (core.get_loyalty_accrual_points_per_unit(),
                                  core.get_loyalty_points_per_night())
    enabled_raw = core.get_loyalty_enabled()
    raw_enabled = input(f"Loyalty programme enabled? (y/n) [{'y' if enabled_raw else 'n'}]: ").strip().lower()
    if raw_enabled in ('y', 'yes', '1', 'true', 'on', 'n', 'no', '0', 'false', 'off'):
        core.set_setting('loyalty_enabled', '1' if raw_enabled in ('y', 'yes', '1', 'true', 'on') else '0')
    new_per_night = _ask('loyalty_points_per_night', "Points per night stayed",
                         old_per_night, int, lambda v: v >= 0)
    new_accrual = _ask('loyalty_accrual_points_per_unit', "Points per $1 of F&B orders (a fraction, e.g. 0.5)",
                       old_accrual, float, lambda v: v >= 0)
    _ask('loyalty_redemption_points_per_currency_unit', "Points per $1 credit when redeeming",
         core.get_loyalty_redemption_points_per_currency_unit(), int, lambda v: v > 0)
    # The order rate must stay below what a night earns, or guests earn more by ordering
    # a coffee than by staying. Same calibration the Pricing & Settings menu enforces.
    order_rate, room_rate = loyalty.points_per_dollar_order_vs_room(new_accrual, new_per_night)
    if new_accrual > room_rate:
        logging.info("%g pts per $1 of room service is MORE than the %.2f pts per $1 a Standard "
                     "night earns -- restoring the previous accrual rate.", new_accrual, room_rate)
        core.set_setting('loyalty_accrual_points_per_unit', old_accrual)

    _ask('lockout_threshold', "Failed admin logins before lockout", core.get_lockout_threshold(),
         int, lambda v: v > 0)
    _ask('lockout_duration', "Lockout length in minutes", core.get_lockout_duration(),
         int, lambda v: v > 0)
    _ask('customer_login_max_attempts', "Failed guest logins before the menu stops asking",
         core.get_customer_login_max_attempts(), int, lambda v: v > 0)
    _ask('booking_refund_cutoff_days', "Free-cancellation window in days (0 = none)",
         core.get_booking_refund_cutoff_days(), int, lambda v: v >= 0)
    logging.info("Hotel settings saved. Current values: Prices/tax %.2f%%, lockout after %d "
                 "attempts for %d min, loyalty %s.", core.get_tax_rate() * 100,
                 core.get_lockout_threshold(), core.get_lockout_duration(),
                 "on" if core.get_loyalty_enabled() else "off")


# RoomTypes.RoomType is varchar(50). Checked here so a long typed category is refused
# with a message rather than truncated by the ODBC driver.
ROOM_TYPE_NAME_MAX_LENGTH = 50


def _edit_one_room_rate(room_types):
    """Ask for one category's new rate. Returns 1 if it was changed, else 0."""
    name = input("Room type to change (Enter to cancel): ").strip()
    if not name:
        return 0
    match = next((rt for rt, _rate, _active in room_types if rt.lower() == name.lower()), None)
    if match is None:
        logging.info("Unknown room type. Available: "
                     + ", ".join(rt for rt, _rate, _active in room_types))
        return 0
    current = next((rate for rt, rate, _active in room_types if rt == match), 0.0)
    raw = input(f"New nightly rate for {match} [{current:,.2f}]: ").strip()
    if not raw:
        logging.info("Kept the current rate.")
        return 0
    try:
        new_rate = float(raw)
    except ValueError:
        logging.info("That is not a number -- keeping %.2f.", current)
        return 0
    if new_rate < 0:
        logging.info("Nightly rate cannot be negative -- keeping %.2f.", current)
        return 0
    return 1 if rooms.update_room_type_rate(match, new_rate) else 0


def _add_room_category(room_types):
    """Ask for a new category and its rate. Returns 1 if it was changed, else 0."""
    name = input("New room type name (Enter to cancel): ").strip()
    if not name:
        return 0
    if len(name) > ROOM_TYPE_NAME_MAX_LENGTH:
        logging.info("Room type names are limited to %d characters.", ROOM_TYPE_NAME_MAX_LENGTH)
        return 0
    if any(rt.lower() == name.lower() for rt, _rate, _active in room_types):
        logging.info("Room type '%s' already exists -- change its rate instead.", name)
        return 0
    raw = input(f"Nightly rate for {name}: ").strip()
    if not raw:
        logging.info("A rate is required to add a room type.")
        return 0
    try:
        new_rate = float(raw)
    except ValueError:
        logging.info("That is not a number. Nothing was added.")
        return 0
    if new_rate < 0:
        logging.info("Nightly rate cannot be negative. Nothing was added.")
        return 0
    return 1 if rooms.update_room_type_rate(name, new_rate) else 0


def _offer_room_rates(wizard=False):
    """Configure nightly rates for each room category.

    Shared by the first-run wizard and the setup checklist. Rates are captured onto
    reservations at booking time, so a change here only affects stays booked from now on.
    Returns how many categories were changed.
    """
    logging.info("Room rates are the price list for the house. A booking captures the rate "
                 "when it is made, so changing a rate never re-prices a stay already booked.")
    changed = 0
    while True:
        try:
            room_types = rooms.get_room_types(active_only=False, include_status=True)
        except Exception as e:
            logging.error(f"Could not load room types: {e}")
            return changed
        if not room_types:
            logging.info("No room types exist to price.")
            return changed
        ui.show_table("Room Types & Nightly Rates",
                      ["Room Type", "Nightly Rate", "Status"],
                      [(rt, f"${rate:,.2f}", "Active" if active else "Inactive")
                       for rt, rate, active in room_types])
        ui.show_menu("Room rates", [
            "1. Change a category's nightly rate",
            "2. Add a new room category",
            "3. %s" % ("Skip for now" if wizard else "Done -- back to setup"),
        ])
        try:
            choice = input("Enter your choice: ").strip()
        except (KeyboardInterrupt, EOFError):
            logging.info("Finished configuring room rates.")
            return changed
        if choice == "1":
            try:
                changed += _edit_one_room_rate(room_types)
            except (KeyboardInterrupt, EOFError):
                logging.info("Finished configuring room rates.")
                return changed
        elif choice == "2":
            try:
                changed += _add_room_category(room_types)
            except (KeyboardInterrupt, EOFError):
                logging.info("Finished configuring room rates.")
                return changed
        elif choice == "3":
            return changed
        else:
            logging.info("Answer 1, 2 or 3.")



def _create_staff_account():
    """Ask for and create one staff account. Returns True on success.

    The logged-in path, not create_first_user(): by the time this is reachable at least one
    account exists, and create_first_user() deliberately refuses in that case.
    """
    try:
        username = input("Enter new username: ").strip()
        if not username:
            logging.info("A username is required.")
            return False
        if core.user_exists(username):
            logging.info(f"User '{username}' already exists.")
            return False
        password = core._prompt_new_password("Password: ")
        if not password:
            return False
        role = core._prompt_role("Role (a)dmin/(s)taff/(m)anager: ")
        if not role:
            logging.info("No account was created.")
            return False
        return core.add_user_with_password(username, password, role)
    except (KeyboardInterrupt, EOFError):
        logging.info("Cancelled. No account was created.")
        return False


def _offer_staff_accounts(wizard=False):
    """Optionally add staff accounts during setup. Returns how many were added.

    The wizard has already created the first administrator; this is where the rest of the
    desk gets an account in the same sitting, rather than being sent to the Admin Panel.
    """
    added = 0
    while True:
        existing = core._scalar_count("SELECT COUNT(*) FROM Users")
        ui.info("%s account(s) exist. Give each person the narrowest role that lets them do "
                "their job: staff for the desk, manager for the panel, admin for settings."
                % ("Unknown" if existing is None else existing))
        ui.show_menu("Staff accounts", [
            "1. Add a staff account",
            "2. %s" % ("Skip for now" if wizard else "Done -- back to setup"),
        ])
        try:
            choice = input("Enter your choice: ").strip()
        except (KeyboardInterrupt, EOFError):
            logging.info("Finished adding staff accounts.")
            return added
        if choice == "1":
            if _create_staff_account():
                added += 1
        elif choice == "2":
            return added
        else:
            logging.info("Answer 1 or 2.")


def _offer_conference_rooms(wizard=False):
    """Optional conference room setup during onboarding. Returns how many rooms were added."""
    added = 0
    existing = core._scalar_count("SELECT COUNT(*) FROM ConferenceRooms")
    if existing is None:
        logging.info("ConferenceRooms is unreachable -- migration 034 applied?")
        return 0
    if existing:
        logging.info("Conference rooms already exist (%d). Skipping the seed -- manage "
                     "them from Admin Panel -> Rooms & Housekeeping -> Conference Rooms.",
                     existing)
        return 0
    if not ui.ask_confirmation("Configure conference rooms now? (optional)", default="n"):
        logging.info("Skipped. Add them later from Admin Panel -> Rooms & Housekeeping "
                     "-> Conference Rooms.")
        return 0
    while True:
        room_number = input("Room number/label (e.g. CR-101, blank to finish): ").strip()
        if not room_number:
            break
        name = input("Room name: ").strip() or room_number
        try:
            capacity = int(input("Capacity: ").strip())
        except ValueError:
            logging.info("Invalid capacity. Skipping this room.")
            continue
        try:
            rate = float(input("Hourly rate: ").strip())
        except ValueError:
            logging.info("Invalid rate. Skipping this room.")
            continue
        new_id = conference.add_conference_room(room_number, name, capacity, rate)
        if new_id is None:
            logging.info("Could not add that room. Check the log and try again.")
            continue
        added += 1
        # At least one setup type per room, so the room is actually bookable.
        st = input("Setup type (" + ", ".join(conference.CONFERENCE_SETUP_TYPES)
                   + ", blank to skip): ").strip()
        if st:
            try:
                cap = int(input("Capacity for that setup (blank = room capacity): ").strip() or capacity)
            except ValueError:
                cap = capacity
            conference.add_room_setup(room_number, st, cap)
        logging.info("Added conference room %s.", room_number)
    return added


def _reference_data_counts():
    """(amenities, promotions, discounts) row counts, or None where the table is unreachable."""
    return (core._scalar_count("SELECT COUNT(*) FROM Amenities"),
            core._scalar_count("SELECT COUNT(*) FROM Promotions"),
            core._scalar_count("SELECT COUNT(*) FROM Discounts"))


def _offer_reference_data(wizard=False):
    """Optional reference data: amenities, promotions and discount codes.

    Shared by the wizard and the checklist. None of it is required to take a payment, but
    a hotel that skips it has an empty guest menu and no discounts, so a full setup flow
    should offer it rather than hide it behind the Admin Panel.
    """
    while True:
        amenities, promotions, discounts = _reference_data_counts()

        def _fmt(value):
            return "unreachable" if value is None else str(value)

        ui.show_table("Reference data (optional)", ["Table", "Rows"],
                      [("Amenities", _fmt(amenities)),
                       ("Promotions", _fmt(promotions)),
                       ("Discount codes", _fmt(discounts))])
        ui.show_menu("Reference data", [
            "1. Manage amenities",
            "2. Manage promotions",
            "3. Manage discount codes",
            "4. %s" % ("Skip for now" if wizard else "Done -- back to setup"),
        ])
        try:
            choice = input("Enter your choice: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == "1":
            orders.manage_amenities_menu()
        elif choice == "2":
            orders.manage_promotions_menu()
        elif choice == "3":
            # Local import: admin.py imports onboarding for _admin_menu, so importing it at
            # module level here would be a cycle.
            from hotel import admin as admin_module
            admin_module.manage_discount_codes()
        elif choice == "4":
            return
        else:
            logging.info("Answer 1, 2, 3 or 4.")


def _setup_summary_rows():
    """The operator-facing review table shown before setup is marked complete."""
    def _fmt(value):
        return "unreachable" if value is None else str(value)

    room_types = rooms.get_room_types(active_only=False, include_status=True) or []
    active = [row for row in room_types if row[2]]
    priced = sum(1 for _rt, rate, _active in active if rate and rate > 0)
    return [
        ("Hotel name", core.get_hotel_name()),
        ("Tax rate", f"{core.get_tax_rate() * 100:.2f}%"),
        ("Peak / off-peak factors",
         f"{core.get_peak_factor():g} / {core.get_offpeak_factor():g}"),
        ("Loyalty programme",
         "on" if core.get_loyalty_enabled() else "off"),
        ("Loyalty earn rates",
         f"{core.get_loyalty_points_per_night()} pts/night, "
         f"{core.get_loyalty_accrual_points_per_unit():g} pts/$1 F&B"),
        ("Active categories priced", f"{priced} of {len(active)}"),
        ("Sellable items", _fmt(core._scalar_count("SELECT COUNT(*) FROM Items"))),
        ("Rooms", _fmt(core._scalar_count("SELECT COUNT(*) FROM Rooms"))),
        ("Conference rooms", _fmt(core._scalar_count("SELECT COUNT(*) FROM ConferenceRooms"))),
        ("Staff accounts", _fmt(core._scalar_count("SELECT COUNT(*) FROM Users"))),
        ("Amenities", _fmt(core._scalar_count("SELECT COUNT(*) FROM Amenities"))),
        ("Promotions", _fmt(core._scalar_count("SELECT COUNT(*) FROM Promotions"))),
        ("Discount codes", _fmt(core._scalar_count("SELECT COUNT(*) FROM Discounts"))),
    ]


def _wizard_step_menu():
    """Re-run one setup step after the review. Returns when the operator is done."""
    while True:
        ui.show_menu("Go back and change something", [
            "1. Hotel settings",
            "2. Nightly rates",
            "3. Staff accounts",
            "4. Item catalogue",
            "5. Rooms",
            "6. Reference data",
            "7. Conference rooms",
            "8. Back to the review",
        ])
        try:
            choice = input("Enter your choice: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == "1":
            _offer_hotel_settings(wizard=True)
        elif choice == "2":
            _offer_room_rates(wizard=True)
        elif choice == "3":
            _offer_staff_accounts(wizard=True)
        elif choice == "4":
            _offer_item_catalogue(wizard=True)
        elif choice == "5":
            _offer_room_layout(wizard=True)
        elif choice == "6":
            _offer_reference_data(wizard=True)
        elif choice == "7":
            _offer_conference_rooms(wizard=True)
        elif choice == "8":
            return
        else:
            logging.info("Answer 1 to 8.")


def run_first_run_onboarding():
    """The first-run wizard. Runs once, before the main menu, on a database not yet onboarded.

    It exists for the one step of docs/ONBOARDING.md that could not be done in-app at all:
    the first login. Everything after that is offered, because a complete setup needs
    settings, rates, accounts, items, rooms and optional reference data to take a payment
    without guessing. The trust assumption is the one the database already makes: whoever
    is at this console can also open SSMS against the empty database. It cannot run twice
    once `mark_onboarding_complete()` has been written, and `create_first_user()` refuses
    outright if any account already exists.
    """
    hotel = core.get_hotel_name()
    ui.box(
        f"Welcome to {hotel} - First-Time Setup",
        "This runs once, before the app is usable.\n\n"
        "It will create your first administrator login, then walk through hotel\n"
        "settings, nightly rates, staff accounts, the item catalogue, rooms and\n"
        "optional reference data. Every step after the login is skippable --\n"
        "Admin Panel → Setup & Destructive → Setup Checklist shows what is still\n"
        "outstanding at any time and can fix most of it.\n\n"
        "Before any of this, database.sql has to have been applied to the server,\n"
        "and the [database] section of config.ini has to point at it (the startup\n"
        "prompt creates that section when it is missing). See docs/ONBOARDING.md.",
        border_style="cyan",
    )

    created_admin_username = None
    # --- 1. The first login. The one step that is not optional. ---
    ui.info("\n-- Step 1: create your first administrator --")
    existing_accounts = core._scalar_count("SELECT COUNT(*) FROM Users")
    if existing_accounts:
        # Reached when someone hand-inserted accounts the old way and never wrote the
        # marker. Say so and move on rather than pretending to create a login we would only
        # refuse: create_first_user() will not add one over the top of an account that is
        # already there, so retrying would loop forever.
        ui.box(
            "Accounts already exist",
            f"This database already has {existing_accounts} account(s), so the wizard will "
            "not create a login over the top of one.\n\n"
            "That is fine -- it just means the first account was inserted by hand. Add "
            "everyone else from Admin Panel → Accounts → Add User.",
            border_style="yellow",
        )
    else:
        logging.info("Nothing can be logged into until an account exists, so this is the "
                     "only mandatory step.")
        attempts = 0
        while True:
            try:
                username = input("Administrator username [admin]: ").strip() or "admin"
            except (KeyboardInterrupt, EOFError):
                logging.info("Setup cancelled. The wizard will run again next time until "
                             "setup is complete or an account exists.")
                return
            try:
                password = core._prompt_new_password("Password: ")
            except (KeyboardInterrupt, EOFError):
                logging.info("Setup cancelled. The wizard will run again next time until "
                             "setup is complete or an account exists.")
                return
            if not password:
                attempts += 1
            elif create_first_user(username, password, "admin"):
                created_admin_username = username
                break
            else:
                attempts += 1
            if attempts >= 3:
                try:
                    retry = input("Failed to create the first administrator. "
                                  "Try again? (Y/n): ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    logging.info("Setup cancelled. The wizard will run again next time.")
                    return
                if retry in ("n", "no"):
                    logging.info("Setup cancelled. Create an administrator manually or re-run "
                                 "the wizard until the first account is created.")
                    return
                attempts = 0
            logging.info("Could not create the account. Check the error above and try again.")

    # --- 2. The master override account. ---
    ui.info("\n-- Step 2: master override account --")
    if core._scalar_count("SELECT COUNT(*) FROM Users WHERE Username = ?", ("master",)):
        logging.info("A 'master' account already exists. Leaving it alone.")
    elif ui.ask_confirmation(
            "Create a 'master' account for the master override? Without it the override "
            "refuses every caller.", default="y"):
        try:
            master_password = core._prompt_new_password("Master password: ")
        except (KeyboardInterrupt, EOFError):
            master_password = None
        if not master_password:
            logging.info("Skipped creating the 'master' account.")
        else:
            # Role 'admin', not 'master'. require_master_override() matches on the username
            # alone, but admin_panel() has no 'master' branch and add_user() only offers
            # admin/staff/manager -- so a 'master'-role row would be an account nobody can ever
            # sign in to.
            #
            # Which insert to use depends on step 1. create_first_user() refuses whenever ANY
            # account exists, which is right for the first login (nothing else may reach this
            # screen) and wrong here: an inherited database has accounts, no marker, and no
            # master -- so it would refuse and leave the override unbacked by the operator's own
            # choice, which is the one case this step exists for. Fall back to the same
            # already-authenticated path the checklist uses.
            if existing_accounts:
                created = core.add_user_with_password("master", master_password, "admin")
            else:
                created = create_first_user("master", master_password, "admin")
            if created:
                logging.info("Master override is now backed by a real account.")
            else:
                logging.info("Could not create the 'master' account. Create one from Admin "
                             "Panel → Accounts → Add User, or the override will refuse every caller.")
    else:
        logging.info("Skipped. The master override will refuse every caller until you "
                     "create a 'master' account (Admin Panel → Accounts → Add User).")

    # --- 2b. Hotel settings. ---
    ui.info("\n-- Step 2b: hotel settings --")
    _offer_hotel_settings(wizard=True)

    # --- 2c. Nightly rates. ---
    ui.info("\n-- Step 2c: nightly rates --")
    _offer_room_rates(wizard=True)

    # --- 2d. Staff accounts. ---
    ui.info("\n-- Step 2d: staff accounts --")
    _offer_staff_accounts(wizard=True)

    # --- 3. Starter catalogue. ---
    ui.info("\n-- Step 3: item catalogue --")
    existing_items = core._scalar_count("SELECT COUNT(*) FROM Items")
    if existing_items:
        # An inherited or hand-configured database. Offering to seed a catalogue into a
        # populated one would quietly add stock nobody asked for, so each optional step is
        # gated on its own live probe rather than on the marker.
        logging.info("The catalogue already has %d item(s). Skipping -- add your own from "
                     "Admin Panel → Items & Services → Add Item, or "
                     "Setup & Destructive → Setup Checklist.", existing_items)
    else:
        logging.info("Nothing can be ordered until at least one item exists, and every row in "
                     "Items is something a guest can actually buy. The room charge is not one "
                     "of them -- the app posts that itself at check-out, so do not look for it "
                     "here.")
        added = _offer_item_catalogue(wizard=True)
        if not added:
            logging.info("No items were added. Nothing can be ordered until there is at least "
                         "one -- Admin Panel → Items & Services → Add Item, or "
                         "Setup & Destructive → Setup Checklist → 3.")

    # --- 4. Rooms. ---
    ui.info("\n-- Step 4: rooms --")
    existing_rooms = core._scalar_count("SELECT COUNT(*) FROM Rooms")
    if existing_rooms:
        logging.info("Rooms already exist (%d). Skipping the layout seed -- top it up from "
                     "Setup & Destructive → Setup Checklist → 4 if you need more.", existing_rooms)
    else:
        logging.info("A room number is <floor><3-digit code>, so floor 9 code 012 is 9012. "
                     "Skipping this is fine: the first booking registers its own room.")
        _offer_room_layout(wizard=True)

    # --- 4b. Conference rooms (optional). ---
    ui.info("\n-- Step 4b: conference rooms (optional) --")
    _offer_conference_rooms(wizard=True)

    # --- 5. Optional reference data. ---
    ui.info("\n-- Step 5: reference data (optional) --")
    _offer_reference_data(wizard=True)

    # Setup review with explicit confirmation. Only mark complete after the operator
    # signs off; if they want to change something, re-run a step and review again.
    while True:
        outstanding = [label for label, done, _ in setup_status() if not done]
        ui.show_table("Setup review", ["Setting", "Value"], _setup_summary_rows())
        if outstanding:
            ui.box(
                "Still outstanding",
                "Some items are not ready for the front desk.\n\n"
                + "".join("  - %s\n" % label for label in outstanding)
                + "\nYou can go back and change or add the things above before finishing.",
                border_style="yellow",
            )
        ui.show_menu("Finish setup", [
            "1. Mark setup complete and go to the main menu",
            "2. Go back and change something",
            "3. Cancel setup (run wizard again next time)",
        ])
        try:
            choice = input("Enter your choice: ").strip()
        except (KeyboardInterrupt, EOFError):
            logging.info("Setup cancelled. The wizard will run again next time until "
                         "setup is complete.")
            return
        if choice == "1":
            try:
                confirm = ui.ask_confirmation(
                    "Mark first-run setup complete? The wizard will not run again.",
                    default="y")
            except (KeyboardInterrupt, EOFError):
                logging.info("Setup cancelled. The wizard will run again next time.")
                return
            if not confirm:
                continue
            break
        elif choice == "2":
            _wizard_step_menu()
            continue
        elif choice == "3":
            logging.info("Setup cancelled. The wizard will run again next time until "
                         "an account exists or setup is marked complete.")
            return
        else:
            logging.info("Answer 1, 2 or 3.")

    mark_onboarding_complete()
    core.log_audit("CREATE", "Setting", ONBOARDING_SETTING, "First-run onboarding completed")

    outstanding = [label for label, done, _ in setup_status() if not done]
    ui.box(
        "Setup complete",
        ((f"Administrator '{created_admin_username}' can now sign in.\n\n"
          if created_admin_username
          else (f"The existing {existing_accounts} account(s) can now sign in.\n\n"
                if existing_accounts else ""))
         + ("Everything on the readiness checklist is green.\n"
            if not outstanding
            else "Still outstanding (Admin Panel → Setup & Destructive → Setup Checklist):\n"
                 + "".join("  - %s\n" % label for label in outstanding))
         + "\nSmoke test, in this order:\n"
           "  1. Bookings → book a stay, two nights, in a room number of your choosing\n"
           "  2. Admin Panel → Rooms & Housekeeping → Explore Floor -- the room should be Available\n"
           "  3. Order something through the guest menu\n"
           "  4. Check the guest out -- the folio must split into Room Charges and F&B\n"
           "  5. Admin Panel → Invoices & Reports → Invoices & Printing -- print it and check the arithmetic"),
        border_style="green",
    )
    ui.pause("Press Enter to reach the main menu...")


def _count_tower_rooms():
    """How many rooms the tower layout holds, computed in Python from the same rules.

    Not a database call: the wizard needs the figure to describe the option BEFORE
    anything is connected, and it is pure arithmetic over the tiers in 008.
    100 floors x 999 codes, 45 floors x 850 codes, 5 floors x 6 codes.
    """
    return 100 * rooms.MAX_ROOMS_PER_FLOOR + 45 * 850 + 5 * 6


def onboarding_checklist(role="admin"):
    """Setup readiness for an administrator, plus the setup actions that are still open.

    Lives in the Admin Panel rather than the main menu, because minting a login is a
    privileged act and the main menu is the guest-facing surface.

    Read-only unless the signed-in role is `admin`, so the screen is safe to show a manager
    browsing the panel without letting the setup paths become a way around the Admin Panel's
    own role checks. Actions are offered one at a time, each confirmed before it runs.
    """
    may_write = str(role or "").lower() == "admin"
    while True:
        checks = setup_status()
        ui.show_table(
            "Setup Checklist",
            ["Ready", "Check", "Detail"],
            [("[green]yes[/green]" if done else "[yellow]no[/yellow]", label, detail)
             for label, done, detail in checks],
        )
        if all(done for _, done, _ in checks):
            logging.info("Everything this hotel needs before the front desk can take a "
                         "payment is in place.")
        if not may_write:
            ui.info("\nSetup actions need the admin role. This view is read-only.")
            return

        ui.pause()
        ui.show_menu("Setup actions", [
            "1. Add a staff account",
            "2. Create the 'master' override account",
            "3. Add items (starter catalogue, or your own)",
            "4. Seed rooms",
            "5. Set nightly rates",
            "6. Edit hotel settings (tax, loyalty, lockout)",
            "7. Manage loyalty tiers",
            "8. Manage amenity, promotion and discount reference data",
            "9. Configure conference rooms",
            "10. Re-check",
            "11. Back to Admin Panel",
        ])
        try:
            choice = input("Enter your choice: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == '1':
            if _create_staff_account():
                logging.info("Account created. It can sign in immediately.")
        elif choice == '2':
            if core.user_exists("master"):
                logging.info("A 'master' account already exists. Leaving it alone.")
            else:
                master_password = core._prompt_new_password("Master password: ")
                # Role 'admin' for the same reason as in the wizard: the override matches on
                # the username, and no login path accepts a 'master' role.
                core.add_user_with_password("master", master_password, "admin")
        elif choice == '3':
            added = _offer_item_catalogue()
            if added:
                logging.info("Added %d item(s). Admin Panel → Items & Services → "
                             "View Items to check them.", added)
            else:
                logging.info("Nothing added -- either you finished without adding anything, "
                             "the names already exist, or the write failed. Check the log.")
        elif choice == '4':
            _offer_room_layout()
        elif choice == '5':
            _offer_room_rates()
        elif choice == '6':
            _offer_hotel_settings()
        elif choice == '7':
            if core.get_loyalty_enabled():
                loyalty.loyalty_admin_menu()
            else:
                logging.info("The loyalty programme is off. Turn it on under "
                             "Setup actions → 6 (hotel settings), then manage tiers here.")
        elif choice == '8':
            _offer_reference_data()
        elif choice == '9':
            _offer_conference_rooms()
        elif choice == '10':
            continue
        elif choice == '11':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def _offer_item_catalogue(wizard=False):
    """Shared by the wizard and the checklist: build up a catalogue from either source.

    Offers the starter set AND lets the operator type their own, because the wizard cannot
    know what a given hotel actually sells and a starter catalogue nobody sells is not much
    use. Adding custom items is a loop rather than a one-shot because hotels almost always
    have a handful and not everyone has them written down in advance.

    `wizard` only changes what the exit option is called, exactly as in _offer_room_layout.
    Returns how many items were added, so the caller can report honestly.
    """
    added = 0
    ui.show_table("Starter catalogue", ["Item", "Price", "Pricing rule"],
                  [[name, f"${price:,.2f}", rule or "standard"]
                   for _id, name, price, rule in items.DEFAULT_SEED_ITEMS])
    logging.info("These %d items are a starting point, not a menu. Anything can be edited, "
                 "added or deleted afterwards from Admin Panel → Items & Services.",
                 len(items.DEFAULT_SEED_ITEMS))
    try:
        while True:
            ui.pause()
            ui.show_menu("Add items", [
                "1. Add the starter catalogue (%d items)" % len(items.DEFAULT_SEED_ITEMS),
                "2. Add an item of your own",
                "3. %s" % ("Skip for now" if wizard else "Cancel -- done adding items"),
            ])
            choice = input("Enter your choice: ").strip()
            if choice == "1":
                added += items.seed_default_items()
            elif choice == "2":
                try:
                    if items.add_custom_item():
                        added += 1
                except KeyboardInterrupt:
                    logging.info("Cancelled that item. The rest of your catalogue is fine.")
            elif choice == "3":
                break
            else:
                logging.info("Answer 1, 2 or %s.", 3)
    except (KeyboardInterrupt, EOFError):
        logging.info("Finished adding items.")
    return added


def _offer_room_layout(wizard=False):
    """Shared by the wizard and the checklist: pick a layout, count it, confirm, seed it.

    `wizard` only changes what the fourth option is called -- "skip" during first-run,
    because not having rooms yet is a legitimate state to carry on from, versus "cancel"
    when someone has come here deliberately to add more.
    """
    tower_rooms = _count_tower_rooms()
    ui.show_menu("Room layout", [
        "1. 150-floor tower (%s rooms) -- the layout in migrations/008_rooms_seed.sql" % f"{tower_rooms:,}",
        "2. Mid-size: 20 floors x 40 rooms (800 rooms)",
        "3. Custom floor count and rooms per floor",
        "4. %s" % ("Skip -- rooms appear as bookings are made" if wizard else "Cancel"),
    ])
    choice = input("Enter your choice: ").strip()
    if choice == "1":
        if not ui.ask_confirmation(f"Seed {tower_rooms:,} rooms? This is a large insert.",
                                   default="n"):
            return
        pending = rooms.count_rooms_to_add(True)
        if pending is None:
            logging.info("Could not count the rooms that would be added. Nothing was changed.")
            return
        if pending == 0:
            logging.info("Every room in the tower layout already exists.")
            return
        rooms.seed_rooms(tower=True)
    elif choice == "2":
        rooms.seed_rooms(floors=20, rooms_per_floor=40)
    elif choice == "3":
        while True:
            floors = ui.ask_number(f"Floors (1-{rooms.MAX_ROOM_FLOORS})", minimum=1, maximum=rooms.MAX_ROOM_FLOORS)
            per_floor = ui.ask_number(f"Rooms per floor (1-{rooms.MAX_ROOMS_PER_FLOOR})", minimum=1,
                                      maximum=rooms.MAX_ROOMS_PER_FLOOR)
            pending = rooms.count_rooms_to_add(False, floors, per_floor)
            if pending is None:
                logging.info("Could not count the rooms that layout would add. Nothing was "
                             "changed.")
                return
            if pending == 0:
                logging.info("Those rooms all exist already.")
                return
            if ui.ask_confirmation(f"Seed {pending:,} room(s)?", default="y"):
                rooms.seed_rooms(floors=floors, rooms_per_floor=per_floor)
                return
            logging.info("Nothing was seeded. Answer 4 to %s, or try again."
                         % ("skip" if wizard else "cancel"))
    else:
        logging.info("Skipped. The first booking will register its own room." if wizard
                     else "Cancelled. No rooms were changed.")
