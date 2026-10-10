"""Clearance cards: map guests/staff to the card assets in `Clearance cards/`.

Card display names are extracted from the SVG files themselves (the third
<text> block, e.g. "DIRECTORY OVERSEER" in Omega.svg), so adding a card means
dropping a new SVG in the folder — no code change.

Mapping:
- Staff roles       -> Sigma / Psi / Omega by Users.Role
- Guests            -> tier x room-category matrix (tiers and categories follow
                       the names already used by LoyaltyTiers / Rooms.RoomType)
- No loyalty account -> Visitor
- emergency=True    -> Mayday (explicit override, never auto-derived)

No schema change: the card is always derived from data that already exists.
"""

import os
import re
import shutil
import logging

from .db import get_connection

# Both the card assets and the report exports live at the repository root, one level up
# from this package, so resolve them as ../<name> rather than next to this module.
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Clearance cards")
EXPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")

CATEGORIES = [
    "Standard", "Deluxe", "Junior Suite", "Suite",
    "Grand Suite", "Penthouse", "Presidential Suite",
]

# Rows keyed by tier, one card per category in CATEGORIES order.
GUEST_MATRIX = {
    "Bronze":   ["Alpha", "Beta", "Beta2", "Gamma", "Gamma2", "Delta", "Epsilon"],
    "Silver":   ["Zeta", "Eta", "Theta", "Iota", "Kappa", "Lambda", "Mu"],
    "Gold":     ["Nu", "Xi", "Ommicron", "Pi", "Rho", "Tau", "Upsilon"],
    # Deluxe and up are all Chi; Standard stays Phi.
    "Platinum": ["Phi"] + ["Chi"] * (len(CATEGORIES) - 1),
}

ROLE_CARDS = {
    "staff": "Sigma",
    "manager": "Psi",
    "admin": "Omega",
    "valet": "Tau",
    "it": "Theta",
}

VISITOR = "Visitor"
MAYDAY = "Mayday"


def _text_blocks(svg_text):
    """All <text> blocks, each flattened to its concatenated tspan content."""
    blocks = []
    for block in re.findall(r"<text\b.*?</text>", svg_text, re.S):
        parts = re.findall(r"<tspan[^>]*>(.*?)</tspan>", block, re.S)
        blocks.append("".join(parts).strip())
    return blocks


def extract_card_name(svg_path):
    """The role/title line of the card (e.g. 'DIRECTORY OVERSEER')."""
    with open(svg_path, "r", encoding="utf-8") as f:
        blocks = _text_blocks(f.read())
    # blocks: [header, badge, name, letter-badge...]
    return blocks[2] if len(blocks) >= 3 else ""


def extract_badge(svg_path):
    """The 'X CLEARANCE' / 'VISITOR PASS' line."""
    with open(svg_path, "r", encoding="utf-8") as f:
        blocks = _text_blocks(f.read())
    return blocks[1] if len(blocks) >= 2 else ""


def card_path(card_key):
    return os.path.join(ASSETS_DIR, f"{card_key}.svg")


def card_png_path(card_key):
    # Optional rasterized copy produced by tools/convert_clearance_svgs.py.
    return os.path.join(ASSETS_DIR, f"{card_key}.png")


def get_card(card_key):
    """Descriptor for a card key (SVG filename stem, e.g. 'Omega')."""
    try:
        name = extract_card_name(card_path(card_key))
        badge = extract_badge(card_path(card_key))
    except (OSError, UnicodeDecodeError):
        name, badge = "", ""
    return {
        "key": card_key,
        "badge": badge,
        "name": name,
        "svg": card_path(card_key),
        "png": card_png_path(card_key),
    }


def guest_card_key(tier, room_type):
    """Card key for a guest from their loyalty tier and room category."""
    row = GUEST_MATRIX.get(str(tier or "").strip())
    if row is None:
        row = GUEST_MATRIX["Bronze"]  # unknown tier falls back to the base row
    try:
        idx = CATEGORIES.index(str(room_type or "").strip())
    except ValueError:
        idx = 0
    return row[idx]


