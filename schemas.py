# type: ignore
"""Pydantic request models for the web edition (PLAN-web-api.md phase 4).

One schema per pilot endpoint. The values that must match the services are the
booking_ledger payment kinds ("Deposit"/"Prepayment"), spelled out as Literals so a
client cannot invent a third kind; dates are `date`, which pydantic parses from the
ISO "YYYY-MM-DD" strings the console uses.
"""
from datetime import date
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class StaffLoginRequest(BaseModel):
    """Login body for a staff account (admin panel).

    `username`/`password` are exactly what admin.verify_staff_login() compares; the
    lockout counter and audit rows are the service's business, not the endpoint's.
    """
    kind: Literal["staff"]
    username: str
    password: str


class GuestLoginRequest(BaseModel):
    """Login body for a booking-desk guest account.

    `email` is the only handle CustomerProfiles keys a login on; `password` was set by
    the guest at first-use registration.
    """
    kind: Literal["guest"]
    email: str
    password: str


LoginRequest = Annotated[
    Union[StaffLoginRequest, GuestLoginRequest],
    Field(discriminator="kind"),
]


class CreateBookingRequest(BaseModel):
    """Body for POST /api/bookings, mirroring the wizard's inputs (book_room()).

    `check_in`/`check_out` are dates; the nightly rate is quoted server-side from the
    room type exactly as the wizard does, so a client cannot underpay a deposit by
    declaring its own amount. `pay_kind` is required and may only be a real
    booking_ledger kind -- the wizard always picks one, and a confirmed booking must be
    backed by a ReservationPayments row (booking_payment_options documents why there is
    no pay-at-check-out path). `customer_id` is ignored for a signed-in guest (their own
    id is used) and free for a staff member booking on a guest's behalf.
    """
    last_name: str
    first_name: str
    room_type: str
    check_in: date
    check_out: date
    pay_kind: Literal["Deposit", "Prepayment"]
    customer_id: Optional[int] = None
    # Card fields are only meaningful when the quoted payment amount is positive.
    card_number: Optional[str] = None
    expiration_date: Optional[str] = None  # MM/YYYY, as the console prompts
    cvv: Optional[str] = None


class CheckInRequest(BaseModel):
    """Body for POST /api/reservations/{room}/check-in.

    The contact fields are exactly what the console prompts between the eligibility
    gate and perform_check_in(); both are optional, as they are on the console.
    """
    first_name: str
    email: Optional[str] = None
    phone: Optional[str] = None


class OrderLine(BaseModel):
    """One line of a room-service order: an item and how many. Prices are quoted
    server-side from the catalogue, so a client never sends a price."""
    item_id: int
    quantity: int = Field(gt=0)


class CreateOrderRequest(BaseModel):
    """Body for POST /api/rooms/{room}/orders.

    `pay_mode` chooses pay-now (a card is charged server-side through
    payments.validate_card; the total is priced exactly as the console prices it) or
    bill (the charges join the room's folio and settle at check-out). `discount_code`
    and the card fields are only meaningful for pay-now.
    """
    items: List[OrderLine] = Field(min_length=1)
    pay_mode: Literal["pay_now", "bill"] = "bill"
    discount_code: Optional[str] = None
    card_number: Optional[str] = None
    expiration_date: Optional[str] = None  # MM/YYYY, as the console prompts
    cvv: Optional[str] = None


class AdvanceOrderRequest(BaseModel):
    """Body for POST /api/orders/{order_id}/advance: the lifecycle move to make.

    "advance" steps to the next status in the lifecycle; "cancel" closes the order. The
    console's numbered prompt is the same two choices.
    """
    action: Literal["advance", "cancel"]


class CreateUserRequest(BaseModel):
    """Body for POST /api/admin/users (admin-only): a new staff account.

    The role is validated as the service's job (create_user rejects anything the
    console's core._prompt_role() cannot offer), so the API cannot mint an account the
    Admin Panel has no branch for.
    """
    username: str
    password: str
    role: str


class SetPasswordRequest(BaseModel):
    """Body for POST /api/admin/users/{username}/password (admin-only).

    Plaintext on purpose, like every login flow in the app (AGENTS.md section 3).
    """
    password: str