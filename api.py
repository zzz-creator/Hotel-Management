# type: ignore
"""FastAPI web/API edition of the hotel management app (see PLAN-web-api.md).

This is a *second* entry point, not a replacement. `python main.py` keeps working
unchanged and remains the fallback; `uvicorn api:app` serves this. Neither front-end
owns a business rule: both call the same non-interactive service functions in the
domain modules, so the two cannot drift (AGENTS.md section 5).

Phases 1-3 built the scaffold, the service twins and the cookie-session layer;
phase 4 adds the pilot endpoints. Every handler touches the database only through
a service function and is a plain `def`, so Starlette runs the blocking pyodbc call
in its threadpool; never make such a handler `async def`, because a synchronous
database call inside the event loop blocks every other request.
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
import booking_ledger
import bookings
import customer
import payments
import reservations
import rooms
from schemas import CheckInRequest, CreateBookingRequest, LoginRequest

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