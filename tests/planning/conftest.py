from pathlib import Path

import pytest

from tests.next.conftest import VALID_CATALOG_YAML


@pytest.fixture
def valid_catalog_path(tmp_path: Path) -> Path:
    path = tmp_path / "layer.yaml"
    path.write_text(VALID_CATALOG_YAML, encoding="utf-8")
    return path
