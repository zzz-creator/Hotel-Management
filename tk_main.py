# type: ignore
"""Tkinter launcher for the hotel app (full-stack clone of the console UI)."""
import main
from hotel import tk_ui

ui = tk_ui.open_tk_app()

if __name__ == "__main__":
    main.main()
    ui.run()
