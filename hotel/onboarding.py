# type: ignore
"""onboarding: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from . import db
from . import ui
from . import core
from . import items
from . import loyalty
from . import rooms

__all__ = [
    'ONBOARDING_SETTING',
    'onboarding_completed',
    'mark_onboarding_complete',
    'create_first_user',
    'setup_status',
    '_offer_hotel_settings',
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

    rated = core._scalar_count("SELECT COUNT(*) FROM RoomTypes WHERE ISNULL(NightlyRate, 0) > 0")
    if rated is None:
        checks.append(("Nightly rates", False, "RoomTypes is unreachable -- migration 013 applied?"))
    elif rated == 0:
        checks.append(("Nightly rates", False,
                       "no room type has a rate -- check-out cannot price a stay"))
    else:
        checks.append(("Nightly rates", True, f"{rated} priced category(s)"))

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
    for key in core.DEFAULT_HOTEL_SETTINGS:
        raw = all_rows.get(key, None)
        if raw is None or str(raw).strip() == '':
            bad.append(f"{key} missing")
        elif key not in ('hotel_name', 'loyalty_enabled'):
            try:
                float(str(raw))
            except (TypeError, ValueError):
                bad.append(f"{key} not a number: {raw!r}")
        elif key == 'loyalty_enabled':
            if str(raw).strip().lower() not in ('1', '0', 'true', 'false', 'yes', 'no', 'y', 'n', 'on', 'off'):
                bad.append(f"loyalty_enabled not a boolean: {raw!r}")

    for key, raw in all_rows.items():
        if key in core.DEFAULT_HOTEL_SETTINGS or key == 'business_date':
            continue
        if raw is None or str(raw).strip() == '':
            continue
        try:
            float(str(raw))
        except (TypeError, ValueError):
            bad.append(f"{key} not a number: {raw!r}")

    if bad:
        checks.append(("Hotel settings", False, "; ".join(bad)))
    else:
        checks.append(("Hotel settings", True, f"all {len(all_rows)} row(s) present and parseable"))

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
                 "changed later from Admin Panel -> 25. Pricing & Settings.")

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


def run_first_run_onboarding():
    """The first-run wizard. Runs once, before the main menu, on a database not yet onboarded.

    It exists for the one step of docs/ONBOARDING.md that could not be done in-app at all:
    the first login. Everything after that is offered but skippable, because the wizard
    cannot know what a given hotel sells, how many rooms it has, or what its floors are laid
    out like, and a setup step that guesses wrong is worse than one that asks.

    Unauthenticated by necessity rather than by choice -- there is no account to
    authenticate against yet. The trust assumption is the one the database already makes:
    whoever is at this console can also open SSMS against the empty database. It cannot run
    twice once `mark_onboarding_complete()` has been written, and `create_first_user()`
    refuses outright if any account already exists, so it cannot become a way to add an
    admin to a live system.
    """
    hotel = core.get_hotel_name()
    ui.box(
        f"Welcome to {hotel} - First-Time Setup",
        "This runs once, before the app is usable.\n\n"
        "It will create your first administrator login, then offer a few starting\n"
        "defaults. Every step after the login is optional and can be skipped --\n"
        "Admin Panel -> 34. Setup Checklist shows what is still outstanding at any time.\n\n"
        "Before any of this, database.sql has to have been applied to the server,\n"
        "and the [database] section of config.ini has to point at it (the startup\n"
        "prompt creates that section when it is missing). See docs/ONBOARDING.md.",
        border_style="cyan",
    )

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
            "everyone else from Admin Panel -> 19. Add User.",
            border_style="yellow",
        )
    else:
        logging.info("Nothing can be logged into until an account exists, so this is the "
                     "only mandatory step.")
        while True:
            username = input("Administrator username [admin]: ").strip() or "admin"
            password = core._prompt_new_password("Password: ")
            if create_first_user(username, password, "admin"):
                break
            logging.info("Could not create the account. Check the error above and try again.")

    # --- 2. The master override account. ---
    ui.info("\n-- Step 2: master override account --")
    if core._scalar_count("SELECT COUNT(*) FROM Users WHERE Username = ?", ("master",)):
        logging.info("A 'master' account already exists. Leaving it alone.")
    elif ui.ask_confirmation(
            "Create a 'master' account for the master override? Without it the override "
            "refuses every caller.", default="y"):
        master_password = core._prompt_new_password("Master password: ")
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
                         "Panel -> Add User, or the override will refuse every caller.")
    else:
        logging.info("Skipped. The master override will refuse every caller until you "
                     "create a 'master' account (Admin Panel -> Add User).")

    # --- 2b. Hotel settings. ---
    ui.info("\n-- Step 2b: hotel settings --")
    _offer_hotel_settings(wizard=True)

    # --- 3. Starter catalogue. ---
    ui.info("\n-- Step 3: item catalogue --")
    existing_items = core._scalar_count("SELECT COUNT(*) FROM Items")
    if existing_items:
        # An inherited or hand-configured database. Offering to seed a catalogue into a
        # populated one would quietly add stock nobody asked for, so each optional step is
        # gated on its own live probe rather than on the marker.
        logging.info("The catalogue already has %d item(s). Skipping -- add your own from Setup "
                     "Checklist -> 3, or Admin Panel -> 13.", existing_items)
    else:
        logging.info("Nothing can be ordered until at least one item exists, and every row in "
                     "Items is something a guest can actually buy. The room charge is not one "
                     "of them -- the app posts that itself at check-out, so do not look for it "
                     "here.")
        added = _offer_item_catalogue(wizard=True)
        if not added:
            logging.info("No items were added. Nothing can be ordered until there is at least "
                         "one -- Admin Panel -> 13. Add Item, or Setup Checklist -> 3.")

    # --- 4. Rooms. ---
    ui.info("\n-- Step 4: rooms --")
    existing_rooms = core._scalar_count("SELECT COUNT(*) FROM Rooms")
    if existing_rooms:
        logging.info("Rooms already exist (%d). Skipping the layout seed -- top it up from "
                     "Setup Checklist -> 4 if you need more.", existing_rooms)
    else:
        logging.info("A room number is <floor><3-digit code>, so floor 9 code 012 is 9012. "
                     "Skipping this is fine: the first booking registers its own room.")
        _offer_room_layout(wizard=True)

    mark_onboarding_complete()
    core.log_audit("CREATE", "Setting", ONBOARDING_SETTING, "First-run onboarding completed")

    outstanding = [label for label, done, _ in setup_status() if not done]
    ui.box(
        "Setup complete",
        (f"Administrator '{username}' can now sign in.\n\n" if not existing_accounts
         else f"The existing {existing_accounts} account(s) can now sign in.\n\n")
        + ("Everything on the readiness checklist is green.\n"
           if not outstanding
           else "Still outstanding (Admin Panel -> 34. Setup Checklist):\n"
                + "".join("  - %s\n" % label for label in outstanding))
        + "\nSmoke test, in this order:\n"
          "  1. Bookings -> book a stay, two nights, in a room number of your choosing\n"
          "  2. Admin Panel -> 29. Rooms & Housekeeping -> 2. Explore Floor -- the room should be Available\n"
          "  3. Order something through the guest menu\n"
          "  4. Check the guest out -- the folio must split into Room Charges and F&B\n"
          "  5. Admin Panel -> 30. Invoices & Printing -- print it and check the arithmetic",
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
            "5. Re-check",
            "6. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            username = input("Enter new username: ").strip()
            if not username:
                logging.info("A username is required.")
            else:
                password = core._prompt_new_password("Password: ")
                role_prompt = ("Role (a)dmin/(s)taff/(m)anager: ")
                new_role = core._prompt_role(role_prompt)
                if not new_role:
                    logging.info("No account was created.")
                elif core.user_exists(username):
                    logging.info(f"User '{username}' already exists.")
                else:
                    # Not create_first_user(): that refuses once any account exists, and by
                    # the time anyone reaches the checklist there is always at least one.
                    core.add_user_with_password(username, password, new_role)
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
                logging.info("Added %d item(s). Admin Panel -> 16. View Items to check them.",
                             added)
            else:
                logging.info("Nothing added -- either you finished without adding anything, "
                             "the names already exist, or the write failed. Check the log.")
        elif choice == '4':
            _offer_room_layout()
        elif choice == '5':
            continue
        elif choice == '6':
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
                 "added or deleted afterwards from Admin Panel -> 13/14/15.", len(items.DEFAULT_SEED_ITEMS))
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
