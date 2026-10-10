# type: ignore
"""FastAPI web/API edition of the hotel management app (see PLAN-web-api.md).

This is a *second* entry point, not a replacement. `python main.py` keeps working
unchanged and remains the fallback; `uvicorn api:app` serves this. Neither front-end
owns a business rule: both call the same non-interactive service functions in the
domain modules, so the two cannot drift (AGENTS.md section 5).

Phases 1-3 built the scaffold, the service twins and the cookie-session layer;
phase 4 adds the pilot endpoints; phase 5 adds the report endpoints, the read
surfaces (room list, staff board, guest's own bookings/loyalty), the billing reads
and the order reads/writes. Every handler
touches the database only through a service function and is a plain `def`, so
Starlette runs the blocking pyodbc call in its threadpool; never make such a handler
`async def`, because a synchronous database call inside the event loop blocks every
other request.
"""
import logging
import os
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware

# Importing core also reads config.ini and calls db.init(...), which wires the database
# connection the service functions will use. Unlike `python main.py`, this never triggers
# core._ensure_database_config()'s interactive setup prompt: an API server must not block on
# stdin. A missing config.ini simply leaves connections returning None, which every
# get_connection() caller already handles.
import core  # noqa: F401

import admin
import auth
import billing
import booking_ledger
import bookings
import customer
import items
import loyalty
import orders
import payments
import reports
import reservations
import rooms
from schemas import (
    AdvanceOrderRequest, CheckInRequest, CreateBookingRequest, CreateOrderRequest, LoginRequest,
)

# Signed-cookie sessions (PLAN-web-api.md, decision 2). The secret is read from the
# environment so it is not committed; the fallback is for local development only and is
# deliberately loud. Set HOTEL_SESSION_SECRET before exposing the API beyond localhost.
SESSION_SECRET_ENV = "HOTEL_SESSION_SECRET"
_DEV_SESSION_SECRET = "dev-only-insecure-session-secret"


def session_secret():
    """Return the cookie-signing secret, warning when it fell back to the dev value."""
    secret = os.environ.get(SESSION_SECRET_ENV)
    if secret:
        return secret
    logging.warning(
        "%s is not set; using the development session secret. Set it before exposing "
        "the API beyond localhost (PLAN-web-api.md decisions 2-3).",
        SESSION_SECRET_ENV,
    )
    return _DEV_SESSION_SECRET


router = APIRouter(prefix="/api")


def require_principal(request: Request) -> dict:
    """FastAPI dependency: the signed-in principal, or HTTP 401.

    Wraps auth.current_principal(), which is deliberately FastAPI-free (phase 3): the
    endpoint layer owns the Request annotation and the exception mapping. A malformed
    cookie is cleared by auth and treated as signed out.
    """
    try:
        return auth.current_principal(request)
    except auth.NotAuthenticated:
        raise HTTPException(status_code=401, detail="Not signed in.")


def require_staff(request: Request) -> dict:
    """A staff principal; console-only actions (check-in) are 403 for guest sessions."""
    principal = require_principal(request)
    if principal["kind"] != "staff":
        raise HTTPException(status_code=403, detail="This action requires a staff account.")
    return principal


def require_guest(request: Request) -> dict:
    """A guest principal; their own data (bookings, loyalty) is 403 for staff sessions."""
    principal = require_principal(request)
    if principal["kind"] != "guest":
        raise HTTPException(status_code=403, detail="This endpoint is for signed-in guest accounts.")
    return principal


def require_management(request: Request) -> dict:
    """A manager-or-admin staff principal.

    Matches the console Admin Panel's capability sets: the account list and the item and
    discount catalogues are the manager tier up, exactly as tests/test_admin_menu.py pins
    them -- a plain staff account cannot reach these.
    """
    principal = require_staff(request)
    if principal["role"] not in ("manager", "admin"):
        raise HTTPException(status_code=403,
                            detail="This action requires a manager or admin account.")
    return principal


