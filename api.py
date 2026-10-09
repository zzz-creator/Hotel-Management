# type: ignore
"""FastAPI web/API edition of the hotel management app (see PLAN-web-api.md).

This is a *second* entry point, not a replacement. `python main.py` keeps working
unchanged and remains the fallback; `uvicorn api:app` serves this. Neither front-end
owns a business rule: both call the same non-interactive service functions in the
domain modules, so the two cannot drift (AGENTS.md section 5).

Phase 1 is a scaffold only -- the app, the signed-cookie session middleware and a
health probe. The pilot endpoints (auth, availability, create-booking, check-in) arrive
in later phases. Every handler that touches the database must be a plain `def` so
Starlette runs the blocking pyodbc call in its threadpool; never make such a handler
`async def`, because a synchronous database call inside the event loop blocks every
other request.
"""
import logging
import os

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

# Importing core also reads config.ini and calls db.init(...), which wires the database
# connection the service functions will use. Unlike `python main.py`, this never triggers
# core._ensure_database_config()'s interactive setup prompt: an API server must not block on
# stdin. A missing config.ini simply leaves connections returning None, which every
# get_connection() caller already handles.
import core  # noqa: F401

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

    @web.get("/api/health")
    def health():
        """Liveness probe. Deliberately does not open a database connection."""
        return {"status": "ok"}

    return web


app = create_app()
