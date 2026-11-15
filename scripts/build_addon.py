#!/usr/bin/env python3
"""Build a deterministic Anki add-on archive from the release allowlist."""

from __future__ import annotations

import argparse
import json
import stat
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = ROOT / "anki_batch_auto_easy"
ADDON_FILES = (
    "__init__.py",
    "adapter.py",
    "config.json",
    "core.py",
    "i18n.py",
    "integration.py",
    "legacy.py",
    "manifest.json",
    "models.py",
    "state.py",
)
ARCHIVE_FILES = (*ADDON_FILES, "README.md", "LICENSE")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
        help="directory for the .ankiaddon file (default: dist/)",
    )
    args = parser.parse_args()

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manifest = json.loads((ADDON_DIR / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("package") != ADDON_DIR.name:
        raise SystemExit("manifest package does not match the add-on folder")
    if manifest.get("human_version") != version:
        raise SystemExit("VERSION and manifest human_version do not match")

    missing = [
        f"anki_batch_auto_easy/{name}"
        for name in ADDON_FILES
        if not (ADDON_DIR / name).is_file()
    ]
    missing += [name for name in ("README.md", "LICENSE") if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"release files are missing: {', '.join(missing)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"oneki-reviewer-{version}.ankiaddon"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(ARCHIVE_FILES):
            source = (ADDON_DIR / name) if name in ADDON_FILES else (ROOT / name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, source.read_bytes())

    with zipfile.ZipFile(output) as archive:
        packaged = set(archive.namelist())
        if packaged != set(ARCHIVE_FILES):
            raise SystemExit("archive contents differ from the release allowlist")
    print(output)


if __name__ == "__main__":
    main()