def require_admin(request: Request) -> dict:
    """An admin-only staff principal (account management, promotions)."""
    principal = require_staff(request)
    if principal["role"] != "admin":
        raise HTTPException(status_code=403, detail="This action requires an admin account.")
    return principal


def _unavailable():
    return HTTPException(status_code=503, detail="The database is unreachable.")


def _window_detail(gate):
    """The 409 body for a stay that is not inside its check-in window yet."""
    detail = {"message": "This stay is not inside its check-in window yet."}
    for key in ("check_in", "check_out", "today"):
        if gate.get(key) is not None:
            detail[key] = str(gate[key])
    return detail


@router.get("/health")
def health():
    """Liveness probe. Deliberately does not open a database connection."""
    return {"status": "ok"}


@router.post("/auth/login")
def login(request: Request, body: LoginRequest):
    """Sign a staff member or a booking-desk guest in, issuing the session cookie.

    On success the principal goes into the signed cookie (auth.issue_principal):
    sessions are signed, not stored server-side, so there is no session table and no
    migration. Lockout counting and the LOGIN/LOGIN_FAILED/LOCKOUT audit rows belong
    to the services; this endpoint only maps their status dicts to HTTP responses.
    """
    if body.kind == "staff":
        result = admin.verify_staff_login(body.username, body.password)
        status = result["status"]
        if status == "ok":
            auth.issue_principal(request, auth.staff_principal(body.username, result["role"]))
            return {"kind": "staff", "username": body.username, "role": result["role"]}
        if status == "unavailable":
            raise _unavailable()
        if status == "not_found":
            raise HTTPException(401, "Username not found.")
        if status == "locked":
            raise HTTPException(403, f"Account is locked until {result['lockout_time']}.")
        if status == "lockout":
            raise HTTPException(403, {
                "message": "Account locked after too many failed attempts.",
                "lockout_time": result["lockout_time"].isoformat(),
            })
        raise HTTPException(401, {
            "message": "Invalid password.",
            "attempts": result["attempts"],
        })

    result = customer.authenticate_customer(body.email, body.password)
    status = result["status"]
    if status == "ok":
        auth.issue_principal(request, auth.guest_principal(result["customer_id"], body.email))
        return {"kind": "guest", "customer_id": result["customer_id"],
                "email": body.email, "first_name": result["first_name"]}
    if status == "unavailable":
        raise _unavailable()
    if status == "unknown":
        raise HTTPException(401, "No account found with that email address.")
    if status == "no_password":
        raise HTTPException(403, "This account has no password set; the front desk can help.")
    raise HTTPException(401, "Invalid password.")


@router.post("/auth/logout")
def logout(request: Request):
    """Drop the signed-in principal; idempotent, so a second logout is a no-op."""
    auth.clear_principal(request)
    return {"ok": True}


@router.get("/me")
def me(request: Request, principal: dict = Depends(require_principal)):
    """The signed-in principal: kind + username/email + role/customer_id."""
    return principal


@router.get("/rooms/availability")
def availability(check_in: date, check_out: date, room_type: Optional[str] = None,
                 floor: Optional[int] = None, limit: int = 50):
    """Rooms free for the whole window, priced by the same rule search_availability() uses."""
    if check_out <= check_in:
        raise HTTPException(400, "check_out must be later than check_in.")
    found = reservations.search_availability(
        check_in, check_out, room_type=room_type, floor=floor, limit=limit)
    return {"rooms": [
        {"room_number": room, "room_type": rtype, "status": status}
        for room, rtype, status in found
    ]}


@router.get("/rooms")
def rooms_list():
    """Room categories with their nightly rates -- what the booking desk shows a guest before sign-in.

    Like the availability endpoint this is deliberately open. The rates come from the
    same get_room_types() the wizard quotes from, including its graceful fallback to
    DEFAULT_ROOM_TYPE_RATES when the RoomTypes table is missing, so a disabled database
    reads exactly as it does on the console rather than as a fabricated price.
    """
    return {"rooms": [
        {"room_type": room_type, "nightly_rate": nightly_rate}
        for room_type, nightly_rate in rooms.get_room_types()
    ]}


