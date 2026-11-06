# type: ignore
"""Tkinter UI backend.

Implements the same surface as hotel.console_ui so it can be plugged into
hotel.ui via set_backend(). Prompts are modal (blocking) so the domain code's
synchronous flow control keeps working unchanged.
"""
import os
import tkinter as tk
from tkinter import simpledialog, messagebox, ttk

from hotel import clearance_ui

CARD = None


class TkUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Hotel Management")
        self.root.geometry("900x640")

        self._busy = False

        top = tk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)
        self.status_label = tk.Label(top, text="Ready", anchor="w")
        self.status_label.pack(fill="x")

        body = tk.PanedWindow(self.root, orient=tk.VERTICAL)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        log_frame = tk.Frame(body)
        self.log = tk.Text(log_frame, wrap="word", state="disabled", height=24)
        sb = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body.add(log_frame, height=480)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._closed = False

    def _on_close(self):
        self._closed = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def _pump(self):
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass

    def _append(self, text):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass
        self._pump()

    # --- display ---
    def clear_screen(self):
        try:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
        except Exception:
            pass
        self._pump()

    def pause(self, message="Press Enter to continue..."):
        messagebox.showinfo("Continue", message, parent=self.root)
        self._pump()

    def info(self, message):
        self._append(str(message))

    def success(self, message):
        self._append(f"[OK] {message}")

    def error(self, message):
        self._append(f"[ERROR] {message}")
        try:
            messagebox.showerror("Error", str(message), parent=self.root)
        except Exception:
            pass
        self._pump()

    def warning(self, message):
        self._append(f"[WARN] {message}")

    def box(self, title, content, border_style="green"):
        self._append(f"== {title} ==")
        self._append(str(content))
        self._append("")

    def show_menu(self, title, lines, subtitle=None):
        self._append(f"== {title} ==")
        if subtitle:
            self._append(str(subtitle))
            self._append("")
        for line in lines:
            self._append(str(line))
        self._append("")

    def show_table(self, title, headers, rows):
        self._append(f"== {title} ==")
        self._append(" | ".join(str(h) for h in headers))
        for row in rows:
            self._append(" | ".join(str(v) for v in row))
        self._append("")

    def show_status_table(self, title, headers, rows, status_col=2):
        self.show_table(title, headers, rows)

    # --- prompts ---
    def ask_number(self, prompt, default=None, minimum=None, maximum=None):
        while True:
            try:
                val = simpledialog.askinteger("Input", str(prompt), parent=self.root,
                                             initialvalue=default if default is not None else None)
            except Exception:
                val = None
            if val is None:
                # cancelled or empty: mimic console re-prompt unless default given
                if default is not None:
                    val = default
                else:
                    continue
            if minimum is not None and val < minimum:
                messagebox.showerror("Invalid", f"Value must be at least {minimum}.", parent=self.root)
                continue
            if maximum is not None and val > maximum:
                messagebox.showerror("Invalid", f"Value must be at most {maximum}.", parent=self.root)
                continue
            return val

    def ask_confirmation(self, prompt, default="n"):
        res = messagebox.askyesno("Confirm", str(prompt), parent=self.root)
        if res is None:
            return default.lower() in ("y", "yes")
        return bool(res)

    def ask_date(self, prompt, default=None):
        from datetime import datetime
        while True:
            suffix = f" [{default}]" if default else ""
            raw = simpledialog.askstring("Date", f"{prompt}{suffix}", parent=self.root)
            if raw is None:
                raw = ""
            raw = raw.strip()
            if raw == "" and default:
                raw = default
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                messagebox.showerror("Invalid date", "Please use YYYY-MM-DD format.", parent=self.root)

    def ask_optional_date(self, prompt):
        from datetime import datetime
        while True:
            raw = simpledialog.askstring("Date", f"{prompt} (blank to skip)", parent=self.root)
            if raw is None:
                return None
            raw = raw.strip()
            if raw == "":
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                messagebox.showerror("Invalid date", "Please use YYYY-MM-DD format.", parent=self.root)

    def ask_text(self, prompt, default=None):
        suffix = f" [{default}]" if default is not None else ""
        raw = simpledialog.askstring("Input", f"{prompt}{suffix}", parent=self.root,
                                    initialvalue=default if default is not None else None)
        if raw is None:
            raw = ""
        raw = raw.strip()
        if raw == "" and default is not None:
            return default
        return raw

    def ask_optional_text(self, prompt):
        raw = simpledialog.askstring("Input", f"{prompt} (blank to skip)", parent=self.root)
        if raw is None:
            return None
        raw = raw.strip()
        if raw == "":
            return None
        return raw

    def choose(self, prompt, options):
        keys = [o[0] if isinstance(o, tuple) else o for o in options]
        # Build a readable prompt
        lines = [str(prompt), ""]
        for i, k in enumerate(keys, 1):
            label = None
            for o in options:
                if isinstance(o, tuple) and o[0] == k and len(o) > 1:
                    label = o[1]
            lines.append(f"{i}. {label if label is not None else k}")
        while True:
            raw = simpledialog.askstring("Choose", "\n".join(lines), parent=self.root)
            if raw is None:
                raw = ""
            raw = raw.strip()
            if raw in keys:
                return raw
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]
            messagebox.showerror("Invalid", "Invalid choice. Please try again.", parent=self.root)

    def set_busy(self, busy, text=None):
        self._busy = busy
        try:
            self.root.configure(cursor="watch" if busy else "")
            self.status_label.configure(text=text or ("Working..." if busy else "Ready"))
        except Exception:
            pass
        self._pump()

    def open_clearance_window(self):
        try:
            clearance_ui.open_clearance_window()
        except Exception as e:
            self.error(f"Cannot open clearance window: {e}")

    def run(self):
        self.root.mainloop()


# STATUS_STYLES passthrough not needed here; facade handles.
