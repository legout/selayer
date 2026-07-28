from __future__ import annotations

import json
from pathlib import Path

import pytest

from selayer.okf.cli import main


def _invoke(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    exit_code = main(arguments)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_console_script_points_to_dependency_free_cli(root: Path) -> None:
    project = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert '[project.scripts]\nselayer-okf = "selayer.okf.cli:run"' in project


def test_generate_creates_a_bundle_and_reports_json(
    tmp_path: Path,
    valid_catalog_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "knowledge"

    exit_code, stdout, stderr = _invoke(
        ["generate", str(valid_catalog_path), str(destination)], capsys
    )

    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "command": "generate",
        "concepts": 11,
        "destination": str(destination),
    }
    assert (destination / "metrics/gross_margin.md").is_file()


def test_sync_dry_run_reports_classified_paths_without_writing(
    tmp_path: Path,
    valid_catalog_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "knowledge"
    main(["generate", str(valid_catalog_path), str(destination)])
    capsys.readouterr()

    exit_code, stdout, stderr = _invoke(
        ["sync", str(valid_catalog_path), str(destination), "--dry-run"], capsys
    )

    payload = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert payload["command"] == "sync"
    assert payload["dry_run"] is True
    assert payload["written"] == []
    assert len(payload["unchanged"]) == 11
    assert payload["conflicts"] == []
    assert payload["orphaned"] == []


def test_validate_reports_concepts_and_advisory_diagnostics(
    tmp_path: Path,
    valid_catalog_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "knowledge"
    main(["generate", str(valid_catalog_path), str(destination)])
    capsys.readouterr()

    exit_code, stdout, stderr = _invoke(
        ["validate", str(destination), "--catalog", str(valid_catalog_path)], capsys
    )

    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "command": "validate",
        "concepts": 11,
        "diagnostics": [],
    }


def test_retrieve_emits_attributed_context_as_json(
    tmp_path: Path,
    valid_catalog_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "knowledge"
    main(["generate", str(valid_catalog_path), str(destination)])
    capsys.readouterr()

    exit_code, stdout, stderr = _invoke(
        [
            "retrieve",
            str(destination),
            "metric.gross_margin",
            "--catalog",
            str(valid_catalog_path),
            "--no-linked",
            "--max-chars",
            "12000",
            "--max-depth",
            "0",
        ],
        capsys,
    )

    payload = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert payload["command"] == "retrieve"
    assert payload["total_chars"] > 0
    assert payload["diagnostics"] == [
        {
            "message": "returned context is unverified",
            "path": "metrics/gross_margin.md",
            "severity": "warning",
        }
    ]
    assert [item["semantic_refs"] for item in payload["items"]] == [
        ["metric.gross_margin"]
    ]
    assert payload["items"][0]["provider"] == "selayer"


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_validate_rejects_missing_or_non_directory_root(
    tmp_path: Path,
    root_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "bundle"
    if root_kind == "file":
        bundle.write_text("not a bundle", encoding="utf-8")

    exit_code, stdout, stderr = _invoke(["validate", str(bundle)], capsys)

    assert exit_code == 1
    assert stdout == ""
    assert stderr.startswith("error: bundle root")
    assert stderr.count("\n") == 1
    assert "Traceback" not in stderr


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_context_rejects_missing_or_non_directory_root(
    tmp_path: Path,
    root_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "bundle"
    if root_kind == "file":
        bundle.write_text("not a bundle", encoding="utf-8")

    exit_code, stdout, stderr = _invoke(
        ["retrieve", str(bundle), "dimension.missing"], capsys
    )

    assert exit_code == 1
    assert stdout == ""
    assert stderr.startswith("error: bundle root")
    assert stderr.count("\n") == 1
    assert "Traceback" not in stderr


def test_domain_errors_use_stderr_and_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "knowledge"
    bundle.mkdir()
    (bundle / "bad.md").write_text("---\ntitle: Bad\n---\n", encoding="utf-8")

    exit_code, stdout, stderr = _invoke(["validate", str(bundle)], capsys)

    assert exit_code == 1
    assert stdout == ""
    assert stderr == (
        "error: bad.md.frontmatter.type: type must be a non-empty string\n"
    )
