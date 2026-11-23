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
PREVIEW_WEBP_QUALITY = 80


def natural_sort_key(text: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def sanitize_wallpaper_filenames(repo_root: Path) -> int:
    renamed = 0
    for item in sorted(repo_root.iterdir(), key=lambda p: p.name):
        if not item.is_file() or item.name.startswith("."):
            continue
        if item.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if " " not in item.name:
            continue
        clean_stem = re.sub(r"\s+", "_", item.stem).strip("_")
        target = item.with_name(f"{clean_stem}{item.suffix.lower()}")
        if not target.exists():
            item.rename(target)
            renamed += 1
    return renamed


def list_root_wallpapers(repo_root: Path) -> list[Path]:
    wallpapers: list[Path] = []
    for item in repo_root.iterdir():
        if not item.is_file() or item.name.startswith("."):
            continue
        if item.suffix.lower() in IMAGE_EXTENSIONS:
            wallpapers.append(item)
    return sorted(wallpapers, key=lambda p: natural_sort_key(p.name), reverse=True)


def preview_name_for(wallpaper: Path) -> str:
    return f"{wallpaper.stem}.webp"


def generate_preview_pillow(src: Path, dst: Path) -> None:
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

        width, height = img.size
        if width > PREVIEW_MAX_WIDTH:
            new_height = max(1, round(height * (PREVIEW_MAX_WIDTH / width)))
            img = img.resize((PREVIEW_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

        img.save(dst, format="WEBP", quality=PREVIEW_WEBP_QUALITY, method=6)


def generate_preview_ffmpeg(src: Path, dst: Path) -> None:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("Neither Pillow nor 'ffmpeg' is available to generate WebP previews.")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        f"scale='min({PREVIEW_MAX_WIDTH},iw)':-2",
        "-c:v",
        "libwebp",
        "-quality",
        str(PREVIEW_WEBP_QUALITY),
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def generate_preview(src: Path, dst: Path) -> None:
    if HAS_PILLOW:
        generate_preview_pillow(src, dst)
    else:
        generate_preview_ffmpeg(src, dst)


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

    renamed_count = sanitize_wallpaper_filenames(repo_root)
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
        elif " " in filename:
            stem, ext = os.path.splitext(filename)
            clean_filename = f"{re.sub(r'\\s+', '_', stem).strip('_')}{ext.lower()}"
            if clean_filename in wallpapers_by_name and clean_filename not in seen_names:
                seen_names.add(clean_filename)
                ordered_existing.append((clean_filename, entry))

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
        f"(new: {len(new_wallpapers)}, renamed: {renamed_count}, "
        f"previews generated: {created_previews}, orphaned previews removed: {removed_previews})."
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