def clearance_for_guest(tier, room_type, has_loyalty_account=True, emergency=False):
    if emergency:
        return MAYDAY
    if not has_loyalty_account or tier is None and room_type is None:
        return VISITOR
    return guest_card_key(tier, room_type)


def clearance_for_role(role):
    return ROLE_CARDS.get(str(role or "").strip().lower(), VISITOR)


def _role_for_username(cursor, username):
    cursor.execute("SELECT Role FROM Users WHERE Username = ?", (username,))
    row = cursor.fetchone()
    return row[0] if row else None


def _guest_for_room(cursor, room_number):
    """The room's current occupant.

    Returns `{card, room_type, tier, name}`, or None when the room is not in
    `Rooms`. `name` is the guest on the live reservation (the person at the door),
    so the desk can identify the *customer* rather than repeat the room number.

    The tier is read for the stay's customer when the stay is linked
    (`Reservations.CustomerID`), so the name and tier on the display describe the
    same person. An unlinked front-desk stay with no `CustomerID` falls back to the
    account's last-room pointer -- `LoyaltyAccounts.RoomNumber` -- which is how this
    resolved before 019 re-keyed loyalty to the customer.
    """
    cursor.execute("SELECT RoomType FROM Rooms WHERE RoomNumber = ?", (room_number,))
    r = cursor.fetchone()
    if not r:
        return None
    room_type = r[0]
    cursor.execute(
        "SELECT LastName, FirstName, CustomerID FROM Reservations WHERE RoomNumber = ?",
        (room_number,),
    )
    stay = cursor.fetchone()
    last, first, customer_id = stay if stay else (None, None, None)
    name = " ".join(part for part in ((first or "").strip(),
                                      (last or "").strip()) if part) or None
    if customer_id:
        cursor.execute("SELECT Tier FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
    else:
        cursor.execute(
            "SELECT TOP 1 Tier FROM LoyaltyAccounts WHERE RoomNumber = ?", (room_number,)
        )
    row = cursor.fetchone()
    tier = row[0] if row and row[0] else None
    return {
        "card": VISITOR if tier is None else guest_card_key(tier, room_type),
        "room_type": room_type,
        "tier": tier,
        "name": name,
    }


def guest_subject(guest, room_number=None):
    """The line the clearance desk shows for a resolved room.

    It names the **customer** and their **loyalty status**; it deliberately does not
    repeat the room number, which the operator already typed (and which identifies a
    room, not a person). A room with no live guest reads as such -- there is no
    customer to name.
    """
    name = (guest or {}).get("name")
    if not name:
        return "No guest checked in"
    tier = (guest or {}).get("tier")
    loyalty = f"{tier} tier" if tier else "no loyalty account"
    return f"Customer: {name}    Loyalty: {loyalty}"


def lookup(scanned):
    """Resolve a scanned card number (staff username or room number) to a card.

    Returns (card_key, subject_label) or (None, reason) when nothing matches.
    """
    scanned = str(scanned or "").strip()
    if not scanned:
        return None, "empty scan"
    try:
        with get_connection() as conn:
            if conn is None:
                return None, "database unavailable"
            cursor = conn.cursor()
            role = _role_for_username(cursor, scanned)
            if role:
                return clearance_for_role(role), f"{scanned} ({role})"
            guest = _guest_for_room(cursor, scanned)
            if guest:
                return guest["card"], guest_subject(guest, scanned)
            return None, f"no user or room matching '{scanned}'"
    except Exception as exc:
        logging.error(f"clearance lookup failed: {exc}")
        return None, str(exc)


def export_card(card_key, dest_dir=None):
    """Copy the card SVG into the exports directory; returns the new path."""
    dest_dir = dest_dir or EXPORTS_DIR
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"clearance_{card_key}.svg")
    shutil.copyfile(card_path(card_key), dest)
    return dest
