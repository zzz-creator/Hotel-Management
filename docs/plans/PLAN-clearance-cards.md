# Clearance Cards — implementation plan

## Goal
Map hotel guests/staff to the SVG "clearance card" assets in `Clearance cards/`,
extract each card's display name from the SVG itself, and show it in a single
tkinter window with the card number scanned at the bottom.

## Card inventory (28 SVGs)
Each SVG carries 4 text groups: header ("CLEARANCE CARD"), badge ("X CLEARANCE"),
role/title (the keycard name, e.g. `DIRECTORY OVERSEER`), Greek-letter badge.
Extraction: parse the `<tspan>` groups; group 3 is the display name.

## Mapping (final)
| Card | Trigger |
|---|---|
| Visitor Pass | Guest with no loyalty account |
| Mayday | Guest checks out with an outstanding balance |
| Sigma / Psi / Omega | `staff` / `manager` / `admin` role |
| Alpha…Epsilon (7) | Bronze × Standard→Presidential |
| Zeta…Mu (7) | Silver × Standard→Presidential |
| Nu…Upsilon (7) | Gold × Standard→Presidential |
| Phi | Platinum × Standard |
| Chi | Platinum × Deluxe and up |

Room category order matches `DEFAULT_ROOM_TYPE_MULTIPLIERS`.

## Files
1. `clearance.py` — card catalog (SVG name extraction), tier×category matrix,
   `clearance_for_guest(tier, category, ...)`, `clearance_for_role(role)`,
   `lookup(scanned)` accepting a room number or username.
2. `clearance_ui.py` — tkinter window: card image, extracted name, scan entry at bottom.
3. `tools/convert_clearance_svgs.py` — optional: rasterize SVGs to PNG when
   `svglib`/`reportlab` is installed; runtime falls back to a drawn card otherwise.
4. `main.py` — menu entry (admin panel + staff desk) opening the window.
5. `tests/test_clearance.py` — name extraction over all SVGs, matrix coverage,
   Platinum-Chi rule, role mapping.
6. Docs: `AGENTS.md` §2 file map, README.
