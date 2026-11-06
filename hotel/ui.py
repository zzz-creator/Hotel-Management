# type: ignore
"""UI facade.

All interactive menu/panel/table rendering for the hotel app goes through here.
The active backend (console or Tkinter) is selected at startup via `set_backend()`;
every public function delegates to it. When no backend is set, a console backend
is used, so existing behaviour is unchanged.
"""
import os

_backend = None


class _ConsoleBackend:
    def __init__(self):
        from hotel import console_ui as _cu
        self._cu = _cu

    def __getattr__(self, name):
        return getattr(self._cu, name)


def set_backend(backend):
    global _backend
    _backend = backend


def get_backend():
    global _backend
    if _backend is None:
        _backend = _ConsoleBackend()
    return _backend


def _call(name, *a, **k):
    return getattr(get_backend(), name)(*a, **k)


# Re-export the public surface, delegating to the active backend.
def clear_screen(*a, **k):
    return _call("clear_screen", *a, **k)

def pause(*a, **k):
    return _call("pause", *a, **k)

def info(*a, **k):
    return _call("info", *a, **k)

def success(*a, **k):
    return _call("success", *a, **k)

def error(*a, **k):
    return _call("error", *a, **k)

def warning(*a, **k):
    return _call("warning", *a, **k)

def box(*a, **k):
    return _call("box", *a, **k)

def show_menu(*a, **k):
    return _call("show_menu", *a, **k)

def show_table(*a, **k):
    return _call("show_table", *a, **k)

def show_status_table(*a, **k):
    return _call("show_status_table", *a, **k)

def ask_number(*a, **k):
    return _call("ask_number", *a, **k)

def ask_confirmation(*a, **k):
    return _call("ask_confirmation", *a, **k)

def ask_date(*a, **k):
    return _call("ask_date", *a, **k)

def ask_optional_date(*a, **k):
    return _call("ask_optional_date", *a, **k)

def ask_text(*a, **k):
    return _call("ask_text", *a, **k)

def ask_optional_text(*a, **k):
    return _call("ask_optional_text", *a, **k)

def choose(*a, **k):
    return _call("choose", *a, **k)

# STATUS_STYLES passthrough
from hotel import console_ui as _cu
console = _cu.console
STATUS_STYLES = _cu.STATUS_STYLES

# open_clearance_window passthrough
def open_clearance_window(*a, **k):
    return _call("open_clearance_window", *a, **k)
