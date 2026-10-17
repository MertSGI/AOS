import sys
import importlib.util
from pathlib import Path

_base_dir = Path(__file__).parent
_source_root = _base_dir.parent / "src"
if _source_root.is_dir() and str(_source_root) not in sys.path:
    # Source-checkout subprocesses must not bind an unrelated installed AOS.
    sys.path.insert(0, str(_source_root))

def _load_hyphenated_pkg(pkg_name: str, folder_name: str):
    current_pkg = sys.modules.get(__name__)
    if current_pkg is not None and hasattr(current_pkg, pkg_name):
        return getattr(current_pkg, pkg_name)

    existing = sys.modules.get(f"extensions.{pkg_name}")
    if existing is not None:
        if current_pkg is not None:
            setattr(current_pkg, pkg_name, existing)
        return existing

    folder_path = _base_dir / folder_name
    init_file = folder_path / "__init__.py"
    if folder_path.exists() and init_file.exists():
        spec = importlib.util.spec_from_file_location(
            f"extensions.{pkg_name}",
            str(init_file),
            submodule_search_locations=[str(folder_path)],
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"extensions.{pkg_name}"] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(f"extensions.{pkg_name}", None)
                if current_pkg is not None and hasattr(current_pkg, pkg_name):
                    delattr(current_pkg, pkg_name)
                raise
            if current_pkg is not None:
                setattr(current_pkg, pkg_name, mod)
            return mod
    return None

_load_hyphenated_pkg("autonomy_fabric", "autonomy-fabric")
_load_hyphenated_pkg("design_intelligence", "design-intelligence")
_load_hyphenated_pkg("model_fabric", "model-fabric")

__all__ = ["autonomy_fabric", "design_intelligence", "model_fabric"]