@router.get("/reservations/board")
def reservations_board(on_date: Optional[date] = None,
                       principal: dict = Depends(require_staff)):
    """The staff arrivals / in-house / departures board for one day.

    Staff-only because it names every arriving, in-house and departing guest
    (docs/BOOKING.md privacy rules). `on_date` defaults to the business date; a day
    with no activity arrives as three empty lists, matching the console board.
    """
    board_date = on_date or core.business_date()
    arrivals, in_house, departures = reservations.arrivals_departures_board(board_date)

    def _rows(entries):
        return [{"room_number": entry[0], "last_name": entry[1], "first_name": entry[2],
                 "check_in": str(entry[3]), "check_out": str(entry[4])} for entry in entries]

    return {"date": str(board_date), "arrivals": _rows(arrivals),
            "in_house": _rows(in_house), "departures": _rows(departures)}


@router.get("/guests/me/bookings")
def my_bookings(request: Request, principal: dict = Depends(require_guest)):
    """The signed-in guest's own stays, newest first.

    Privacy: the web identity is the cookie's verified CustomerID, never a name, so a
    same-named stranger's stays cannot leak -- bookings.customer_stays() is keyed on
    that id alone (docs/BOOKING.md privacy rules).
    """
    stays = bookings.customer_stays(principal["customer_id"])
    return {"customer_id": principal["customer_id"], "email": principal["email"],
            "stays": [{"room_number": stay[0], "check_in": str(stay[1]),
                       "check_out": str(stay[2]),
                       "nights": core.stay_nights(stay[1], stay[2])} for stay in stays]}


@router.get("/guests/me/loyalty")
def my_loyalty(request: Request, principal: dict = Depends(require_guest)):
    """The signed-in guest's loyalty balance and tier, read-only.

    Unlike the console flow, this GET never creates a missing loyalty account -- a read
    must not write. When loyalty is disabled or the guest has no account yet, `eligible`
    is false and the tier fields are null (the console prints the same pair of states).
    """
    customer_id = principal["customer_id"]
    points = loyalty.get_points_by_customer(customer_id)
    details = loyalty.get_tier_details_by_customer(customer_id)
    name = principal["email"]
    if details is None:
        return {"customer_id": customer_id, "email": name, "eligible": False,
                "points": points, "tier": None, "lifetime_points": None,
                "points_multiplier": None, "discount_percent": None, "perks": None}
    return {"customer_id": customer_id, "email": name, "eligible": True,
            "points": points, "lifetime_points": details["lifetime_points"],
            "tier": details["tier"], "points_multiplier": details["points_multiplier"],
            "discount_percent": details["discount_percent"], "perks": details["perks"]}


@router.get("/rooms/{room}/invoices")
def room_invoices(room: str, principal: dict = Depends(require_staff)):
    """The stored invoices for a room, newest first.

    Staff-only: an invoice is a snapshot of a guest's money, and the room does not
    belong to the reader by construction. The rows are the same list_invoices_for_room()
    the console invoice menu reads, so the two cannot disagree about what exists.
    """
    rows = billing.list_invoices_for_room(room)
    return {"room_number": room, "invoices": [
        {"invoice_id": row[0], "room_number": row[1], "invoice_date": str(row[2]),
         "total_amount": float(row[3] or 0.0), "amount_paid": float(row[4] or 0.0)}
        for row in rows
    ]}


