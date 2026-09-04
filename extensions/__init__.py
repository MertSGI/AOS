import sys
import importlib.util
from pathlib import Path

_base_dir = Path(__file__).parent

def _load_hyphenated_pkg(pkg_name: str, folder_name: str):
    folder_path = _base_dir / folder_name
    if folder_path.exists() and (folder_path / "__init__.py").exists():
        spec = importlib.util.spec_from_file_location(
            f"extensions.{pkg_name}",
            str(folder_path / "__init__.py"),
            submodule_search_locations=[str(folder_path)]
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"extensions.{pkg_name}"] = mod
            spec.loader.exec_module(mod)

_load_hyphenated_pkg("autonomy_fabric", "autonomy-fabric")
_load_hyphenated_pkg("design_intelligence", "design-intelligence")
