from __future__ import annotations

import json
from pathlib import Path

from selayer.cli import main


def test_project_registers_unified_console_script(root: Path) -> None:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'selayer = "selayer.cli:run"' in pyproject


def test_catalog_validate_emits_report(
    valid_catalog_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    assert main(["catalog", "validate", str(valid_catalog_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["passed"] is True


def test_invalid_catalog_exits_one(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n", encoding="utf-8")
    assert main(["catalog", "validate", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False


def test_missing_catalog_emits_secret_safe_json_failure(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    assert main(["catalog", "validate", str(missing)]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error"] == "could not read or validate catalog"
    # The secret-safe message must never echo the path or a traceback.
    assert str(missing) not in captured.err
    assert str(missing) not in captured.out
    assert "Traceback" not in captured.err
    # A report is never produced for an unreadable catalog.
    assert captured.out == ""