@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: int, principal: dict = Depends(require_staff)):
    """One invoice, itemized: header, the room/F&B lines and the payments that settled it.

    Staff-only (guest name, card last four). The data is billing.load_invoice(), the
    same twin print_invoice() renders, so a change to what an invoice contains lands
    once. A missing invoice is 404; an unreachable database is 503.
    """
    try:
        loaded = billing.load_invoice(invoice_id)
    except RuntimeError:
        raise _unavailable()
    if not loaded:
        raise HTTPException(404, f"No invoice #{invoice_id}.")
    inv, items, payments = loaded
    return {
        "invoice_id": inv.InvoiceID, "room_number": inv.RoomNumber,
        "invoice_date": str(inv.InvoiceDate),
        "guest_name": (inv.GuestName or "").strip() or None,
        "subtotal": float(inv.Subtotal or 0.0),
        "discount_code_amount": float(inv.DiscountCodeAmount or 0.0),
        "tier_discount_amount": float(inv.TierDiscountAmount or 0.0),
        "tax_amount": float(inv.TaxAmount or 0.0),
        "total_amount": float(inv.TotalAmount or 0.0),
        "points_redeemed": inv.PointsRedeemed,
        "redemption_value": float(inv.RedemptionValue or 0.0),
        "amount_paid": float(inv.AmountPaid or 0.0),
        "prepaid_amount": float(getattr(inv, "PrepaidAmount", 0) or 0.0),
        "room_subtotal": float(getattr(inv, "RoomSubtotal", 0) or 0.0),
        "room_tax_amount": float(getattr(inv, "RoomTaxAmount", 0) or 0.0),
        "room_total": float(getattr(inv, "RoomTotal", 0) or 0.0),
        "fnb_subtotal": float(getattr(inv, "FnbSubtotal", 0) or 0.0),
        "fnb_discount_code_amount": float(getattr(inv, "FnbDiscountCodeAmount", 0) or 0.0),
        "fnb_tier_discount_amount": float(getattr(inv, "FnbTierDiscountAmount", 0) or 0.0),
        "fnb_tax_amount": float(getattr(inv, "FnbTaxAmount", 0) or 0.0),
        "items": [
            {"id": t.ID, "item_name": t.ItemName, "quantity": t.Quantity or 1,
             "unit_price": float(t.UnitPrice or 0.0), "amount": float(t.Amount or 0.0),
             "paid_earlier": bool(t.PaidEarlier),
             "charge_group": t.ChargeGroup or core.CHARGE_GROUP_FNB,
             "created_at": str(t.CreatedAt)}
            for t in items
        ],
        "payments": [
            {"kind": p.Kind, "amount": float(p.Amount or 0.0), "card_last4": p.CardLast4,
             "paid_at": str(p.PaidAt), "notes": p.Notes,
             "applied_to_invoice_id": p.AppliedToInvoiceID}
            for p in payments
        ],
    }


@router.get("/rooms/{room}/folio")
def room_folio(room: str, principal: dict = Depends(require_staff)):
    """The live folio a room's next check-out would bill, oldest line first.

    Staff-only. Read through billing.open_folio(), the same window
    bill_room_transactions() bills: unbilled lines, plus pay-now rows awaiting an
    invoice. `paid_earlier` is true exactly for those pay-now rows.
    """
    rows = billing.open_folio(room)
    return {"room_number": room, "lines": [
        {"id": row[0], "item_name": row[1], "quantity": row[2] or 1,
         "unit_price": float(row[3] or 0.0), "amount": float(row[4] or 0.0),
         "paid_earlier": bool(row[5]),
         "charge_group": row[6] or core.CHARGE_GROUP_FNB,
         "created_at": str(row[7])}
        for row in rows
    ]}


@router.get("/rooms/{room}/outstanding")
def room_outstanding(room: str, principal: dict = Depends(require_staff)):
    """What is still true before the stay's settlement counts as finished; empty = done.

    Staff-only (stay state). The strings are exactly what settlement_outstanding()
    reports on the console; here it is called with the room alone, so the stay-points
    check -- which needs the stay's customer id and dates, not derivable from a room
    number -- is skipped.
    """
    return {"room_number": room,
            "outstanding": billing.settlement_outstanding(room)}


def _order_row(row):
    """Map get_orders_for_room()'s tuple to JSON (order_id, status, placed, updated, items, notes)."""
    return {"order_id": row[0], "status": row[1], "placed_at": str(row[2]),
            "updated_at": str(row[3]), "items": row[4], "notes": row[5]}


