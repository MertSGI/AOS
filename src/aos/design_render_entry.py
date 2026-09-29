"""Project-aware render entry discovery for Design Intelligence."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RenderEntry:
    entrypoint: Path
    stylesheet: Optional[Path]
    source: str
    contract_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        return {key: str(item) if isinstance(item, Path) else item for key, item in value.items()}


def _under_workspace(workspace: Path, value: str) -> Path:
    candidate = (workspace / value).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError("Render entry escapes the project workspace")
    return candidate


def _looks_like_dynamic_application(root: Path, entry: Path) -> bool:
    """Return true when a discovered HTML shell requires an application server.

    Bounded static discovery is intentionally conservative.  A Vite project
    commonly has an ``index.html`` file, but that file is only a module-loading
    shell and opening it through ``file://`` is not proof that the application
    rendered.
    """
    html = entry.read_text(encoding="utf-8", errors="replace")
    module_sources = re.findall(
        r"<script\b[^>]*\btype\s*=\s*['\"]module['\"][^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]",
        html,
        flags=re.IGNORECASE,
    )
    package_path = root / "package.json"
    package: Dict[str, Any] = {}
    if package_path.is_file():
        try:
            value = json.loads(package_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                package = value
        except (OSError, json.JSONDecodeError):
            package = {}

    dependencies: Dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    uses_vite = "vite" in dependencies or any(
        re.search(r"(^|\s)vite(?:\s|$)", str(command))
        for command in scripts.values()
    )
    source_module = any(
        source.startswith("/")
        or Path(source.split("?", 1)[0]).suffix.lower() in {".ts", ".tsx", ".jsx"}
        for source in module_sources
    )
    return bool(module_sources and (uses_vite or source_module))


def resolve_render_entry(workspace: Path) -> RenderEntry:
    """Resolve an explicit project contract, then bounded static conventions."""
    root = workspace.expanduser().resolve()
    contract_path = root / ".aos" / "render-entry.json"
    if contract_path.is_file():
        value = json.loads(contract_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "1.0.0":
            raise ValueError("Render entry contract must use schema_version 1.0.0")
        if value.get("render_type") != "STATIC_HTML":
            raise ValueError("Only STATIC_HTML render entries are currently proven")
        entry = _under_workspace(root, str(value.get("entrypoint") or ""))
        if not entry.is_file() or entry.suffix.lower() not in {".html", ".htm"}:
            raise ValueError(f"Render entrypoint is unavailable: {entry}")
        stylesheet = None
        if value.get("stylesheet"):
            stylesheet = _under_workspace(root, str(value["stylesheet"]))
            if not stylesheet.is_file():
                raise ValueError(f"Render stylesheet is unavailable: {stylesheet}")
        return RenderEntry(entry, stylesheet, "PROJECT_CONTRACT", contract_path)

    for relative in ("index.html", "public/index.html", "dist/index.html", "build/index.html"):
        entry = root / relative
        if not entry.is_file():
            continue
        if _looks_like_dynamic_application(root, entry):
            raise ValueError(
                "Dynamic application render requires bounded loopback HTTP evidence; "
                "file:// capture is not proven"
            )
        css_candidates = (
            entry.with_name("index.css"),
            entry.with_name("styles.css"),
            root / "index.css",
        )
        stylesheet = next((path for path in css_candidates if path.is_file()), None)
        return RenderEntry(entry.resolve(), stylesheet.resolve() if stylesheet else None, "BOUNDED_STATIC_DISCOVERY")
    raise FileNotFoundError(
        "No proven UI render entry. Add .aos/render-entry.json or a supported static entrypoint."
    )
