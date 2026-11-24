"""The release script stamps the manifest from a tag."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "stamp_version.py"
stamp = runpy.run_path(str(SCRIPT))["stamp"]


@pytest.mark.parametrize(
    ("tag", "version"), [("v0.0.2", "0.0.2"), ("1.2.3", "1.2.3"), ("v1.0.0-rc.1", "1.0.0-rc.1")]
)
def test_stamps_version(tmp_path: Path, tag: str, version: str) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"domain": "omnifocus", "version": "0.0.0"}))
    assert stamp(tag, manifest) == version
    data = json.loads(manifest.read_text())
    assert data == {"domain": "omnifocus", "version": version}


@pytest.mark.parametrize("tag", ["latest", "v1", "v1.2", "release-1", ""])
def test_rejects_bad_tags(tmp_path: Path, tag: str) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    with pytest.raises(SystemExit):
        stamp(tag, manifest)
    assert manifest.read_text() == "{}"