@router.get("/rooms/{room}/orders")
def room_orders(room: str, active_only: bool = False, principal: dict = Depends(require_staff)):
    """A room's orders, newest first; `?active_only=true` drops completed/cancelled ones.

    Staff-only: an order says what a guest bought and when. The rows are the same
    get_orders_for_room() the console's order menu reads, so the web cannot disagree
    with the queue staff see.
    """
    rows = orders.get_orders_for_room(room, active_only=active_only)
    return {"room_number": room, "orders": [_order_row(r) for r in rows]}


@router.get("/guests/me/orders")
def my_orders(principal: dict = Depends(require_guest)):
    """The signed-in guest's own orders, across the rooms they hold, newest first.

    Guest-only. Keyed on the cookie's verified CustomerID: the stays come from
    bookings.customer_stays(), so a same-named stranger's orders can never appear
    (docs/BOOKING.md privacy rules).
    """
    own = [stay[0] for stay in bookings.customer_stays(principal["customer_id"])]
    results = []
    for room in own:
        results.extend(_order_row(r) for r in orders.get_orders_for_room(room))
    results.sort(key=lambda o: o["placed_at"], reverse=True)
    return {"customer_id": principal["customer_id"], "orders": results}


@router.get("/orders/{order_id}")
def order_detail(order_id: int, principal: dict = Depends(require_staff)):
    """One order with its line items.

    Staff-only. A missing order is 404; an unreachable database is 503.
    """
    try:
        record = orders.get_order(order_id)
    except RuntimeError:
        raise _unavailable()
    if not record:
        raise HTTPException(404, f"No order #{order_id}.")
    return {
        "order_id": record[0], "room_number": record[1], "status": record[2],
        "placed_at": str(record[3]), "updated_at": str(record[4]), "notes": record[5],
        "items": [
            {"item_name": i[0], "quantity": i[1], "unit_price": float(i[2] or 0.0)}
            for i in orders.get_order_items(order_id)
        ],
    }


@router.post("/rooms/{room}/orders", status_code=201)
def create_order(room: str, body: CreateOrderRequest,
                 principal: dict = Depends(require_principal)):
    """Place a room-service order: charge the card now, or add it to the room bill.

    A staff member may order for any room; a signed-in guest only for a room among their
    own stays. Each line's price is quoted server-side from the catalogue, so a client
    cannot set prices, and a pay-now total is priced exactly as the console prices it
    (discount code + loyalty tier + tax) through the same billing twins. A declined card
    records nothing and answers 402.
    """
    actor = auth.audit_actor(principal)
    if principal["kind"] == "guest":
        own = {stay[0] for stay in bookings.customer_stays(principal["customer_id"])}
        if room not in own:
            raise HTTPException(403, "You can only order for your own room.")
    lines = []
    for line in body.items:
        price = items.get_dynamic_price(line.item_id)
        if price is None:
            raise HTTPException(400, f"Item {line.item_id} is not available.")
        lines.append((line.item_id, float(price), line.quantity))
    card_processor = None
    if body.pay_mode == "pay_now":
        if not body.card_number or not body.expiration_date:
            raise HTTPException(400, "Card number and expiration are required to pay now.")
        ok, reason, last4 = payments.validate_card(body.card_number, body.expiration_date, body.cvv)
        if not ok:
            raise HTTPException(400, reason)
        card_processor = lambda amount: (True, last4)
    result = orders.place_order(
        room, lines, pay_mode=body.pay_mode, card_processor=card_processor,
        discount_code=body.discount_code, actor=actor)
    if result["status"] == "declined":
        raise HTTPException(402, "The card was declined; nothing was ordered.")
    return {"status": "ok", "order_id": result["order_id"], "room_number": room,
            "total": result["total"], "paid": result["paid"], "pay_mode": body.pay_mode}


@router.post("/orders/{order_id}/advance")
def advance_order(order_id: int, body: AdvanceOrderRequest,
                  principal: dict = Depends(require_staff)):
    """Advance an order to its next state, or cancel it.

    Staff-only (the same function the staff "Order Management" menu calls). The pre-read
    answers 404 for a missing order and 503 for an unreachable database before any write;
    the audit row and the in-room notification name the web actor.
    """
    try:
        record = orders.get_order(order_id)
    except RuntimeError:
        raise _unavailable()
    if not record:
        raise HTTPException(404, f"No order #{order_id}.")
    status = orders.advance_order(order_id, action=body.action,
                                  actor=auth.audit_actor(principal))
    if status is None:
        raise HTTPException(500, f"Order #{order_id} could not be updated.")
    return {"order_id": order_id, "status": status}


