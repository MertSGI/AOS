from pathlib import Path

import pytest

from aos.runtime_assets import RuntimeAssetError, materialize_runtime_assets


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "src" / "aos").mkdir(parents=True)
    (root / "src" / "aos" / "__init__.py").write_text("", encoding="utf-8")
    (root / "extensions").mkdir()
    (root / "extensions" / "__init__.py").write_text("", encoding="utf-8")
    (root / "schemas" / "v0.1").mkdir(parents=True)
    (root / "schemas" / "v0.1" / "project_descriptor.schema.json").write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema"}',
        encoding="utf-8",
    )
    (root / "descriptors").mkdir()
    (root / "descriptors" / "lari.autonomous-host.descriptor.json").write_text("{}", encoding="utf-8")
    (root / "descriptors" / "lari-ui-v2.autonomous-host.descriptor.json").write_text("{}", encoding="utf-8")
    (root / "descriptors" / "nemotron.planner-policy.json").write_text("{}", encoding="utf-8")
    (root / "descriptors" / "extra.json").write_text('{"x":1}', encoding="utf-8")
    return root


def test_runtime_assets_are_self_contained_and_hashed(tmp_path: Path):
    source = _source(tmp_path)
    slot = tmp_path / "candidate" / "abc"
    result = materialize_runtime_assets(source, slot)
    assert (slot / "schemas" / "v0.1" / "project_descriptor.schema.json").is_file()
    assert (slot / "descriptors" / "lari.autonomous-host.descriptor.json").is_file()
    assert (slot / "descriptors" / "lari-ui-v2.autonomous-host.descriptor.json").is_file()
    assert (slot / "descriptors" / "nemotron.planner-policy.json").is_file()
    assert (slot / "site" / "aos" / "__init__.py").is_file()
    assert (slot / "site" / "extensions" / "__init__.py").is_file()
    assert len(result["asset_tree_sha256"]) == 64
    assert result["file_count"] == 7
    assert Path(result["manifest_path"]).is_file()


def test_runtime_assets_fail_closed_when_required_file_missing(tmp_path: Path):
    source = _source(tmp_path)
    (source / "schemas" / "v0.1" / "project_descriptor.schema.json").unlink()
    with pytest.raises(RuntimeAssetError):
        materialize_runtime_assets(source, tmp_path / "slot")
