from __future__ import annotations

import json
from pathlib import Path

import pytest

from selayer import cli
from selayer.catalog import SemanticLayer
from selayer.cli import main
from selayer.cli import main as unified_main
from selayer.okf import OkfBundle
from selayer.okf.cli import main as legacy_main

#: Authored Reference document and overlay mirrored from the composition suite.
_AUTHORED_REFERENCE = (
    "---\ntype: Reference\ntitle: Guide\nstatus: stable\n---\n\n"
    "# Guidance\nText.\n"
)
_AUTHORED_OVERLAY = (
    "---\nselayer_id: metric.gross_margin\n---\n\n"
    "# Usage Guidance\nUse at item grain.\n\n"
    "# Caveats\nDo not mix grains.\n"
)


@pytest.fixture
def generated_bundle(valid_catalog_path: Path, tmp_path: Path) -> Path:
    """A generated OKF bundle on disk (no stdout) for parity comparisons."""
    layer = SemanticLayer.load(valid_catalog_path)
    destination = tmp_path / "knowledge"
    OkfBundle.generate(layer, destination)
    return destination


@pytest.fixture
def authored_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Write the valid Reference and overlay inputs, returning their roots."""
    references = tmp_path / "references"
    references.mkdir()
    (references / "guide.md").write_text(_AUTHORED_REFERENCE, encoding="utf-8")
    overlays = tmp_path / "overlays" / "metrics"
    overlays.mkdir(parents=True)
    (overlays / "gross_margin.md").write_text(_AUTHORED_OVERLAY, encoding="utf-8")
    return references, tmp_path / "overlays"


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


@pytest.mark.parametrize(
    "exception",
    [
        KeyError("programmer mistake"),
        IndexError("programmer mistake"),
        ValueError("programmer mistake"),
        TypeError("programmer mistake"),
    ],
)
def test_unexpected_programmer_errors_propagate(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
) -> None:
    """Programmer errors escaping validate_catalog must not be masked.

    The catch clause must be narrow (``OSError`` only): ``validate_catalog``
    already adapts the only legitimate ``ValueError`` subclass
    (``CatalogValidationError``) into a failed report, so any ``ValueError`` or
    ``LookupError`` (``KeyError``/``IndexError``) — and likewise ``TypeError`` —
    reaching the CLI is a genuine bug that must propagate rather than be turned
    into the secret-safe failure payload.
    """

    def boom(_catalog: object) -> None:
        raise exception

    monkeypatch.setattr(cli, "validate_catalog", boom)
    with pytest.raises(type(exception)):
        main(["catalog", "validate", "irrelevant.yaml"])


def test_unified_and_legacy_okf_validate_match(
    generated_bundle: Path, capsys,  # type: ignore[no-untyped-def]
) -> None:
    assert unified_main(["okf", "validate", str(generated_bundle)]) == 0
    unified = capsys.readouterr()
    assert legacy_main(["validate", str(generated_bundle)]) == 0
    legacy = capsys.readouterr()
    assert unified.out == legacy.out
    assert unified.err == legacy.err


def test_okf_build_accepts_reference_and_overlay_directories(
    valid_catalog_path: Path,
    authored_inputs: tuple[Path, Path],
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    references, overlays = authored_inputs
    output = tmp_path / "knowledge"
    assert unified_main(
        [
            "okf",
            "build",
            str(valid_catalog_path),
            str(output),
            "--references",
            str(references),
            "--overlays",
            str(overlays),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "build"
    # Deterministic concept and diagnostic counts are reported.
    assert payload["concepts"] >= 1
    assert payload["diagnostics"] == 0