@router.get("/admin/users")
def admin_users(principal: dict = Depends(require_management)):
    """Usernames and roles (never passwords), excluding the master override account.

    Manager-or-admin, like the console's "View Users". The master-gated password screen
    has no API twin on purpose: a credential never travels over HTTP.
    """
    return {"users": [{"username": u, "role": r} for (u, r) in admin.list_users()]}


@router.get("/admin/items")
def admin_items(principal: dict = Depends(require_management)):
    """The item catalogue, read exactly as the console's Items screen reads it."""
    return {"items": [{"item_id": i, "name": n, "price": float(p), "pricing_rule": rule}
                      for (i, n, p, rule) in items.list_items()]}


@router.get("/admin/discounts")
def admin_discounts(principal: dict = Depends(require_management)):
    """The discount codes on the book, for the manager tier up."""
    return {"discounts": [{"code": c, "percentage": float(p)}
                          for (c, p) in admin.list_discount_codes()]}


@router.get("/admin/promotions")
def admin_promotions(principal: dict = Depends(require_admin)):
    """Current promotions -- admin-only, like the console's Manage Promotions."""
    return {"promotions": [
        {"promotion_id": p[0], "title": p[1], "details": p[2], "discount_code": p[3]}
        for p in orders.get_promotions()
    ]}


@router.post("/bookings", status_code=201)
def create_booking(request: Request, body: CreateBookingRequest,
                   principal: dict = Depends(require_principal)):
    """The booking desk over HTTP: quote, claim, pay, commit in one transaction.

    The quote is computed server-side exactly as the wizard does (book_room): the
    room type's rate, the tax, and the deposit/full options -- a client cannot pay
    less than the deposit it chose. A signed-in guest is always booked as their own
    customer; a staff member may book on a guest's behalf by passing customer_id.
    """
    actor = auth.audit_actor(principal)
    customer_id = body.customer_id
    if principal["kind"] == "guest":
        customer_id = principal["customer_id"]

    try:
        rates = dict(rooms.get_room_types() or [])
    except Exception:
        raise _unavailable()
    if body.room_type not in rates:
        raise HTTPException(400, f"No room type '{body.room_type}' is available to book.")
    nightly_rate = rates[body.room_type]
    nights = core.stay_nights(body.check_in, body.check_out)
    if nights <= 0:
        raise HTTPException(400, "The stay must be at least one night long.")
    if body.check_in < core.business_date():
        raise HTTPException(400, "The check-in date cannot be in the past.")

    options = booking_ledger.booking_payment_options(
        nights, nightly_rate, core.get_tax_rate())
    matches = [option for option in options if option[2] == body.pay_kind]
    if not matches:
        raise HTTPException(400, f"'{body.pay_kind}' is not a payment option for this quote.")
    pay_amount = matches[0][3]
    card_processor = None
    if float(pay_amount or 0.0) > 0:
        if not body.card_number or not body.expiration_date:
            raise HTTPException(400, "Card number and expiration are required for this payment option.")
        ok, reason, last4 = payments.validate_card(
            body.card_number, body.expiration_date, body.cvv)
        if not ok:
            raise HTTPException(400, reason)
        card_processor = lambda amount: (True, last4)

    result = bookings.create_booking(
        customer_id=customer_id, last_name=body.last_name, first_name=body.first_name,
        room_type=body.room_type, check_in=body.check_in, check_out=body.check_out,
        pay_kind=body.pay_kind, pay_amount=pay_amount,
        card_processor=card_processor, actor=actor)
    status = result["status"]
    if status == "ok":
        return {"status": "ok", "booking_ref": result["booking_ref"],
                "room_number": result["room_number"], "room_type": result["room_type"],
                "nightly_rate": result["nightly_rate"],
                "card_last4": result.get("card_last4")}
    if status == "unavailable":
        raise _unavailable()
    if status == "no_type":
        raise HTTPException(400, result["reasons"][0])
    if status == "no_room":
        raise HTTPException(409, {
            "message": "The room was taken while you were paying; nothing was booked.",
            "reasons": result["reasons"],
        })
    if status == "declined":
        raise HTTPException(402, "The card was declined; nothing was booked.")
    if status == "payment_error":
        raise HTTPException(500, "The payment could not be recorded; nothing was booked.")
    if status == "commit_failed":
        raise HTTPException(500, f"Booking could not be saved: {result.get('error')}")
    raise HTTPException(500, f"Unexpected booking result: {status}")


