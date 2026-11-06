# type: ignore
"""Tkinter full-stack frontend for the hotel app.

Inputs are rendered in the main window (no separate message boxes). Display
output goes to a scrollable text panel. Prompt methods block the calling thread
via wait_variable so the synchronous domain flow keeps working.
"""
import os
import tkinter as tk
from tkinter import ttk

from hotel import clearance


class TkinterUI:
    def __init__(self, root=None):
        self.owns_root = root is None
        self.root = root or tk.Tk()
        self.root.title("Hotel Management")
        self.root.geometry("900x600")

        # output panel
        self.output = tk.Text(self.root, wrap="word", state="disabled", font=("Consolas", 10))
        self.output.pack(side="top", fill="both", expand=True, padx=8, pady=8)

        # input bar
        bar = tk.Frame(self.root)
        bar.pack(side="bottom", fill="x", padx=8, pady=8)
        self.prompt_label = tk.Label(bar, text="", anchor="w")
        self.prompt_label.pack(side="left")
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(bar, textvariable=self.entry_var, width=40)
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.submit_btn = tk.Button(bar, text="Submit", command=self._submit)
        self.submit_btn.pack(side="left")
        self._submit_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="")
        self.status = tk.Label(self.root, textvariable=self.status_var, anchor="w", fg="#444444")
        self.status.pack(side="bottom", fill="x", padx=8)

        self._busy = False

    # ---------- internals ----------
    def _append(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        self.root.update_idletasks()

    def _clear_output(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def _submit(self):
        self._submit_var.set(self._submit_var.get() + 1)

    def _wait_submit(self):
        self.entry.focus_set()
        self._submit_var.set(0)
        self.root.wait_variable(self._submit_var)
        return self.entry_var.get()

    def _set_prompt(self, text):
        self.prompt_label.configure(text=text)
        self.entry_var.set("")

    def _clear_prompt(self):
        self.prompt_label.configure(text="")
        self.entry_var.set("")

    # ---------- public UI methods ----------
    def clear_screen(self):
        self._clear_output()

    def pause(self, message="Press Enter to continue..."):
        self._set_prompt(message)
        self._wait_submit()
        self._clear_prompt()

    def info(self, message):
        self._append(str(message))

    def success(self, message):
        self._append(f"OK: {message}")

    def error(self, message):
        self._append(f"ERROR: {message}")

    def warning(self, message):
        self._append(f"WARNING: {message}")

    def box(self, title, content, border_style="green"):
        self._append(f"== {title} ==\n{content}")

    def show_menu(self, title, lines, subtitle=None):
        self._append(f"== {title} ==")
        if subtitle:
            self._append(str(subtitle))
            self._append("")
        for line in lines:
            self._append(str(line))

    def show_table(self, title, headers, rows):
        self._append(f"== {title} ==")
        self._append(" | ".join(str(h) for h in headers))
        for row in rows:
            self._append(" | ".join(str(v) for v in row))

    def show_status_table(self, title, headers, rows, status_col=2):
        self.show_table(title, headers, rows)

    def ask_number(self, prompt, default=None, minimum=None, maximum=None):
        while True:
            suffix = f" [{default}]" if default is not None else ""
            self._set_prompt(f"{prompt}{suffix}:")
            raw = self._wait_submit()
            self._clear_prompt()
            if raw == "" and default is not None:
                return default
            try:
                value = int(raw)
            except ValueError:
                self.error("Invalid input. Please enter a number.")
                continue
            if minimum is not None and value < minimum:
                self.error(f"Value must be at least {minimum}.")
                continue
            if maximum is not None and value > maximum:
                self.error(f"Value must be at most {maximum}.")
                continue
            return value

    def ask_confirmation(self, prompt, default="n"):
        self._set_prompt(f"{prompt} (y/N):")
        raw = self._wait_submit().strip().lower()
        self._clear_prompt()
        if raw == "":
            return default.lower() in ("y", "yes")
        return raw in ("y", "yes")

    def ask_date(self, prompt, default=None):
        from datetime import datetime
        while True:
            suffix = f" [{default}]" if default else ""
            self._set_prompt(f"{prompt}{suffix}:")
            raw = self._wait_submit().strip()
            self._clear_prompt()
            if raw == "" and default:
                raw = default
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                self.error("Invalid date. Please use YYYY-MM-DD format.")

    def ask_optional_date(self, prompt):
        from datetime import datetime
        while True:
            self._set_prompt(f"{prompt} (blank to skip):")
            raw = self._wait_submit().strip()
            self._clear_prompt()
            if raw == "":
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                self.error("Invalid date. Please use YYYY-MM-DD format.")

    def ask_text(self, prompt, default=None):
        suffix = f" [{default}]" if default is not None else ""
        self._set_prompt(f"{prompt}{suffix}:")
        raw = self._wait_submit().strip()
        self._clear_prompt()
        if raw == "" and default is not None:
            return str(default).strip()
        return raw

    def ask_optional_text(self, prompt):
        self._set_prompt(f"{prompt} (blank to skip):")
        raw = self._wait_submit().strip()
        self._clear_prompt()
        if raw == "":
            return None
        return raw

    def open_clearance_window(self):
        try:
            from hotel import clearance_ui
            # reuse existing window but attach to self.root as parent
            clearance_ui.ClearanceWindow(root=self.root).run() if False else None
        except Exception as exc:
            self.error(f"Cannot open clearance window: {exc}")

    def run(self):
        if self.owns_root:
            self.root.mainloop()


def open_tk_app():
    ui = TkinterUI()
    # monkeypatch hotel.ui so domain code uses this backend
    import hotel.ui as ui_mod
    for name in ["clear_screen", "pause", "info", "success", "error", "warning",
                 "box", "show_menu", "show_table", "show_status_table",
                 "ask_number", "ask_confirmation", "ask_date", "ask_optional_date",
                 "ask_text", "ask_optional_text", "open_clearance_window"]:
        setattr(ui_mod, name, getattr(ui, name))
    return ui
