#!/usr/bin/env python3
"""Write a release version into the integration's manifest.json.

Usage: stamp_version.py <tag-or-version> [manifest-path]

Accepts ``v1.2.3`` or ``1.2.3`` (optionally with a ``-rc.1`` style suffix).
The committed manifest carries a placeholder; the real version is stamped at
release time so the tag is the single source of truth.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_MANIFEST = Path("custom_components/omnifocus/manifest.json")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$")


def stamp(tag: str, manifest: Path = DEFAULT_MANIFEST) -> str:
    """Set ``version`` in ``manifest`` from ``tag`` and return the version."""
    version = tag.removeprefix("v")
    if not VERSION_RE.match(version):
        raise SystemExit(f"Not a valid release version: {tag!r}")
    data = json.loads(manifest.read_text())
    data["version"] = version
    manifest.write_text(json.dumps(data, indent=2) + "\n")
    return version


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 3:
        raise SystemExit(__doc__)
    path = Path(sys.argv[2]) if len(sys.argv) == 3 else DEFAULT_MANIFEST
    print(stamp(sys.argv[1], path))
