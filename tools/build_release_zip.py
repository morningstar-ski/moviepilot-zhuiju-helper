from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


IGNORED_PARTS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
REQUIRED_ROOT_FILES = {"__init__.py", "logic.py", "state.py"}
FORBIDDEN_PREFIXES = ("plugins.", "icons/", "package.", ".git/")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_plugin_metadata(workspace: Path) -> tuple[str, dict]:
    payload = json.loads(read_text(workspace / "package.v2.json"))
    if not isinstance(payload, dict) or not payload:
        raise SystemExit("package.v2.json is empty or invalid")
    plugin_id, plugin_meta = next(iter(payload.items()))
    if not isinstance(plugin_meta, dict):
        raise SystemExit(f"package.v2.json entry for {plugin_id} is invalid")
    return plugin_id, plugin_meta


def expected_release_asset_name(plugin_id: str, version: str) -> str:
    return f"{plugin_id.lower()}_v{version}.zip"


def iter_plugin_files(plugin_dir: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(plugin_dir)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        files.append((path, relative.as_posix()))
    return files


def validate_release_zip(zip_path: Path, *, expected_icon: str) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    if not names:
        raise SystemExit(f"release zip is empty: {zip_path}")
    if len(set(names)) != len(names):
        raise SystemExit(f"release zip contains duplicate entries: {zip_path}")

    missing_root = sorted(name for name in REQUIRED_ROOT_FILES if name not in names)
    if missing_root:
        raise SystemExit(f"release zip missing required root files: {missing_root}")
    if expected_icon not in names:
        raise SystemExit(f"release zip missing icon file: {expected_icon}")

    forbidden = [
        name
        for name in names
        if name.startswith(FORBIDDEN_PREFIXES)
        or name.endswith("/__pycache__")
        or "/__pycache__/" in name
        or name.endswith(".pyc")
        or name.endswith(".pyo")
    ]
    if forbidden:
        raise SystemExit(f"release zip contains forbidden entries: {forbidden}")

    return names


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release_zip(workspace: Path, output_dir: Path) -> tuple[Path, list[str], str]:
    plugin_id, plugin_meta = load_plugin_metadata(workspace)
    plugin_dir = workspace / "plugins.v2" / plugin_id.lower()
    if not plugin_dir.is_dir():
        raise SystemExit(f"plugin directory not found: {plugin_dir}")

    icon_name = str(plugin_meta.get("icon") or "").strip()
    if not icon_name:
        raise SystemExit("package.v2.json missing icon")
    icon_path = workspace / "icons" / icon_name
    if not icon_path.is_file():
        raise SystemExit(f"icon file not found: {icon_path}")

    version = str(plugin_meta.get("version") or "").strip()
    if not version:
        raise SystemExit("package.v2.json missing version")

    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / expected_release_asset_name(plugin_id, version)

    added_names: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for source_path, arcname in iter_plugin_files(plugin_dir):
            zf.write(source_path, arcname)
            added_names.add(arcname)
        if icon_name not in added_names:
            zf.write(icon_path, icon_name)

    names = validate_release_zip(zip_path, expected_icon=icon_name)
    return zip_path, names, sha256_file(zip_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp") / "nextreleasetracker-release")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    output_dir = args.output_dir.resolve()

    zip_path, names, digest = build_release_zip(workspace, output_dir)
    print(f"[ok] release zip: {zip_path}")
    print(f"[ok] entries: {len(names)}")
    for name in names:
        print(f" - {name}")
    print(f"[ok] sha256: {digest}")


if __name__ == "__main__":
    main()
