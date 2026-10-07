"""Rasterize the clearance-card SVGs to PNGs next to the sources.

Optional: the runtime falls back to a drawn card when no PNG (and no
cairosvg) is present. Run once after changing an SVG:

    python tools/convert_clearance_svgs.py

Requires: pip install cairosvg
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import clearance  # noqa: E402


def main():
    try:
        import cairosvg
    except ImportError:
        print("cairosvg is not installed. Run: pip install cairosvg")
        return 1
    count = 0
    for filename in sorted(os.listdir(clearance.ASSETS_DIR)):
        if not filename.endswith(".svg"):
            continue
        key = filename[:-4]
        out = clearance.card_png_path(key)
        cairosvg.svg2png(url=os.path.join(clearance.ASSETS_DIR, filename),
                         write_to=out, output_width=338, output_height=189)
        count += 1
        print(f"  {filename} -> {os.path.basename(out)}")
    print(f"Converted {count} card(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
