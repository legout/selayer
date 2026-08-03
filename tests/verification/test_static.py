"""Tests for the catalog static-validation adapter and coded ``CatalogIssue``.

``validate_catalog`` adapts the existing catalog loader into the immutable
verification report model: a successful load yields a passed static outcome
alongside the loaded layer, while a ``CatalogValidationError`` is mapped to a
failed outcome whose diagnostics carry the catalog's stable issue codes. The
``CatalogIssue`` type gains a ``code`` field while keeping its old positional
``(path, message)`` construction for backward compatibility.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from selayer import TableSchema
from selayer.catalog import CatalogIssue
from selayer.verification import StaticCheck, validate_catalog, verify


def test_catalog_issue_keeps_old_positional_construction() -> None:
    issue = CatalogIssue("metrics.margin", "unknown measure")
    assert issue.path == "metrics.margin"
    assert issue.message == "unknown measure"
    assert issue.code == "catalog.invalid"


def test_validate_catalog_returns_layer_and_passed_report(
    valid_catalog_path: Path,
) -> None:
    result = validate_catalog(valid_catalog_path)
    assert result.layer is not None
    assert result.report.passed
    assert result.report.outcomes[0].check_id == "catalog.static"


def test_validate_catalog_returns_coded_failure(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n", encoding="utf-8")
    result = validate_catalog(path)
    assert result.layer is None
    assert not result.report.passed
    assert result.report.diagnostics[0].code == "catalog.version.unsupported"


def test_validate_catalog_malformed_yaml_fails_with_safe_message(
    tmp_path: Path,
) -> None:
    """A malformed catalog yields a failed report with a safe code/message.

    The diagnostic must carry a fixed domain message rather than raw YAML
    parser output, while still failing the report on the default safe code.
    """
    path = tmp_path / "bad.yaml"
    path.write_text("version: [1\nname: ecommerce\n", encoding="utf-8")
    result = validate_catalog(path)
    assert result.layer is None
    assert not result.report.passed
    diagnostics = result.report.diagnostics
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "catalog.invalid"
    assert diagnostics[0].message == "catalog file is not valid YAML"


def test_validate_catalog_malformed_yaml_never_leaks_source_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A YAML parse error must never echo source secrets into the report.

    PyYAML reproduces the offending source line verbatim in its diagnostic, so
    a syntax error on a line carrying a credential would otherwise leak that
    credential through the report. The adapter must surface only a fixed,
    secret-safe domain message across every reachable surface, including a
    stdout/stderr serialisation path a CLI consumer would use.
    """
    secret = "XYZ-SECRET-123"
    # The flow-mapping opener after the value makes the secret-bearing line
    # itself the offending source line in PyYAML's raw (unsanitised) text.
    path = tmp_path / "leak.yaml"
    path.write_text(
        "version: 1\nname: ecommerce\ndata_sources:\n  orders:\n"
        f"    pwd: {secret} {{ a: b\n",
        encoding="utf-8",
    )

    result = validate_catalog(path)

    assert result.layer is None
    assert not result.report.passed
    diagnostics = result.report.diagnostics
    assert len(diagnostics) == 1
    # Stable, secret-safe code: the default catalog code is unchanged.
    assert diagnostics[0].code == "catalog.invalid"

    # Exercise a stdout serialisation path a CLI consumer would use, then
    # assert the secret is absent from every rendered surface.
    report_dict = result.report.to_dict()
    print(json.dumps(report_dict))
    captured = capsys.readouterr()
    surfaces = [
        diagnostics[0].message,
        repr(diagnostics[0]),
        repr(result.report),
        str(result.report),
        json.dumps(report_dict),
        captured.out,
        captured.err,
    ]
    for surface in surfaces:
        assert secret not in surface

    # The diagnostic carries a fixed domain message, not raw YAML text.
    assert diagnostics[0].message == "catalog file is not valid YAML"


# ---------------------------------------------------------------------------
# Declaration-rule parity between programmatic layers and loaded catalogs
# ---------------------------------------------------------------------------


def test_static_check_rejects_duplicate_grain(valid_layer) -> None:  # type: ignore[no-untyped-def]
    source = valid_layer.data_sources["orders"]
    bad = replace(
        valid_layer,
        data_sources={
            **valid_layer.data_sources,
            "orders": replace(source, grain=(source.grain[0], source.grain[0])),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.grain.duplicate_column" in {
        item.code for item in report.diagnostics
    }


def test_static_check_rejects_nullable_grain(valid_layer) -> None:  # type: ignore[no-untyped-def]
    source = valid_layer.data_sources["orders"]
    grain_column = source.grain[0]
    fields = tuple(
        replace(field, nullable=True) if field.name == grain_column else field
        for field in source.schema.fields
    )
    bad = replace(
        valid_layer,
        data_sources={
            **valid_layer.data_sources,
            "orders": replace(source, schema=TableSchema(fields)),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.grain.nullable_column" in {item.code for item in report.diagnostics}


def test_static_check_rejects_relationship_type_mismatch(valid_layer) -> None:  # type: ignore[no-untyped-def]
    relationship = valid_layer.relationships["product_order_items"]
    bad = replace(
        valid_layer,
        relationships={
            **valid_layer.relationships,
            "product_order_items": replace(
                relationship,
                target_column="quantity",
            ),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.relationship.join_type_mismatch" in {
        item.code for item in report.diagnostics
    }


def test_static_check_rejects_sum_of_string_fact(valid_layer) -> None:  # type: ignore[no-untyped-def]
    fact = valid_layer.facts["item_revenue"]
    bad = replace(
        valid_layer,
        facts={
            **valid_layer.facts,
            "item_revenue": replace(fact, data_type="string"),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.measure.invalid_aggregation_type" in {
        item.code for item in report.diagnostics
    }


def test_static_check_passes_clean_layer(valid_layer) -> None:  # type: ignore[no-untyped-def]
    report = verify(valid_layer, StaticCheck())
    assert report.passed
    assert report.diagnostics == ()


def test_verify_rejects_unknown_check(valid_layer) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError, match="unsupported verification check"):
        verify(valid_layer, object())  # type: ignore[arg-type]
