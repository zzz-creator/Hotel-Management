# type: ignore
"""Inline form helpers for the Tkinter shell."""
import tkinter as tk


class Form:
    def __init__(self, parent, fields, title=None):
        self.fields = fields
        self.frame = tk.Frame(parent, bg=parent.cget("bg") if hasattr(parent, "cget") else None)
        if title:
            tk.Label(self.frame, text=title, font=("Segoe UI", 12, "bold"),
                     bg=self.frame.cget("bg"), fg="#89b4fa").pack(anchor="w", pady=(4, 8))
        self.entries = {}
        self.error_labels = {}
        for label, kind, default in fields:
            row = tk.Frame(self.frame)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=28, anchor="w", bg=self.frame.cget("bg"),
                     fg="#cdd6f4").pack(side="left")
            var = tk.StringVar(value="" if default is None else str(default))
            ent = tk.Entry(row, textvariable=var, bg="#2a2a3c", fg="#cdd6f4",
                           insertbackground="#cdd6f4", relief="flat", highlightthickness=1,
                           highlightbackground="#45475a")
            ent.pack(side="left", fill="x", expand=True, ipady=2)
            self.entries[label] = (var, kind)
            err = tk.Label(self.frame, text="", fg="red", anchor="w")
            err.pack(anchor="w")
            self.error_labels[label] = err
        btn_row = tk.Frame(self.frame)
        btn_row.pack(pady=8)
        self._done = tk.IntVar(value=0)  # 1=submit, 2=cancel
        tk.Button(btn_row, text="Submit", command=lambda: self._done.set(1)).pack(side="left", padx=4)
        tk.Button(btn_row, text="Cancel", command=lambda: self._done.set(2)).pack(side="left", padx=4)
        self.frame.pack(fill="x", padx=8, pady=8)

    def wait(self):
        self.frame.wait_variable(self._done)
        return self._done.get() == 1

    def collect(self):
        result = {}
        for label, (var, kind) in self.entries.items():
            result[label] = var.get().strip()
        return result

    def set_error(self, label, msg):
        self.error_labels[label].configure(text=msg)

    def destroy(self):
        self.frame.destroy()
