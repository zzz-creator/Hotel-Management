# type: ignore
"""Pydantic request models for the web edition (PLAN-web-api.md phase 4).

One schema per pilot endpoint. The values that must match the services are the
booking_ledger payment kinds ("Deposit"/"Prepayment"), spelled out as Literals so a
client cannot invent a third kind; dates are `date`, which pydantic parses from the
ISO "YYYY-MM-DD" strings the console uses.
"""
from datetime import date
from typing import Annotated, Literal, Optional, Union

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