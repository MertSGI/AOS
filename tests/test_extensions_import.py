"""Tests for extensions package import behavior and module attribute binding."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
import pytest


def test_import_extensions_autonomy_fabric_direct():
    import extensions.autonomy_fabric
    assert extensions.autonomy_fabric is not None
    assert "extensions.autonomy_fabric" in sys.modules


def test_import_extensions_attribute_access():
    import extensions
    assert hasattr(extensions, "autonomy_fabric")
    assert hasattr(extensions, "design_intelligence")
    assert hasattr(extensions, "model_fabric")
    assert extensions.autonomy_fabric is sys.modules["extensions.autonomy_fabric"]


def test_from_extensions_autonomy_fabric_import():
    from extensions.autonomy_fabric.execution_backend import ExecutionCapability
    assert ExecutionCapability.PROCESS_EXEC.value == "PROCESS_EXEC"


def test_repeated_and_reloaded_imports():
    import extensions
    first_mod = extensions.autonomy_fabric
    reloaded = importlib.reload(extensions)
    assert reloaded.autonomy_fabric is first_mod
    assert hasattr(reloaded, "autonomy_fabric")
    assert hasattr(reloaded, "design_intelligence")
    assert hasattr(reloaded, "model_fabric")


def test_isolated_runtime_asset_layout(tmp_path):
    """Simulate candidate slot layout (site/extensions) with hyphenated subpackages."""
    site_dir = tmp_path / "site"
    ext_dir = site_dir / "extensions"
    ext_dir.mkdir(parents=True)
    
    # Copy extensions/__init__.py
    source_init = Path(__file__).resolve().parent.parent / "extensions" / "__init__.py"
    (ext_dir / "__init__.py").write_text(source_init.read_text(encoding="utf-8"), encoding="utf-8")
    
    # Create fake hyphenated subpackage
    fake_sub = ext_dir / "autonomy-fabric"
    fake_sub.mkdir()
    (fake_sub / "__init__.py").write_text("IS_MOCK = True\n", encoding="utf-8")
    
    # Run in subprocess to test completely fresh isolated import
    import subprocess
    cmd = [
        sys.executable,
        "-c",
        (
            f"import sys; sys.path.insert(0, r'{site_dir}'); "
            "import extensions; "
            "assert hasattr(extensions, 'autonomy_fabric'); "
            "assert getattr(extensions.autonomy_fabric, 'IS_MOCK', False) is True; "
            "print('ISOLATED_OK')"
        )
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "ISOLATED_OK" in res.stdout


def test_failure_behavior_without_partial_module_leaks(tmp_path, monkeypatch):
    """If a subpackage fails to load during exec_module, no partial module is leaked in sys.modules."""
    site_dir = tmp_path / "site_err"
    ext_dir = site_dir / "extensions"
    ext_dir.mkdir(parents=True)
    
    source_init = Path(__file__).resolve().parent.parent / "extensions" / "__init__.py"
    (ext_dir / "__init__.py").write_text(source_init.read_text(encoding="utf-8"), encoding="utf-8")
    
    bad_sub = ext_dir / "autonomy-fabric"
    bad_sub.mkdir()
    (bad_sub / "__init__.py").write_text("raise RuntimeError('Intentional load syntax/init failure')\n", encoding="utf-8")
    
    import subprocess
    cmd = [
        sys.executable,
        "-c",
        (
            f"import sys\n"
            f"sys.path.insert(0, r'{site_dir}')\n"
            "try:\n"
            "    import extensions\n"
            "except RuntimeError as e:\n"
            "    assert 'Intentional load syntax/init failure' in str(e)\n"
            "    assert 'extensions.autonomy_fabric' not in sys.modules, 'Partial module leaked in sys.modules!'\n"
            "    print('LEAK_PREVENTED_OK')\n"
        )
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "LEAK_PREVENTED_OK" in res.stdout
