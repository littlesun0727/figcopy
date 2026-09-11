"""Verify that benchmark baselines bind inputs without exporting images or secrets."""

from pathlib import Path

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import atomic_write_json, read_json, sha256_file
from collage.devtools import benchmark


def test_baseline_records_exif_transform_and_omits_private_values(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "samples"
    source.mkdir()
    image = Image.new("RGB", (12, 8), "#225577")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source / "sample.jpg", exif=exif)
    catalog = tmp_path / "catalog.json"
    atomic_write_json(
        catalog,
        {
            "version": "auto-rebuild-catalog/1",
            "samples": [
                {
                    "id": "S01",
                    "filename": "sample.jpg",
                    "sha256": sha256_file(source / "sample.jpg"),
                }
            ],
        },
    )
    monkeypatch.setattr(
        benchmark, "_workspace_fingerprint", lambda root: {"head": "a" * 40}
    )
    monkeypatch.setenv("YIBU_API_KEY", "fixture-secret-do-not-export")
    output = tmp_path / "baseline.json"
    benchmark.record_baseline(source, catalog, output, repository=tmp_path)
    record = read_json(output)
    assert record["samples"][0]["source_size"] == [12, 8]
    assert record["samples"][0]["normalized_size"] == [8, 12]
    assert record["samples"][0]["exif_orientation"] == 6
    assert record["capabilities"]["credential_config_present_in_process"] is True
    assert record["capabilities"]["network_calls"] == 0
    assert record["automatic_rebuild_coverage"] is None
    exported = output.read_text(encoding="utf-8")
    assert "fixture-secret" not in exported
    assert str(tmp_path) not in exported
    assert not list(tmp_path.rglob("*.png"))
    with pytest.raises(CollageError) as caught:
        benchmark.record_baseline(source, catalog, output, repository=tmp_path)
    assert caught.value.code == "OUTPUT_EXISTS"


def test_changed_sample_cannot_reuse_old_baseline_identity(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.jpg"
    Image.new("RGB", (4, 4), "red").save(image_path)
    catalog = tmp_path / "catalog.json"
    atomic_write_json(
        catalog,
        {
            "version": "auto-rebuild-catalog/1",
            "samples": [{"id": "S01", "filename": "sample.jpg", "sha256": "0" * 64}],
        },
    )
    with pytest.raises(CollageError) as caught:
        benchmark.record_baseline(
            tmp_path, catalog, tmp_path / "baseline.json", repository=tmp_path
        )
    assert caught.value.code == "BASELINE_SOURCE_CHANGED"
    assert not (tmp_path / "baseline.json").exists()
