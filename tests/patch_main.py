# type: ignore
"""Test helper: patch the object that actually owns a name after the main.py split.

Before the split every function and module-level global lived in main.py, so tests wrote
`mock.patch.object(main, "get_connection", ...)`. After the split the name lives in its
domain module and every caller reaches it as `db.get_connection` / `core.get_setting` /
`session.CURRENT_USER`, so patching `main` would miss every caller. `patch_main(name, ...)`
resolves the owner module and patches there instead, keeping the tests meaningful.

Ownership is authoritative rather than guessed: each module declares `__all__` listing
exactly the names it binds (see tools/split_main.py). `ui` and `db` are imported modules and
`config`/`config_path` belong to core; those four are patched where they are *used*.

Usage:
    from patch_main import patch_main

    with patch_main("get_connection", return_value=FakeConn()):
        ...
"""
import contextlib
from unittest import mock

from hotel import db
from hotel import session
from hotel import core
from hotel import rooms
from hotel import items
from hotel import loyalty
from hotel import payments
from hotel import keycards
from hotel import reservations
from hotel import booking_ledger
from hotel import billing
from hotel import orders
from hotel import notifications
from hotel import bookings
from hotel import customer
from hotel import concierge
from hotel import admin
from hotel import onboarding
import main

# Owner-resolution order. session and main are listed explicitly because they are the two
# modules that hold state rather than addressable helpers.
MODULES = [session, core, rooms, items, loyalty, payments, keycards, reservations,
           booking_ledger, billing, orders, notifications, bookings, customer, concierge,
           admin, onboarding]

# Names defined by main.py itself (the facade / logging / entry point).
MAIN_NAMES = {'handle_cli_args', 'CustomFormatter', 'logger'}

# Names that are imports elsewhere, not owned state: patch where they are used.
WHOLE_MODULE = {'ui', 'db'}
CORE_IMPORT = {'config', 'config_path'}

_MISSING = object()


def owner_of(name):
    """The module that binds `name`. Raises AttributeError if nobody does."""
    if name in MAIN_NAMES:
        return main
    for module in MODULES:
        if name in getattr(module, '__all__', ()):
            return module
    # session.py predates __all__; fall back to a plain attribute probe for it.
    if hasattr(session, name):
        return session
    raise AttributeError("patch_main: no module owns %r" % (name,))


def patch_main(name, *args, **kwargs):
    """Like mock.patch.object, but against the module that owns `name` after the split."""
    if name == 'get_connection':
        # Every caller goes through db.get_connection now; main's alias is only for the
        # identity probe in tests/verify_e2e.py.
        return mock.patch.object(db, 'get_connection', *args, **kwargs)
    if name in CORE_IMPORT:
        return mock.patch.object(core, name, *args, **kwargs)
    if name in WHOLE_MODULE:
        return _WholeModulePatch(name, args, kwargs)
    return mock.patch.object(owner_of(name), name, *args, **kwargs)


class _WholeModulePatch:
    """Replace an imported submodule (`ui`, `db`) in every project module that binds it.

    `ui` and `db` are imported by many modules, so swapping the binding in one place leaves
    the others calling the real thing. This installs one replacement everywhere and
    restores every binding on exit. One mock object is shared so `with patch_main("db") as
    db_mock:` asserts against the object the code actually received.
    """

    def __init__(self, name, args, kwargs):
        self.name = name
        self.kwargs = dict(kwargs)
        explicit = args[0] if args else self.kwargs.pop('new', _MISSING)
        self.replacement = mock.MagicMock() if explicit is _MISSING else explicit
        self.stack = None

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        for module in MODULES + [main]:
            if hasattr(module, self.name):
                self.stack.enter_context(
                    mock.patch.object(module, self.name, self.replacement, **self.kwargs))
        return self.replacement

    def __exit__(self, *exc_info):
        return self.stack.__exit__(*exc_info)