@router.post("/reservations/{room}/check-in")
def check_in(room: str, body: CheckInRequest, principal: dict = Depends(require_staff)):
    """Staff check-in for a known stay.

    The gate runs first (check_in_eligibility), so a bogus room gets a 404 and a stay
    outside its window a 409 before any state changes, then perform_check_in() makes
    the room occupied, upserts the guest profile and issues the key card -- the same
    state changes the console flow makes, with the audit row naming the web actor.
    """
    gate = reservations.check_in_eligibility(room)
    reason = gate.get("reason")
    if reason == "unavailable":
        raise _unavailable()
    if reason == "no_stay":
        raise HTTPException(404, f"No reservation is held for room {room}.")
    if reason == "window":
        raise HTTPException(409, _window_detail(gate))
    result = reservations.perform_check_in(
        room, body.first_name, email=body.email, phone=body.phone,
        actor=auth.audit_actor(principal))
    if result.get("ok"):
        return {"room_number": result["room_number"],
                "key_card": result.get("key_card"),
                "customer_id": result.get("customer_id")}
    if result.get("reason") == "window":
        raise HTTPException(409, _window_detail(result))
    raise HTTPException(500, "Check-in could not be completed.")


@router.get("/reports/{report_name}")
def report(report_name: str, customer: Optional[str] = None, room: Optional[str] = None,
           start: Optional[str] = None, end: Optional[str] = None,
           floor: Optional[str] = None, date: Optional[str] = None,
           booking_ref: Optional[str] = None, limit: int = 5000,
           principal: dict = Depends(require_staff)):
    """Any report's rows without the CSV file; the same options the CLI takes.

    Staff-only: these carry guest data (names, emails, card last four), which the
    guest-facing surface must never reveal (docs/BOOKING.md privacy rules). The rows
    come from reports.report_rows(), the data twin the CSV exports also run through, so
    the console and the API cannot drift about what a report means.
    """
    if report_name not in reports.REPORT_ROWS:
        raise HTTPException(404, f"Unknown report '{report_name}'.")
    params = {}
    if report_name == "loyalty":
        params = {"customer": customer, "room_number": room}
    elif report_name in ("revenue", "occupancy"):
        params = {"start_date": start, "end_date": end}
    elif report_name == "housekeeping":
        params = {"floor": floor, "on_date": date}
    elif report_name == "booking_ledger":
        params = {"booking_ref": booking_ref}
    elif report_name == "audit":
        params = {"limit": limit}
    params = {key: value for key, value in params.items() if value is not None}
    try:
        headers, rows = reports.report_rows(report_name, **params)
    except ValueError as exc:
        # A filter reference that resolves to nothing or to several guests, e.g. a
        # loyalty statement for a name shared by two profiles.
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return {"report": report_name, "headers": headers, "rows": rows}


def create_app():
    """Build the FastAPI application. Split out so tests can construct an isolated app."""
    web = FastAPI(title="Hotel Management API", version="0.1.0")

    # same_site="lax" is the CSRF baseline for cookie auth on state-changing methods;
    # add a token only if exposure widens beyond the trusted network (PLAN-web-api.md).
    web.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(),
        same_site="lax",
        https_only=False,
    )

    web.include_router(router)
    return web


app = create_app()