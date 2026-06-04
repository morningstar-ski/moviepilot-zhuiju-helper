from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from build_release_zip import build_release_zip


REQUIRED_PLUGIN_METHODS = {
    "init_plugin",
    "get_state",
    "get_api",
    "get_form",
    "get_page",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compile_python_sources(workspace: Path) -> None:
    for relative in [
        Path("plugins.v2/nextreleasetracker/__init__.py"),
        Path("plugins.v2/nextreleasetracker/logic.py"),
        Path("plugins.v2/nextreleasetracker/state.py"),
        Path("tests/test_nextreleasetracker_logic.py"),
    ]:
        path = workspace / relative
        compile(read_text(path), str(path), "exec")
    print("[ok] python syntax")


def run_unit_tests(workspace: Path) -> None:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_nextreleasetracker_logic"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(result.stderr or "")
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print("[ok] unit tests")


def load_package_metadata(workspace: Path) -> dict:
    payload = json.loads(read_text(workspace / "package.v2.json"))
    plugin_meta = payload["NextReleaseTracker"]
    print("[ok] package.v2.json parsed")
    return plugin_meta


def parse_plugin_class(workspace: Path) -> tuple[str, set[str], str]:
    module = ast.parse(read_text(workspace / "plugins.v2/nextreleasetracker/__init__.py"))
    plugin_class = None
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "NextReleaseTracker":
            plugin_class = node
            break
    if plugin_class is None:
        raise SystemExit("NextReleaseTracker class not found")

    methods = {
        node.name
        for node in plugin_class.body
        if isinstance(node, ast.FunctionDef)
    }
    plugin_version = None
    for node in plugin_class.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "plugin_version":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        plugin_version = node.value.value
    if plugin_version is None:
        raise SystemExit("plugin_version not found")
    print("[ok] plugin class parsed")
    return plugin_class.name, methods, plugin_version


def check_metadata_sync(plugin_meta: dict, plugin_version: str) -> None:
    if plugin_meta["version"] != plugin_version:
        raise SystemExit(
            f"version mismatch: package.v2.json={plugin_meta['version']} plugin_version={plugin_version}"
        )
    if "nextreleasetracker.png" != plugin_meta["icon"]:
        raise SystemExit("icon mismatch in package.v2.json")
    print("[ok] metadata sync")


def check_repo_layout(workspace: Path) -> None:
    required = [
        workspace / "package.v2.json",
        workspace / "plugins.v2/nextreleasetracker/__init__.py",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"missing required files: {missing}")
    print("[ok] repo layout")


def check_release_bundle(workspace: Path, output_dir: Path) -> None:
    zip_path, names, digest = build_release_zip(workspace, output_dir)
    print(f"[ok] release zip built: {zip_path}")
    print(f"[ok] release zip entries: {len(names)}")
    print(f"[ok] release zip sha256: {digest}")


def check_host_contract(workspace: Path, moviepilot_source: Path) -> None:
    plugin_base_path = moviepilot_source / "app/plugins/__init__.py"
    helper_plugin_path = moviepilot_source / "app/helper/plugin.py"
    if not plugin_base_path.exists() or not helper_plugin_path.exists():
        raise SystemExit(f"MoviePilot source not usable: {moviepilot_source}")

    plugin_base_ast = ast.parse(read_text(plugin_base_path))
    host_base = None
    for node in plugin_base_ast.body:
        if isinstance(node, ast.ClassDef) and node.name == "_PluginBase":
            host_base = node
            break
    if host_base is None:
        raise SystemExit("_PluginBase not found in MoviePilot source")

    host_methods = {
        node.name
        for node in host_base.body
        if isinstance(node, ast.FunctionDef)
    }
    missing_methods = sorted(REQUIRED_PLUGIN_METHODS - host_methods)
    if missing_methods:
        raise SystemExit(f"MoviePilot _PluginBase missing expected methods: {missing_methods}")

    _, plugin_methods, _ = parse_plugin_class(workspace)
    missing_impl = sorted(REQUIRED_PLUGIN_METHODS - plugin_methods)
    if missing_impl:
        raise SystemExit(f"plugin missing required methods: {missing_impl}")

    helper_text = read_text(helper_plugin_path)
    if "package.{package_version}.json" not in helper_text:
        raise SystemExit("MoviePilot helper plugin loader no longer uses package.{package_version}.json")
    if "plugins.{package_version}" not in helper_text:
        raise SystemExit("MoviePilot helper plugin loader no longer uses plugins.{package_version}")
    print("[ok] host contract sample")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--moviepilot-source", type=Path, default=Path(r"C:\tmp\MoviePilot"))
    parser.add_argument("--release-output-dir", type=Path, default=Path("/tmp") / "nextreleasetracker-release")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    moviepilot_source = args.moviepilot_source.resolve()
    release_output_dir = args.release_output_dir.resolve()

    check_repo_layout(workspace)
    compile_python_sources(workspace)
    plugin_meta = load_package_metadata(workspace)
    _, _, plugin_version = parse_plugin_class(workspace)
    check_metadata_sync(plugin_meta, plugin_version)
    check_release_bundle(workspace, release_output_dir)
    check_host_contract(workspace, moviepilot_source)
    run_unit_tests(workspace)
    print("[ok] local release validation passed")


if __name__ == "__main__":
    main()
