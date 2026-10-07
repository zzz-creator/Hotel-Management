import json
import os
import re

SPRITE_JSON = "sprite.json"
SVG_EXT = ".svg"

# Characters invalid in Windows filenames
INVALID_FILENAME_CHARS = r"<>:\\|\?\*\""
INVALID_FILENAME_PATTERN = re.compile(f"[{INVALID_FILENAME_CHARS}]")


def sanitize_filename(name: str) -> str:
    """Convert a costume name into a safe filename."""
    name = name.strip()
    name = INVALID_FILENAME_PATTERN.sub("_", name)
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    return name or "unnamed"


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(base_dir, SPRITE_JSON)

    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Could not find {SPRITE_JSON} in {base_dir}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    costumes = data.get("costumes")
    if not isinstance(costumes, list):
        raise ValueError("Expected 'costumes' to be a list in sprite.json")

    renamed = []
    skipped = []

    for item in costumes:
        asset_id = item.get("assetId")
        costume_name = item.get("name")
        if not asset_id or not costume_name:
            skipped.append((asset_id, costume_name, "missing assetId or name"))
            continue

        src_filename = f"{asset_id}{SVG_EXT}"
        src_path = os.path.join(base_dir, src_filename)
        if not os.path.exists(src_path):
            skipped.append((asset_id, costume_name, "source file not found"))
            continue

        safe_name = sanitize_filename(costume_name)
        dst_filename = f"{safe_name}{SVG_EXT}"
        dst_path = os.path.join(base_dir, dst_filename)

        if os.path.exists(dst_path) and os.path.samefile(src_path, dst_path):
            skipped.append((asset_id, costume_name, "already named"))
            continue

        if os.path.exists(dst_path):
            skipped.append((asset_id, costume_name, f"target file already exists: {dst_filename}"))
            continue

        os.rename(src_path, dst_path)
        renamed.append((src_filename, dst_filename))

    print(f"Renamed {len(renamed)} file(s).")
    for src, dst in renamed:
        print(f"  {src} -> {dst}")
    if skipped:
        print(f"Skipped {len(skipped)} item(s):")
        for asset_id, costume_name, reason in skipped:
            print(f"  {asset_id} -> {costume_name}: {reason}")


if __name__ == "__main__":
    main()
