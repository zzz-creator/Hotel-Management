"""Rasterize the clearance-card SVGs to PNGs next to the sources.

Optional: the runtime falls back to a drawn card when no PNG (and no
svglib/reportlab) is present. Run once after changing an SVG:

    python tools/convert_clearance_svgs.py

Requires: pip install svglib reportlab rlPyCairo Pillow
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hotel import clearance  # noqa: E402
from svglib.svglib import svg2rlg  # noqa: E402
from reportlab.graphics import renderPM  # noqa: E402


def main():
    count = 0
    for filename in sorted(os.listdir(clearance.ASSETS_DIR)):
        if not filename.endswith(".svg"):
            continue
        key = filename[:-4]
        out = clearance.card_png_path(key)
        drawing = svg2rlg(os.path.join(clearance.ASSETS_DIR, filename))
        renderPM.drawToFile(drawing, out, fmt="PNG", dpi=300)
        count += 1
        print(f"  {filename} -> {os.path.basename(out)}")
    print(f"Converted {count} card(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
