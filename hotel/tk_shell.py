# type: ignore
"""Button-driven Tkinter shell: sidebar navigation + content area + log panel."""
import tkinter as tk
from tkinter import ttk

from hotel import tk_ui


class Shell:
    def __init__(self):
        self.ui = tk_ui.TkinterUI.__new__(tk_ui.TkinterUI)
        # build manually to control layout
        self.root = tk.Tk()
        self.root.title("Hotel Management")
        self.root.geometry("1100x700")
        self.ui.root = self.root
        self.ui.owns_root = True
        self.ui.output = None

        # sidebar
        self.sidebar = tk.Frame(self.root, width=180, bg="#2b2b2b")
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # content + log
        right = tk.Frame(self.root)
        right.pack(side="right", fill="both", expand=True)
        self.content = tk.Frame(right)
        self.content.pack(side="top", fill="both", expand=True)
        self.log = tk.Text(right, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(side="bottom", fill="x")

        # input bar
        bar = tk.Frame(right)
        bar.pack(side="bottom", fill="x", padx=8, pady=4)
        self.prompt_label = tk.Label(bar, text="", anchor="w")
        self.prompt_label.pack(side="left")
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(bar, textvariable=self.entry_var, width=40)
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.entry.bind("<Return>", lambda _e: self._submit())
        tk.Button(bar, text="Submit", command=self._submit).pack(side="left")
        self._submit_var = tk.IntVar(value=0)

        # wire ui methods to this shell's widgets
        self.ui.entry_var = self.entry_var
        self.ui.entry = self.entry
        self.ui.prompt_label = self.prompt_label
        self.ui._submit_var = self._submit_var
        self.ui.output = self.log
        self._current = None

    def _submit(self):
        self._submit_var.set(self._submit_var.get() + 1)

    def _wait_submit(self):
        self.entry.focus_set()
        self._submit_var.set(0)
        self.root.wait_variable(self._submit_var)
        return self.entry_var.get()

    def log_append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    def add_sidebar_button(self, text, cmd):
        b = tk.Button(self.sidebar, text=text, command=cmd, anchor="w",
                      bg="#3c3c3c", fg="white", relief="flat", padx=12, pady=8)
        b.pack(fill="x")

    def show_buttons(self, title, items):
        """items: list of (label, callable)"""
        self.clear_content()
        tk.Label(self.content, text=title, font=("Segoe UI", 14, "bold")).pack(pady=8)
        for label, cmd in items:
            tk.Button(self.content, text=label, command=cmd, width=40).pack(pady=3)

    def run(self):
        self.root.mainloop()

    # --- ui-compatible methods (so domain code using ui.* works) ---
    def clear_screen(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def pause(self, message="Press Enter to continue..."):
        self.prompt_label.configure(text=message)
        self._wait_submit()
        self.prompt_label.configure(text="")
        self.entry_var.set("")

    def info(self, message):
        self.log_append(str(message))

    def success(self, message):
        self.log_append(f"OK: {message}")

    def error(self, message):
        self.log_append(f"ERROR: {message}")

    def warning(self, message):
        self.log_append(f"WARNING: {message}")

    def box(self, title, content, border_style="green"):
        self.log_append(f"== {title} ==\n{content}")

    def show_menu(self, title, lines, subtitle=None):
        # fallback text menu (used by not-yet-converted panels)
        self.log_append(f"== {title} ==")
        if subtitle:
            self.log_append(str(subtitle))
        for line in lines:
            self.log_append(str(line))

    def show_table(self, title, headers, rows):
        self.log_append(f"== {title} ==")
        self.log_append(" | ".join(str(h) for h in headers))
        for row in rows:
            self.log_append(" | ".join(str(v) for v in row))

    def show_status_table(self, title, headers, rows, status_col=2):
        self.show_table(title, headers, rows)

    def ask_number(self, prompt, default=None, minimum=None, maximum=None):
        while True:
            suffix = f" [{default}]" if default is not None else ""
            self.prompt_label.configure(text=f"{prompt}{suffix}:")
            raw = self._wait_submit().strip()
            self.prompt_label.configure(text="")
            self.entry_var.set("")
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
        self.prompt_label.configure(text=f"{prompt} (y/N):")
        raw = self._wait_submit().strip().lower()
        self.prompt_label.configure(text="")
        self.entry_var.set("")
        if raw == "":
            return default.lower() in ("y", "yes")
        return raw in ("y", "yes")

    def ask_date(self, prompt, default=None):
        from datetime import datetime
        while True:
            suffix = f" [{default}]" if default else ""
            self.prompt_label.configure(text=f"{prompt}{suffix}:")
            raw = self._wait_submit().strip()
            self.prompt_label.configure(text="")
            self.entry_var.set("")
            if raw == "" and default:
                raw = default
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                self.error("Invalid date. Please use YYYY-MM-DD format.")

    def ask_optional_date(self, prompt):
        from datetime import datetime
        while True:
            self.prompt_label.configure(text=f"{prompt} (blank to skip):")
            raw = self._wait_submit().strip()
            self.prompt_label.configure(text="")
            self.entry_var.set("")
            if raw == "":
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                self.error("Invalid date. Please use YYYY-MM-DD format.")

    def ask_text(self, prompt, default=None):
        suffix = f" [{default}]" if default is not None else ""
        self.prompt_label.configure(text=f"{prompt}{suffix}:")
        raw = self._wait_submit().strip()
        self.prompt_label.configure(text="")
        self.entry_var.set("")
        if raw == "" and default is not None:
            return str(default).strip()
        return raw

    def ask_optional_text(self, prompt):
        self.prompt_label.configure(text=f"{prompt} (blank to skip):")
        raw = self._wait_submit().strip()
        self.prompt_label.configure(text="")
        self.entry_var.set("")
        if raw == "":
            return None
        return raw

    def open_clearance_window(self):
        try:
            from hotel import clearance_ui
            import tkinter as tk
            top = tk.Toplevel(self.root)
            top.title("Clearance Card Desk")
            clearance_ui.ClearanceWindow(root=top)
        except Exception as exc:
            self.error(f"Cannot open clearance window: {exc}")
