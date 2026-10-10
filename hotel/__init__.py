"""The runtime modules behind the two entry points at the repository root.

`main.py` (console) and `api.py` (web) stay at the repo root because that is what
launches the application; everything they import lives in this package (moved here
9 October 2026 so the root holds only launch files and documentation). Cross-module
calls inside the package are relative (`from . import db`) so there is exactly one
binding per module -- a test that patches `hotel.<module>` affects every caller, the
same invariant `tools/split_main.py` established for the post-split root layout.
"""