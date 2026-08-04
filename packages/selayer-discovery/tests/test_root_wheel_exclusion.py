"""Guard that the root ``selayer`` wheel never ships the discovery member.

The guarantee is structural: hatchling only packages the paths listed in
``[tool.hatch.build.targets.wheel].packages`` of the root ``pyproject.toml``.
This test parses that config (no build step) so a packaging edit that would
include ``selayer_discovery`` in the root wheel fails CI immediately.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def test_root_wheel_excludes_discovery_member() -> None:
    config = tomllib.loads(_ROOT_PYPROJECT.read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["src/selayer"]
