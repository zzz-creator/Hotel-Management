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
        from hotel import tk_theme
        tk_theme.apply_theme(self.root)
        self.palette = tk_theme.PALETTE
        self.ui.root = self.root
        self.ui.owns_root = True
        self.ui.output = None

        # sidebar
        self.sidebar = tk.Frame(self.root, width=200, bg=self.palette["sidebar"])
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # content + log
        right = tk.Frame(self.root)
        right.pack(side="right", fill="both", expand=True)
        self.content = tk.Frame(right)
        self.content.pack(side="top", fill="both", expand=True)
        self.prompt_area = tk.Frame(right)
        self.prompt_area.pack(side="top", fill="x")
        # log drawer (collapsible)
        self.log_toggle = tk.Button(right, text="Show log", command=self._toggle_log, relief="flat",
                                    bg=self.palette["surface"], fg=self.palette["muted"])
        self.log_toggle.pack(side="bottom", fill="x")
        self.log = tk.Text(right, height=8, wrap="word", state="disabled", font=("Consolas", 9),
                           bg=self.palette["surface"], fg=self.palette["text"])
        self.log.pack(side="bottom", fill="x")
        self.log_visible = True

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
                      bg=self.palette["sidebar"], fg=self.palette["text"], relief="flat",
                      padx=16, pady=10, font=("Segoe UI", 10, "bold"),
                      activebackground=self.palette["accent"], activeforeground="#1e1e2e")
        b.pack(fill="x")

    def show_buttons(self, title, items):
        """items: list of (label, callable)"""
        self.clear_content()
        tk.Label(self.content, text=title, font=("Segoe UI", 14, "bold"),
                 bg=self.palette["bg"], fg=self.palette["accent"]).pack(pady=(12, 8), anchor="w", padx=12)
        grid = tk.Frame(self.content, bg=self.palette["bg"])
        grid.pack(fill="both", expand=True, padx=12)
        for i, (label, cmd) in enumerate(items):
            r, c = divmod(i, 2)
            b = tk.Button(grid, text=label, command=cmd, width=34,
                          bg=self.palette["surface"], fg=self.palette["text"], relief="flat",
                          padx=10, pady=8, font=("Segoe UI", 10),
                          activebackground=self.palette["accent"], activeforeground="#1e1e2e")
            b.grid(row=r, column=c, padx=6, pady=4, sticky="ew")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

    def _toggle_log(self):
        if self.log_visible:
            self.log.pack_forget()
            self.log_toggle.configure(text="Show log")
            self.log_visible = False
        else:
            self.log.pack(side="bottom", fill="x", before=self.log_toggle)
            self.log_toggle.configure(text="Hide log")
            self.log_visible = True

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
        import tkinter as tk
        from tkinter import ttk
        self.clear_content()
        tk.Label(self.content, text=title, font=("Segoe UI", 12, "bold")).pack(pady=4)
        tree = ttk.Treeview(self.content, columns=[str(h) for h in headers], show="headings")
        for h in headers:
            tree.heading(str(h), text=str(h))
            tree.column(str(h), width=120)
        for row in rows:
            tree.insert("", "end", values=[str(v) for v in row])
        vsb = ttk.Scrollbar(self.content, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def show_status_table(self, title, headers, rows, status_col=2):
        import tkinter as tk
        from tkinter import ttk
        self.clear_content()
        tk.Label(self.content, text=title, font=("Segoe UI", 12, "bold")).pack(pady=4)
        tree = ttk.Treeview(self.content, columns=[str(h) for h in headers], show="headings")
        for h in headers:
            tree.heading(str(h), text=str(h))
            tree.column(str(h), width=120)
        colors = {"Available": "green", "Occupied": "cyan", "Cleaning": "orange", "Dirty": "red", "Maintenance": "magenta"}
        for row in rows:
            tag = None
            try:
                status = str(row[status_col]).strip()
                if status in colors:
                    tag = status
                    tree.tag_configure(tag, foreground=colors[status])
            except Exception:
                pass
            tree.insert("", "end", values=[str(v) for v in row], tags=(tag,) if tag else ())
        vsb = ttk.Scrollbar(self.content, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def ask_number(self, prompt, default=None, minimum=None, maximum=None):
        from hotel import tk_forms
        while True:
            for w in self.prompt_area.winfo_children():
                w.destroy()
            form = tk_forms.Form(self.prompt_area, [("value", "number", default)], title=prompt)
            ok = form.wait()
            raw = form.collect().get("value", "") if ok else ""
            form.destroy()
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
        from hotel import tk_forms
        for w in self.prompt_area.winfo_children():
            w.destroy()
        form = tk_forms.Form(self.prompt_area, [("value", "text", None)], title=f"{prompt} (y/N)")
        ok = form.wait()
        raw = form.collect().get("value", "") if ok else ""
        form.destroy()
        if raw == "":
            return default.lower() in ("y", "yes")
        return raw.lower() in ("y", "yes")

    def ask_date(self, prompt, default=None):
        from datetime import datetime
        while True:
            for w in self.prompt_area.winfo_children():
                w.destroy()
            form = None
            from hotel import tk_forms
            form = tk_forms.Form(self.prompt_area, [("value", "date", default)], title=prompt)
            ok = form.wait()
            raw = form.collect().get("value", "") if ok else ""
            form.destroy()
            if raw == "" and default:
                raw = default
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                self.error("Invalid date. Please use YYYY-MM-DD format.")

    def ask_optional_date(self, prompt):
        from datetime import datetime
        while True:
            from hotel import tk_forms
            for w in self.prompt_area.winfo_children():
                w.destroy()
            form = tk_forms.Form(self.prompt_area, [("value", "date", None)], title=f"{prompt} (blank to skip)")
            ok = form.wait()
            raw = form.collect().get("value", "") if ok else ""
            form.destroy()
            if raw == "":
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                self.error("Invalid date. Please use YYYY-MM-DD format.")

    def ask_text(self, prompt, default=None):
        from hotel import tk_forms
        for w in self.prompt_area.winfo_children():
            w.destroy()
        form = tk_forms.Form(self.prompt_area, [("value", "text", default)], title=prompt)
        ok = form.wait()
        val = form.collect().get("value", "") if ok else ""
        form.destroy()
        if val == "" and default is not None:
            return str(default).strip()
        return val

    def ask_optional_text(self, prompt):
        from hotel import tk_forms
        for w in self.prompt_area.winfo_children():
            w.destroy()
        form = tk_forms.Form(self.prompt_area, [("value", "text", None)], title=f"{prompt} (blank to skip)")
        ok = form.wait()
        raw = form.collect().get("value", "") if ok else ""
        form.destroy()
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
