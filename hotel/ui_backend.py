# type: ignore
"""UI backend selection.

Domain code calls `hotel.ui.*` which delegates to the active backend. The console
backend is the default (None means "use the console implementation in ui.py").
"""
_active = None


def set_backend(backend):
    global _active
    _active = backend


def get_backend():
    return _active


def has_backend():
    return _active is not None
