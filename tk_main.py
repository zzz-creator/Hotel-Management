# type: ignore
"""Tkinter entry point for the hotel management app.

Keeps the console path (main.py) untouched: this launcher installs the
Tkinter UI backend, overrides input() so the domain code's direct input()
calls open modal dialogs, then runs the same startup + main loop as main.py.
"""
import builtins
import sys

from hotel import ui
from hotel import tk_ui

backend = tk_ui.TkUI()
ui.set_backend(backend)

# Route every direct input() call in the domain modules through the backend.
_original_input = builtins.input


def _tk_input(prompt=""):
    try:
        return ui.ask_text("" if prompt is None else str(prompt))
    except Exception:
        return _original_input(prompt)


builtins.input = _tk_input

import main as app  # noqa: E402  (must come after backend install)

if __name__ == "__main__":
    try:
        app.main()
    except SystemExit:
        pass
    except Exception as e:
        ui.error(f"Fatal error: {e}")
        raise
