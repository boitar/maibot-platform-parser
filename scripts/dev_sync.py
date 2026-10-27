"""Copy the local custom branch into an existing MaiBot installation."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
PACKAGE_FILES = (
    "plugin.py", "sender.py", "_manifest.json", "README.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "config.toml.example",
)
MARKER = ".local-development.json"


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(SOURCE), *args], text=True).strip()


def source_snapshot() -> dict[str, bytes]:
    paths = [SOURCE / name for name in PACKAGE_FILES]
    paths.extend(p for p in (SOURCE / "core").rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    snapshot = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(SOURCE):
            raise ValueError(f"Source file is a symlink or outside the repository: {path}")
        if path.suffix in {".pyc", ".pyo"}:
            continue
        data = path.read_bytes()
        if path.suffix == ".py":
            ast.parse(data, filename=str(path))
        snapshot[path.relative_to(SOURCE).as_posix()] = data
    return snapshot


def sync(maibot_dir: Path, *, apply: bool) -> dict:
    branch = git("branch", "--show-current")
    if branch != "custom":
        raise ValueError(f"Local debugging requires custom; current branch: {branch or 'detached HEAD'}")
    snapshot = source_snapshot()
    manifest = json.loads(snapshot["_manifest.json"])
    plugin_id = manifest["id"]
    plugins = maibot_dir.resolve() / "plugins"
    candidates = []
    for path in plugins.iterdir():
        manifest_path = path / "_manifest.json"
        if not path.name.startswith(".") and path.is_dir() and manifest_path.is_file():
            if json.loads(manifest_path.read_text(encoding="utf-8")).get("id") == plugin_id:
                candidates.append(path)
    if len(candidates) != 1:
        raise ValueError(f"Expected one installed {plugin_id}; found {len(candidates)}")
    target = candidates[0]
    if target.is_symlink() or target.resolve().parent != plugins:
        raise ValueError("Installed plugin must be a real directory inside MaiBot/plugins")
    config = target / "config.toml"
    if not config.is_file() or config.is_symlink():
        raise ValueError("Installed plugin must have a regular config.toml")
    original_config = config.read_bytes()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()}
    changed = [name for name, data in snapshot.items() if not (target / name).is_file() or (target / name).read_bytes() != data]
    previous = json.loads((target / MARKER).read_text()) if (target / MARKER).is_file() else {}
    removed = sorted(set(previous.get("files", {})) - set(snapshot))
    result = {"source": str(SOURCE), "branch": branch, "commit": git("rev-parse", "HEAD"),
              "target": str(target), "changed": changed, "removed": removed, "applied": False}
    if not apply or (not changed and not removed and previous.get("files") == hashes):
        return result
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = maibot_dir.resolve() / "data" / "local_plugin_backups" / plugin_id / timestamp
    backup.mkdir(parents=True, mode=0o700)
    staging_root = plugins / ".update_tmp"
    staging_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="local-parser-", dir=staging_root) as temporary:
        candidate = Path(temporary) / "plugin"
        shutil.copytree(target, candidate, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo"))
        # Runtime code belongs to this checkout; retain config, backups and other user files.
        shutil.rmtree(candidate / "core")
        for name, data in snapshot.items():
            dest = candidate / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink():
                raise ValueError(f"Refusing to replace a symlink: {name}")
            dest.write_bytes(data)
        for name in removed:
            path = candidate / name
            if not path.resolve().is_relative_to(candidate.resolve()):
                raise ValueError(f"Invalid previous deployment path: {name}")
            if path.is_file():
                path.unlink()
        receipt = {**result, "files": hashes, "deployed_at": timestamp,
                   "dirty": git("status", "--porcelain"), "backup": str(backup / "installed")}
        (candidate / MARKER).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if config.read_bytes() != original_config or (candidate / "config.toml").read_bytes() != original_config:
            raise RuntimeError("Configuration changed during staging; retry after the save completes")
        target.rename(backup / "installed")
        try:
            candidate.rename(target)
            # Directory renames alone may not report a .py event to MaiBot's watcher.
            (target / "sender.py").touch()
        except BaseException:
            if not target.exists():
                (backup / "installed").rename(target)
            raise
    result.update(applied=True, backup=str(backup / "installed"), files=hashes)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maibot-dir", required=True, type=Path, help="Existing MaiBot instance directory")
    parser.add_argument("--apply", action="store_true", help="Deploy; default is preview only")
    args = parser.parse_args()
    print(json.dumps(sync(args.maibot_dir, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
