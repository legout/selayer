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

#: A credential sentinel embedded in fake driver/IO error messages to prove the
#: unified ``okf`` envelope never leaks raw exception text to stderr.
_SECRET_SENTINEL = "AKIAIOSFODNN7EXAMPLE-TOKEN-SENTINEL-9f3a"

#: Authored Reference document and overlay mirrored from the composition suite.
_AUTHORED_REFERENCE = (
    "---\ntype: Reference\ntitle: Guide\nstatus: stable\n---\n\n# Guidance\nText.\n"
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
    generated_bundle: Path,
    capsys,  # type: ignore[no-untyped-def]
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
    assert (
        unified_main(
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
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "build"
    # Deterministic concept and diagnostic counts are reported.
    assert payload["concepts"] >= 1
    assert payload["diagnostics"] == 0


def test_unified_okf_build_envelope_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Unified ``okf build`` must not leak raw exception text to stderr.

    An ``OSError`` escaping ``OkfBundle.build`` can echo credentials,
    authenticated locations, paths, or raw driver text. The unified envelope
    must emit a fixed JSON failure with none of that, and keep exit code 1.
    """

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(
            f"parquet driver failed: token={_SECRET_SENTINEL} "
            f"at /home/runner/.aws/credentials"
        )

    monkeypatch.setattr(cli.OkfBundle, "build", boom)
    destination = tmp_path / "knowledge"
    assert (
        unified_main(["okf", "build", str(valid_catalog_path), str(destination)]) == 1
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert set(payload) == {"error"}
    assert payload["error"]  # non-empty fixed message
    # The credential/path/driver text must never reach either stream.
    assert _SECRET_SENTINEL not in captured.err
    assert _SECRET_SENTINEL not in captured.out
    assert "/home/runner/.aws/credentials" not in captured.err
    assert "Traceback" not in captured.err
    # No partial success report is produced.
    assert captured.out == ""


def test_unified_okf_shared_command_envelope_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Unified shared OKF commands must not leak raw exception text.

    A ``ValueError`` from the shared handler (e.g. an authenticated URL in a
    raw driver message) must surface only as a fixed JSON failure on stderr.
    """

    def boom(_arguments: object) -> int:
        raise ValueError(
            f"s3://access:{_SECRET_SENTINEL}@bucket.example.com/data/orders"
        )

    monkeypatch.setattr(cli, "execute_okf", boom)
    bundle = tmp_path / "knowledge"
    assert unified_main(["okf", "validate", str(bundle)]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert set(payload) == {"error"}
    assert payload["error"]  # non-empty fixed message
    assert _SECRET_SENTINEL not in captured.err
    assert _SECRET_SENTINEL not in captured.out
    assert "bucket.example.com" not in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_legacy_okf_still_echoes_exception_text(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Legacy ``selayer-okf`` preserves its ``error: <message>`` envelope.

    The unified area is hardened to a fixed JSON payload, but the legacy CLI
    keeps its original behavior (interpolating the message) and exit code 1,
    so existing scripts and the legacy error contract are unchanged.
    """
    bundle = tmp_path / "knowledge"
    bundle.mkdir()
    (bundle / "bad.md").write_text("---\ntitle: Bad\n---\n", encoding="utf-8")
    assert legacy_main(["validate", str(bundle)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    # The legacy envelope still interpolates the message (contrast with the
    # unified secret-safe JSON failure above).
    assert captured.err.startswith("error: bad.md.frontmatter.type:")


# ---------------------------------------------------------------------------
# catalog compatibility
# ---------------------------------------------------------------------------


def test_catalog_compatibility_emits_report(
    valid_catalog_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """A default compatibility run emits a passed compatibility report."""
    assert main(["catalog", "compatibility", str(valid_catalog_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["check_kind"] == "compatibility"
    assert payload["passed"] is True
    check_ids = {outcome["check_id"] for outcome in payload["outcomes"]}
    assert "compatibility.metric.gross_margin" in check_ids


def test_catalog_compatibility_with_flags_focuses_requests(
    valid_catalog_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Repeated ``--metric``/``--dimension`` flags restrict the request set."""
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--metric",
                "gross_margin",
                "--dimension",
                "product_category",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    check_ids = {outcome["check_id"] for outcome in payload["outcomes"]}
    assert "compatibility.metric_dimension.gross_margin.product_category" in check_ids


def test_catalog_compatibility_query_cases_accepted(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """A query-cases JSON file of valid objects yields explicit outcomes."""
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps([{"metrics": ["gross_margin"], "dimensions": ["product_category"]}]),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--metric",
                "gross_margin",
                "--query-cases",
                str(cases),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    check_ids = {outcome["check_id"] for outcome in payload["outcomes"]}
    assert "compatibility.explicit.0000" in check_ids


def test_catalog_compatibility_query_cases_reject_unknown_keys_secret_safe(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Unknown query-case keys are rejected without leaking file content.

    A query-cases file may carry attacker-controlled or credential-bearing
    bytes; the CLI must reject an unknown key (here ``sql``) with the fixed
    secret-safe envelope and never echo the offending value to either stream.
    """
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps([{"metrics": ["gross_margin"], "sql": "SELECT 'leak'"}]),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--query-cases",
                str(cases),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert set(payload) == {"error"}
    assert payload["error"]  # non-empty fixed message
    # The offending SQL/value and the path must never reach either stream.
    assert "SELECT 'leak'" not in captured.err
    assert "SELECT 'leak'" not in captured.out
    assert str(cases) not in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_catalog_compatibility_unknown_selector_flags_are_secret_safe(
    valid_catalog_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Unknown ``--metric``/``--dimension`` values never leak to any stream.

    Top-level selector values are user-supplied and untrusted; an unknown
    metric or dimension is a declaration failure whose indexed ``check_id``,
    diagnostic path/message, and evidence must never echo the raw name. The
    sentinel must be absent from the JSON report on stdout and from stderr.
    """
    metric_sentinel = "LEAK_METRIC_TOKEN_4f8a"
    dimension_sentinel = "LEAK_DIMENSION_TOKEN_7c2b"
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--metric",
                metric_sentinel,
                "--dimension",
                dimension_sentinel,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["passed"] is False
    check_ids = {outcome["check_id"] for outcome in payload["outcomes"]}
    assert "compatibility.declaration.metric.0000" in check_ids
    assert "compatibility.declaration.dimension.0000" in check_ids
    # The sentinels must never reach stdout (report JSON) or stderr.
    assert metric_sentinel not in captured.out
    assert dimension_sentinel not in captured.out
    assert metric_sentinel not in captured.err
    assert dimension_sentinel not in captured.err
    assert "Traceback" not in captured.err


def test_catalog_compatibility_invalid_catalog_emits_static_failure(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """An invalid catalog has no layer, so its static failure report is shown."""
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n", encoding="utf-8")
    assert main(["catalog", "compatibility", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_kind"] == "static"
    assert payload["passed"] is False


def test_catalog_compatibility_missing_catalog_is_secret_safe(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """A missing catalog surfaces only the fixed secret-safe envelope."""
    missing = tmp_path / "does_not_exist.yaml"
    assert main(["catalog", "compatibility", str(missing)]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error"] == "could not run compatibility check"
    assert str(missing) not in captured.err
    assert str(missing) not in captured.out
    assert "Traceback" not in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Review findings: query-case JSON field/shape validation (no traceback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "sentinel"),
    [
        # A bare-string selector would be character-iterated into single-letter
        # "selectors" by QueryRequest; it must be rejected as a bad shape.
        ({"metrics": "gross_margin"}, "gross_margin"),
        # A non-string selector element is a bad shape.
        ({"metrics": [123]}, "123"),
        # A present-but-null ``filters`` is rejected (only omission yields
        # ``{}``); it previously fell through ``entry.get``'s ``None`` branch.
        ({"metrics": ["gross_margin"], "filters": None}, "null"),
        # A non-mapping ``filters`` value must not reach QueryRequest.__init__
        # (which would call ``.items()`` on it and raise AttributeError).
        ({"metrics": ["gross_margin"], "filters": [1, 2, 3]}, "AttributeError"),
        # A non-mapping scalar ``filters`` value (e.g. a secret string) is
        # rejected rather than echoed.
        (
            {"metrics": ["gross_margin"], "filters": "LEAK_FILTER_TOKEN_b3e1"},
            "LEAK_FILTER_TOKEN_b3e1",
        ),
        # A filter value that is neither scalar, list, nor range is rejected.
        (
            {
                "metrics": ["gross_margin"],
                "filters": {"product_category": {"weird": 1}},
            },
            "weird",
        ),
        # A range filter missing a bound is rejected.
        (
            {
                "metrics": ["gross_margin"],
                "filters": {"product_category": {"start": "a"}},
            },
            "start",
        ),
    ],
)
def test_catalog_compatibility_query_cases_reject_bad_shape_secret_safe(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
    payload: dict[str, object],
    sentinel: str,
) -> None:
    """Malformed query-case shapes are rejected without a traceback or leak.

    Every invalid field shape is caught before QueryRequest construction, so
    the secret-safe envelope is emitted (never an AttributeError/traceback) and
    the offending bytes never reach stdout or stderr.
    """
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([payload]), encoding="utf-8")
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--query-cases",
                str(cases),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    body = json.loads(captured.err)
    assert set(body) == {"error"}
    assert body["error"]  # non-empty fixed message
    # No traceback and no echo of the offending bytes or the file path.
    assert "Traceback" not in captured.err
    assert sentinel not in captured.err
    assert sentinel not in captured.out
    assert str(cases) not in captured.err
    assert captured.out == ""


def test_catalog_compatibility_query_cases_reject_filters_null_secret_safe(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """A present ``filters: null`` is rejected, not silently treated as ``{}``.

    Only an *omitted* ``filters`` key defaults to ``{}``; an explicit ``null``
    must be rejected through the secret-safe envelope. Previously
    ``entry.get("filters")`` returned ``None`` for both omission and an
    explicit ``null``, so the request silently became unfilled and succeeded.
    A present ``null`` would also reach ``QueryRequest.__init__`` and call
    ``.items()`` on it if not caught here.
    """
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps([{"metrics": ["gross_margin"], "filters": None}]),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--query-cases",
                str(cases),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    body = json.loads(captured.err)
    assert set(body) == {"error"}
    assert body["error"]  # non-empty fixed message
    # No traceback, no echo of the file path, and no offending bytes.
    assert "Traceback" not in captured.err
    assert str(cases) not in captured.err
    assert captured.out == ""


def test_catalog_compatibility_query_cases_accept_range_filter(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """The range filter form ``{"start": s, "end": e}`` is accepted and planned."""
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "metrics": ["gross_margin"],
                    "filters": {"product_category": {"start": "a", "end": "z"}},
                }
            ]
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--metric",
                "gross_margin",
                "--query-cases",
                str(cases),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    check_ids = {outcome["check_id"] for outcome in payload["outcomes"]}
    assert "compatibility.explicit.0000" in check_ids


@pytest.mark.parametrize("dimensions", [None, []])
def test_catalog_compatibility_query_cases_metric_alone_allows_no_dimensions(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
    dimensions: object,
) -> None:
    """A metric-alone case may omit dimensions or pass an empty list."""
    entry: dict[str, object] = {"metrics": ["gross_margin"]}
    if dimensions is not None:
        entry["dimensions"] = dimensions
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([entry]), encoding="utf-8")
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--metric",
                "gross_margin",
                "--query-cases",
                str(cases),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    explicit = next(
        outcome
        for outcome in payload["outcomes"]
        if outcome["check_id"] == "compatibility.explicit.0000"
    )
    assert explicit["evidence"]["compatible"] is True
    assert explicit["evidence"]["selected_dimensions"] == ""


# ---------------------------------------------------------------------------
# Re-review finding: explicit query-case metrics/dimensions presence rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "sentinel"),
    [
        # ``metrics`` is required: a missing key is rejected.
        ({"dimensions": ["LEAK_DIM_4f1a"]}, "LEAK_DIM_4f1a"),
        # ``metrics`` must not be an explicit null.
        ({"metrics": None, "dimensions": ["LEAK_DIM_4f1a"]}, "LEAK_DIM_4f1a"),
        # ``metrics`` must be a non-empty list.
        ({"metrics": [], "dimensions": ["LEAK_DIM_4f1a"]}, "LEAK_DIM_4f1a"),
        # ``metrics`` entries must be non-empty strings.
        ({"metrics": [""], "dimensions": ["LEAK_DIM_4f1a"]}, "LEAK_DIM_4f1a"),
        # ``dimensions`` may be omitted/``[]`` only: a present null is rejected.
        (
            {"metrics": ["LEAK_METRIC_7c2b"], "dimensions": None},
            "LEAK_METRIC_7c2b",
        ),
    ],
)
def test_catalog_compatibility_query_cases_reject_metrics_dimensions_presence_secret_safe(
    valid_catalog_path: Path,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
    payload: dict[str, object],
    sentinel: str,
) -> None:
    """Explicit query-case metrics/dimensions presence rules are enforced.

    ``metrics`` must be present, non-null, and a non-empty list of non-empty
    strings; ``dimensions`` may only be omitted or an empty list (a present
    null is rejected). Every rejection is masked by the secret-safe envelope:
    no offending selector/bytes reach stdout or stderr and there is no
    traceback.
    """
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([payload]), encoding="utf-8")
    assert (
        main(
            [
                "catalog",
                "compatibility",
                str(valid_catalog_path),
                "--query-cases",
                str(cases),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    body = json.loads(captured.err)
    assert set(body) == {"error"}
    assert body["error"]  # non-empty fixed message
    # No traceback and no echo of the offending selector or the file path.
    assert "Traceback" not in captured.err
    assert sentinel not in captured.err
    assert sentinel not in captured.out
    assert str(cases) not in captured.err
    assert captured.out == ""
