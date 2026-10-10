# type: ignore
"""The hotel application package.

Every domain module lives here; `main.py` at the repository root is the entry point
and compatibility facade, so `python main.py` and `import main as app` both keep
working. The modules follow the split rules described in `plans/PLAN-split-main-py.md`:
cross-module references are module-qualified (`billing.post_room_charge(...)`,
`session.CURRENT_USER`), so each name has exactly one binding and patching the owner
module affects every caller.

This file deliberately imports no submodules. An eager import here would run during
package initialisation, before the tier ordering in the split plan is established,
and reintroduce the circular imports the split exists to avoid. Import the module you
need instead: `from hotel import db`.
"""
