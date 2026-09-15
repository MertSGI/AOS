"""Immutable non-code asset bundling for AOS Runtime V1 candidate slots."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict

REQUIRED_FILES = (
    "schemas/v0.1/project_descriptor.schema.json",
    "descriptors/lari.autonomous-host.descriptor.json",
    "descriptors/nemotron.planner-policy.json",
)
ASSET_DIRS = ("schemas", "descriptors")


class RuntimeAssetError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def materialize_runtime_assets(source_root: Path, slot_root: Path) -> Dict[str, Any]:
    source_root = source_root.expanduser().resolve()
    slot_root = slot_root.expanduser().resolve()
    if source_root == slot_root:
        raise RuntimeAssetError("source_root and slot_root must differ")

    copied = []
    for dirname in ASSET_DIRS:
        source = source_root / dirname
        target = slot_root / dirname
        if not source.is_dir():
            raise RuntimeAssetError(f"Required runtime asset directory missing: {source}")
        for node in source.rglob("*"):
            if node.is_symlink():
                raise RuntimeAssetError(f"Runtime asset symlink is not allowed: {node}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        copied.append(dirname)

    missing = [rel for rel in REQUIRED_FILES if not (slot_root / rel).is_file()]
    if missing:
        raise RuntimeAssetError(f"Required runtime assets missing after materialization: {missing}")

    files: Dict[str, str] = {}
    for dirname in ASSET_DIRS:
        for path in sorted((slot_root / dirname).rglob("*")):
            if path.is_file():
                rel = path.relative_to(slot_root).as_posix()
                files[rel] = _sha256(path)

    digest = hashlib.sha256()
    for rel, file_sha in sorted(files.items()):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\n")
    tree_sha = digest.hexdigest()

    manifest = {
        "schema_version": "1.0.0",
        "asset_directories": copied,
        "required_files": list(REQUIRED_FILES),
        "file_count": len(files),
        "files": files,
        "asset_tree_sha256": tree_sha,
    }
    manifest_path = slot_root / "runtime-assets-manifest.json"
    _atomic_json(manifest_path, manifest)
    return {
        "slot_root": str(slot_root),
        "schemas_root": str(slot_root / "schemas"),
        "descriptors_root": str(slot_root / "descriptors"),
        "manifest_path": str(manifest_path),
        "asset_tree_sha256": tree_sha,
        "file_count": len(files),
    }
