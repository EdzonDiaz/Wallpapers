#!/usr/bin/env python3
"""Sync Preview thumbnails and Walls.json for Blueprint/Frames dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote

try:
    from PIL import Image, ImageOps

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PREVIEW_DIR_NAME = "Preview"
JSON_FILE_NAME = "Walls.json"
PREVIEW_MAX_WIDTH = 600
PREVIEW_JPEG_QUALITY = 82


def natural_sort_key(text: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def list_root_wallpapers(repo_root: Path) -> list[Path]:
    wallpapers: list[Path] = []
    for item in repo_root.iterdir():
        if not item.is_file() or item.name.startswith("."):
            continue
        if item.suffix.lower() in IMAGE_EXTENSIONS:
            wallpapers.append(item)
    return sorted(wallpapers, key=lambda p: natural_sort_key(p.name), reverse=True)


def preview_name_for(wallpaper: Path) -> str:
    return f"{wallpaper.stem}.jpg"


def generate_preview_pillow(src: Path, dst: Path) -> None:
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (0, 0, 0))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        if width > PREVIEW_MAX_WIDTH:
            new_height = max(1, round(height * (PREVIEW_MAX_WIDTH / width)))
            img = img.resize((PREVIEW_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

        img.save(dst, format="JPEG", quality=PREVIEW_JPEG_QUALITY, optimize=True, progressive=True)


def generate_preview_sips(src: Path, dst: Path) -> None:
    sips_bin = shutil.which("sips")
    if not sips_bin:
        raise RuntimeError("Neither Pillow nor macOS 'sips' is available to generate previews.")

    probe = subprocess.run(
        [sips_bin, "-g", "pixelWidth", str(src)],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r"pixelWidth:\s*(\d+)", probe.stdout)
    width = int(match.group(1)) if match else PREVIEW_MAX_WIDTH

    cmd = [sips_bin, "-s", "format", "jpeg", "-s", "formatOptions", str(PREVIEW_JPEG_QUALITY)]
    if width > PREVIEW_MAX_WIDTH:
        cmd.extend(["--resampleWidth", str(PREVIEW_MAX_WIDTH)])
    cmd.extend([str(src), "--out", str(dst)])
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def generate_preview(src: Path, dst: Path) -> None:
    if HAS_PILLOW:
        generate_preview_pillow(src, dst)
    else:
        generate_preview_sips(src, dst)


def load_existing_entries(json_path: Path) -> list[dict]:
    if not json_path.exists():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def extract_filename_from_url(url: str) -> str:
    if not url:
        return ""
    return unquote(url.rstrip("/").rsplit("/", 1)[-1])


def sync(repo_root: Path, repo_slug: str, branch: str, force_previews: bool) -> None:
    preview_dir = repo_root / PREVIEW_DIR_NAME
    preview_dir.mkdir(exist_ok=True)
    json_path = repo_root / JSON_FILE_NAME

    wallpapers = list_root_wallpapers(repo_root)
    wallpapers_by_name = {w.name: w for w in wallpapers}
    expected_previews: set[str] = set()

    # 1. Generate missing (or forced) previews
    created_previews = 0
    for wall in wallpapers:
        p_name = preview_name_for(wall)
        expected_previews.add(p_name)
        p_path = preview_dir / p_name
        if force_previews or not p_path.exists():
            generate_preview(wall, p_path)
            created_previews += 1

    # 2. Remove orphaned previews
    removed_previews = 0
    for item in preview_dir.iterdir():
        if not item.is_file() or item.name.startswith("."):
            continue
        if item.name not in expected_previews:
            item.unlink()
            removed_previews += 1

    # 3. Rebuild Walls.json preserving existing order & custom metadata
    base_url = f"https://raw.githubusercontent.com/{repo_slug}/{branch}"
    existing_entries = load_existing_entries(json_path)

    ordered_existing: list[tuple[str, dict]] = []
    seen_names: set[str] = set()

    for entry in existing_entries:
        if not isinstance(entry, dict):
            continue
        filename = extract_filename_from_url(str(entry.get("url", "")))
        if filename in wallpapers_by_name and filename not in seen_names:
            seen_names.add(filename)
            ordered_existing.append((filename, entry))

    new_wallpapers = [w.name for w in wallpapers if w.name not in seen_names]

    def build_entry(filename: str, previous: dict | None = None) -> dict:
        wall = wallpapers_by_name[filename]
        p_name = preview_name_for(wall)
        result: dict = {}
        result["name"] = previous.get("name", "") if previous else ""
        result["author"] = previous.get("author", "") if previous else ""
        if previous and "collections" in previous:
            result["collections"] = previous["collections"]
        result["url"] = f"{base_url}/{quote(filename)}"
        result["thumbnail"] = f"{base_url}/{PREVIEW_DIR_NAME}/{quote(p_name)}"
        result["downloadable"] = previous.get("downloadable", True) if previous else True
        if previous:
            for key, value in previous.items():
                if key not in result:
                    result[key] = value
        return result

    final_entries = [build_entry(name) for name in new_wallpapers] + [
        build_entry(name, prev) for name, prev in ordered_existing
    ]

    new_json_content = json.dumps(final_entries, indent=4, ensure_ascii=False) + "\n"
    if not json_path.exists() or json_path.read_text(encoding="utf-8") != new_json_content:
        json_path.write_text(new_json_content, encoding="utf-8")

    print(
        f"Synced {len(final_entries)} wallpapers "
        f"(new: {len(new_wallpapers)}, previews generated: {created_previews}, "
        f"orphaned previews removed: {removed_previews})."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync wallpaper previews and Walls.json.")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "EdzonDiaz/Wallpapers"),
        help="GitHub repository slug (owner/repo).",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Target branch name.",
    )
    parser.add_argument(
        "--force-previews",
        action="store_true",
        help="Regenerate all preview thumbnails even if they already exist.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    sync(repo_root, args.repo, args.branch, args.force_previews)
    return 0


if __name__ == "__main__":
    sys.exit(main())
