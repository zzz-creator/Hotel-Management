# type: ignore
"""auth: signed-cookie session handling for the web edition (PLAN-web-api.md phase 3).

The SessionMiddleware in api.py signs the cookie and hands every request a
`request.session` dict; this module decides what goes in it and resolves the
per-request principal from it. Everything here is deliberately free of
FastAPI/Starlette imports, so the module (and its tests) import in the repo's
current environment, where the web dependencies in requirements.txt are not yet
installed. Phase 4 registers `current_principal` with `Depends()` -- FastAPI accepts
any plain callable -- and owns the Request annotation, the cookie `SameSite`
baseline and the HTTPException mapping.

A principal is a plain dict, so it survives the cookie's JSON round-trip:

    {"kind": "staff", "username": ..., "role": ...}     -- admin panel sign-in
    {"kind": "guest", "customer_id": int, "email": ...} -- booking desk sign-in

The audit actor for a staff principal is the username (the console's CURRENT_USER);
for a guest it is the email. Those are the two choices the Phase-2 services
(verify_staff_login, authenticate_customer) already made, and audit_actor() is the
web-path side of them.
"""
import logging

__all__ = [
    'NotAuthenticated',
    'staff_principal',
    'guest_principal',
    'audit_actor',
    'issue_principal',
    'clear_principal',
    'current_principal',
]

PRINCIPAL_KEY = "principal"


class NotAuthenticated(Exception):
    """No signed-in principal for this request. The endpoint layer maps this to HTTP 401."""


def _valid_principal(principal):
    """True when `principal` has the shape a signed cookie should carry.

    The cookie is tamper-proof (signed), but an old payload from a previous version or
    a hand-built one must not half-authenticate: require the kind-specific keys,
    non-empty strings, and an integer `customer_id` (which is how the JSON round-trip
    returns it).
    """
    if not isinstance(principal, dict):
        return False
    kind = principal.get("kind")
    if kind == "staff":
        return (isinstance(principal.get("username"), str) and bool(principal["username"])
                and isinstance(principal.get("role"), str) and bool(principal["role"]))
    if kind == "guest":
        return (isinstance(principal.get("customer_id"), int)
                and isinstance(principal.get("email"), str) and bool(principal["email"]))
    return False


def staff_principal(username, role):
    """The session payload for a signed-in staff member (admin panel login).

    `username` and `role` are exactly what the console's CURRENT_USER and role checks
    use, so a future permission check has them without another query. The audit actor
    is the username, mirroring the console.
    """
    return {"kind": "staff", "username": username, "role": role}


def guest_principal(customer_id, email):
    """The session payload for a signed-in guest at the booking desk.

    `customer_id` is the CustomerProfiles key loyalty is keyed on; `email` is what
    authenticate_customer() audits with, so audit_actor() returns it unchanged.
    """
    return {"kind": "guest", "customer_id": int(customer_id), "email": email}


def audit_actor(principal):
    """The actor the web path passes to log_audit(..., user=...) for this principal.

    Staff rows name the username (like the console's CURRENT_USER); guest rows name
    the email (like authenticate_customer()). A principal that current_principal()
    would reject is a programming error here, so this raises rather than guessing:
    a guessed actor would poison the AuditLog.
    """
    if not _valid_principal(principal):
        raise NotAuthenticated("No valid principal.")
    if principal["kind"] == "guest":
        return principal["email"]
    return principal["username"]


def issue_principal(request, principal):
    """Put `principal` into the signed session cookie (api.py SessionMiddleware).

    `request` is duck-typed: it only needs a `.session` dict, so this module needs no
    Starlette import.
    """
    request.session[PRINCIPAL_KEY] = principal


def clear_principal(request):
    """Drop any signed-in principal; the logout endpoint calls this."""
    request.session.pop(PRINCIPAL_KEY, None)


def current_principal(request):
    """Resolve the signed-in principal for this request, or raise NotAuthenticated.

    Phase 4 registers this with Depends(); the endpoint layer supplies the Request and
    maps the exception to HTTP 401. A malformed payload is cleared and treated as
    signed out, because an old cookie shape must never half-authenticate a later
    version.
    """
    principal = request.session.get(PRINCIPAL_KEY)
    if not _valid_principal(principal):
        request.session.pop(PRINCIPAL_KEY, None)
        raise NotAuthenticated("Not signed in.")
    return principal