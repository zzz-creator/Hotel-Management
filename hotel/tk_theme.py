# type: ignore
"""Modern dark theme for the Tkinter frontend."""
import tkinter as tk
from tkinter import ttk

PALETTE = {
    "bg": "#1e1e2e",
    "surface": "#2a2a3c",
    "sidebar": "#181825",
    "accent": "#89b4fa",
    "accent_hover": "#b4d0fb",
    "text": "#cdd6f4",
    "muted": "#a6adc8",
    "border": "#45475a",
    "success": "#a6e3a1",
    "error": "#f38ba8",
    "warning": "#f9e2af",
}


def apply_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(bg=PALETTE["bg"])
    style.configure(".", background=PALETTE["bg"], foreground=PALETTE["text"],
                    font=("Segoe UI", 10))
    style.configure("TFrame", background=PALETTE["bg"])
    style.configure("Surface.TFrame", background=PALETTE["surface"])
    style.configure("Sidebar.TFrame", background=PALETTE["sidebar"])
    style.configure("TLabel", background=PALETTE["bg"], foreground=PALETTE["text"])
    style.configure("Muted.TLabel", foreground=PALETTE["muted"])
    style.configure("Heading.TLabel", font=("Segoe UI", 14, "bold"), foreground=PALETTE["accent"])
    style.configure("TButton", background=PALETTE["surface"], foreground=PALETTE["text"],
                    borderwidth=0, focusthickness=0, padding=(12, 6))
    style.map("TButton", background=[("active", PALETTE["accent"]), ("pressed", PALETTE["accent"])],
              foreground=[("active", "#1e1e2e")])
    style.configure("Primary.TButton", background=PALETTE["accent"], foreground="#1e1e2e",
                    font=("Segoe UI", 10, "bold"))
    style.map("Primary.TButton", background=[("active", PALETTE["accent_hover"])])
    style.configure("Treeview", background=PALETTE["surface"], foreground=PALETTE["text"],
                    fieldbackground=PALETTE["surface"], borderwidth=0, rowheight=24)
    style.configure("Treeview.Heading", background=PALETTE["sidebar"], foreground=PALETTE["accent"],
                    font=("Segoe UI", 10, "bold"), borderwidth=0)
    style.map("Treeview", background=[("selected", PALETTE["accent"])],
              foreground=[("selected", "#1e1e2e")])
    style.configure("TEntry", fieldbackground=PALETTE["surface"], foreground=PALETTE["text"],
                    borderwidth=1, relief="flat")
    style.configure("TScrollbar", background=PALETTE["surface"], troughcolor=PALETTE["bg"])
    return style
