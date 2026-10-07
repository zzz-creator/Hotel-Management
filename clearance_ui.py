"""Tkinter 'tap your keycard' window for clearance cards.

One window: the card image and extracted name up top, a status line, and the
card-number entry along the bottom. Enter (or a scanner that ends with
Enter) looks the card up and swaps the display.
"""

import os
import re
import tempfile
import logging

import tkinter as tk

import clearance

CARD_W, CARD_H = 338, 189  # 4x the 84.5 x 47.25 card viewBox


def _band_color(card):
    try:
        with open(card["svg"], "r", encoding="utf-8") as f:
            fills = re.findall(r'fill="(#[0-9a-fA-F]{6})"', f.read())
    except OSError:
        fills = []
    for color in fills:
        if color.lower() not in ("#ebeff0", "#000000"):
            return color
    return "#808080"


def _render_png(card):
    """Rasterize the actual SVG via svglib+reportlab; returns a temp PNG path."""
    try:
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM
    except ImportError:
        return None
    try:
        fd, tmp = tempfile.mkstemp(prefix="clearance_", suffix=".png")
        os.close(fd)
        drawing = svg2rlg(card["svg"])
        renderPM.drawToFile(drawing, tmp, fmt="PNG", dpi=600)
        return tmp
    except Exception as exc:
        logging.debug(f"svglib render failed: {exc}")
        return None


class ClearanceWindow:
    def __init__(self, root=None):
        self.owns_root = root is None
        self.root = root or tk.Tk()
        self.root.title("Clearance Card Desk")
        self._photo = None
        self._fallback = None

        self.image_frame = tk.Frame(self.root)
        self.image_frame.pack(padx=16, pady=(16, 4), expand=True, fill="both")

        self.name_label = tk.Label(self.root, font=("Segoe UI", 14, "bold"))
        self.name_label.pack()

        self.badge_label = tk.Label(self.root, font=("Segoe UI", 10))
        self.badge_label.pack()

        self.status_label = tk.Label(self.root, fg="#444444", wraplength=400)
        self.status_label.pack(pady=(4, 8))

        scan_frame = tk.Frame(self.root)
        scan_frame.pack(fill="x", padx=16, pady=(0, 16))
        tk.Label(scan_frame, text="Card number / username / room:").pack(side="left")
        self.scan_entry = tk.Entry(scan_frame)
        self.scan_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.scan_entry.bind("<Return>", lambda _e: self._on_scan())
        self.scan_entry.focus_set()

        self._show_placeholder()

    def _show_placeholder(self):
        self._set_image_widget(tk.Label(self.image_frame, text="(no card)",
                                        width=48, height=11))
        self.name_label.configure(text="Scan a card")
        self.badge_label.configure(text="")
        self.status_label.configure(text="Enter a username or room number below.")

    def _set_image_widget(self, widget):
        for child in self.image_frame.winfo_children():
            if child is not widget:
                child.destroy()
        self._image_widget = widget
        widget.pack(expand=True, fill="both", anchor="center")

    def _draw_fallback(self, card):
        W, H = CARD_W * 2, CARD_H * 2
        ox, oy = CARD_W // 2, CARD_H // 2  # offset so the card is centered
        canvas = tk.Canvas(self.image_frame, width=W, height=H,
                           bg="#d9d9d9", highlightthickness=0)
        canvas.create_rectangle(ox + 4, oy + 4, ox + CARD_W - 4, oy + CARD_H - 4,
                                fill="#ebeff0", outline="#000000")
        canvas.create_rectangle(ox + 4, oy + CARD_H * 0.45, ox + CARD_W - 4,
                                oy + CARD_H * 0.45 + 68,
                                fill=_band_color(card), outline="")
        canvas.create_text(ox + CARD_W / 2, oy + 60, text="CLEARANCE\nCARD",
                           font=("Segoe UI", 10))
        canvas.create_text(ox + CARD_W / 2, oy + CARD_H * 0.45 + 22,
                           text=card["badge"], fill="white",
                           font=("Segoe UI", 12, "bold"))
        canvas.create_text(ox + CARD_W / 2, oy + CARD_H * 0.45 + 46,
                           text=card["name"], fill="white",
                           font=("Segoe UI", 9))
        return canvas

    def show_card(self, card, subject):
        png = card["png"] if os.path.exists(card["png"]) else _render_png(card)
        shown_image = False
        if png and os.path.exists(png):
            try:
                from PIL import Image, ImageTk
                img = Image.open(png)
                self._photo = ImageTk.PhotoImage(img)
                label = tk.Label(self.image_frame, image=self._photo)
                self._set_image_widget(label)
                shown_image = True
            except Exception as exc:
                logging.debug(f"PIL render failed: {exc}")
        if not shown_image:
            self._set_image_widget(self._draw_fallback(card))
        self.name_label.configure(text=card["name"] or card["key"])
        self.badge_label.configure(text=card["badge"])
        self.status_label.configure(text=subject)

    def _on_scan(self):
        scanned = self.scan_entry.get()
        card_key, subject = clearance.lookup(scanned)
        if card_key is None:
            self.status_label.configure(text=subject)
            return
        self.show_card(clearance.get_card(card_key), subject)

    def run(self):
        if self.owns_root:
            self.root.mainloop()


def open_clearance_window():
    logging.info("keycards managed by AutoScale Lite")
    try:
        ClearanceWindow().run()
    except tk.TclError as exc:
        logging.error(f"Cannot open the clearance card window: {exc}")


if __name__ == "__main__":
    open_clearance_window()
