# UI Input Inventory (Phase 0)

Decoupling complete: no module calls `input()` directly except `hotel/ui.py` and
`hotel/clearance_ui.py`. All prompts go through `ui.ask_*`.

## Direct `input()` counts before decoupling (per module)

- main.py: 1
- hotel/admin.py: 35
- hotel/billing.py: 16
- hotel/bookings.py: 6
- hotel/concierge.py: 4
- hotel/conference.py: 25
- hotel/conference_catering.py: 5
- hotel/core.py: 13
- hotel/customer.py: 23
- hotel/items.py: 27
- hotel/keycards.py: 6 (now ask_text)
- hotel/loyalty.py: 11
- hotel/notifications.py: 7
- hotel/onboarding.py: 23
- hotel/orders.py: 23
- hotel/payments.py: 3
- hotel/reservations.py: 17
- hotel/rooms.py: 7
- hotel/ui.py: 5 (console implementation only)

Total: ~257 sites, all migrated to `ui.ask_text/ask_number/ask_date/...`.

## Display (`ui.*`) call sites

All 17 modules that imported ui continue to call the facade; after Tkinter patching
(`tk_main.py`) these resolve to `TkinterUI` methods.
