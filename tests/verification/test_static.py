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

from selayer import DataSource, Fact, Metric, SemanticLayer, TableSchema
from selayer.catalog import CatalogIssue, CatalogValidationError
from selayer.model import SemanticStatus
from selayer.sources.config import ParquetConfig
from selayer.sources.schema import FieldSchema, ScalarType
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


# ---------------------------------------------------------------------------
# P1: fact-expression parity between programmatic layers and the YAML loader
# ---------------------------------------------------------------------------


def test_static_check_fact_expression_reports_unknown_source_symbol(
    valid_layer,  # type: ignore[no-untyped-def]
) -> None:
    """A typed fact referencing an unknown source symbol reports it.

    Parity with the YAML loader, which calls the shared
    ``validate_row_expression`` helper so an unknown source in a fact
    expression emits "source 'X' is not known". The typed validator must use
    the same helper rather than silently skipping the reference.
    """
    fact = valid_layer.facts["item_revenue"]
    bad = replace(
        valid_layer,
        facts={
            **valid_layer.facts,
            "item_revenue": replace(
                fact,
                expression=Fact.from_expression(
                    "item_revenue", "order_items", "phantom.col", "decimal"
                ).expression,
            ),
        },
    )
    report = verify(bad, StaticCheck())
    assert any(
        diagnostic.path == "facts.item_revenue.expression"
        and diagnostic.message == "source 'phantom' is not known"
        for diagnostic in report.diagnostics
    )


def test_static_check_fact_expression_reports_function_arity(
    valid_layer,  # type: ignore[no-untyped-def]
) -> None:
    """A typed fact calling a row function with the wrong arity reports it.

    Parity with the YAML loader's row-function arity check: the typed
    validator must run the same ``validate_row_expression`` helper.
    """
    fact = valid_layer.facts["item_revenue"]
    bad = replace(
        valid_layer,
        facts={
            **valid_layer.facts,
            "item_revenue": replace(
                fact,
                expression=Fact.from_expression(
                    "item_revenue",
                    "order_items",
                    "coalesce(order_items.total)",
                    "decimal",
                ).expression,
            ),
        },
    )
    report = verify(bad, StaticCheck())
    assert any(
        diagnostic.path == "facts.item_revenue.expression"
        and diagnostic.message == "function 'coalesce' expects 2 argument(s), got 1"
        for diagnostic in report.diagnostics
    )


def _minimal_layer_with_defective_fact() -> SemanticLayer:
    """A programmatic layer whose one fact has an unknown source + bad arity."""
    return SemanticLayer(
        version=1,
        name="m",
        label="",
        description="",
        data_sources={
            "orders": DataSource(
                name="orders",
                connector=ParquetConfig("x"),
                schema=TableSchema(
                    (
                        FieldSchema("id", ScalarType("utf8"), False),
                        FieldSchema("total", ScalarType("float64"), True),
                    )
                ),
                grain=("id",),
            )
        },
        dimensions={},
        facts={
            "amount": Fact.from_expression(
                "amount",
                "orders",
                "phantom.col + coalesce(orders.total)",
                "decimal",
            )
        },
        measures={},
        metrics={},
        relationships={},
    )


def test_static_fact_expression_diagnostics_match_yaml_loader(
    tmp_path: Path,
) -> None:
    """A programmatic defective fact yields the same diagnostics as load().

    Builds one minimal catalog with a fact expression that is invalid in two
    independent ways (an unknown source symbol and a wrong-arity function call)
    in both forms -- a YAML document and an equivalent programmatic
    ``SemanticLayer`` -- and asserts the typed ``verify_static`` diagnostics are
    identical to the loader's ``CatalogValidationError`` issues.
    """
    yaml_text = (
        "version: 1\nname: m\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: total, type: float64, nullable: true}\n"
        "facts:\n  amount:\n    source: orders\n"
        "    expression: phantom.col + coalesce(orders.total)\n"
        "    data_type: decimal\n"
    )
    path = tmp_path / "layer.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    yaml_pairs = {(issue.path, issue.message) for issue in caught.value.issues}

    report = verify(_minimal_layer_with_defective_fact(), StaticCheck())
    typed_pairs = {
        (diagnostic.path, diagnostic.message) for diagnostic in report.diagnostics
    }

    assert typed_pairs == yaml_pairs


# ---------------------------------------------------------------------------
# P2: malformed programmatic collection entries yield coded diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("data_sources", "orders"),
        ("dimensions", "order_date"),
        ("facts", "item_revenue"),
        ("measures", "total_item_revenue"),
        ("metrics", "gross_margin"),
        ("relationships", "product_order_items"),
    ],
)
def test_static_check_malformed_collection_entry_is_coded_not_crash(
    valid_layer,  # type: ignore[no-untyped-def]
    section: str,
    key: str,
) -> None:
    """A malformed programmatic collection entry yields a coded diagnostic.

    ``_validate_named_models`` records the wrong value type as a coded issue;
    the subsequent per-object and cross-collection validators must never
    dereference the malformed value (which would raise ``AttributeError``).
    """
    bad = replace(
        valid_layer,
        **{section: {**getattr(valid_layer, section), key: "not-a-model"}},
    )
    report = verify(bad, StaticCheck())
    assert report.diagnostics
    assert any(
        diagnostic.path == f"{section}.{key}" and "must be a" in diagnostic.message
        for diagnostic in report.diagnostics
    )


def test_verify_rejects_unknown_check(valid_layer) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError, match="unsupported verification check"):
        verify(valid_layer, object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Deprecation replacement-graph validation (Task 3)
# ---------------------------------------------------------------------------


def _metric_layer(valid_layer: SemanticLayer, **metrics: Metric) -> SemanticLayer:
    """Return a copy of ``valid_layer`` with the given metrics overlaid.

    Replacement metrics are passed by local name (e.g.
    ``gross_margin=...``); the overlay preserves every other collection so the
    declaration rules still resolve measures, facts, and sources.
    """
    return replace(valid_layer, metrics={**valid_layer.metrics, **metrics})


def _deprecated(name: str, metric: Metric, replaced_by: str | None) -> Metric:
    return replace(
        metric, name=name, status=SemanticStatus.DEPRECATED, replaced_by=replaced_by
    )


def test_static_replacement_missing_fails(valid_layer: SemanticLayer) -> None:
    """A deprecated object with no resolvable replacement fails static validation."""
    base = valid_layer.metrics["gross_margin"]
    # No ``replaced_by`` declared at all.
    layer = _metric_layer(valid_layer, gross_margin=_deprecated("gross_margin", base, None))
    report = verify(layer, StaticCheck())
    codes = {item.code for item in report.diagnostics}
    assert "catalog.deprecation.replacement_missing" in codes
    assert not report.passed


def test_static_replacement_unresolved_target_fails(valid_layer: SemanticLayer) -> None:
    """A ``replaced_by`` pointing at an unknown object fails as missing."""
    base = valid_layer.metrics["gross_margin"]
    layer = _metric_layer(
        valid_layer, gross_margin=_deprecated("gross_margin", base, "metric.ghost")
    )
    report = verify(layer, StaticCheck())
    codes = {item.code for item in report.diagnostics}
    assert "catalog.deprecation.replacement_missing" in codes
    assert not report.passed


def test_static_replacement_kind_mismatch_fails(valid_layer: SemanticLayer) -> None:
    """A replacement of a different semantic kind fails."""
    base = valid_layer.metrics["gross_margin"]
    layer = _metric_layer(
        valid_layer,
        gross_margin=_deprecated("gross_margin", base, "dimension.product_category"),
    )
    report = verify(layer, StaticCheck())
    codes = {item.code for item in report.diagnostics}
    assert "catalog.deprecation.replacement_kind" in codes
    assert "catalog.deprecation.replacement_missing" not in codes
    assert not report.passed


def test_static_self_replacement_fails(valid_layer: SemanticLayer) -> None:
    """An object that replaces itself fails with the self-replacement code."""
    base = valid_layer.metrics["gross_margin"]
    layer = _metric_layer(
        valid_layer,
        gross_margin=_deprecated("gross_margin", base, "metric.gross_margin"),
    )
    report = verify(layer, StaticCheck())
    codes = {item.code for item in report.diagnostics}
    assert "catalog.deprecation.self_replacement" in codes
    assert "catalog.deprecation.cycle" not in codes
    assert not report.passed


def test_static_replacement_cycle_fails(valid_layer: SemanticLayer) -> None:
    """A two-object replacement cycle fails with the cycle code on both nodes."""
    base = valid_layer.metrics["gross_margin"]
    v2 = replace(base, name="gross_margin_v2")
    layer = _metric_layer(
        valid_layer,
        gross_margin=_deprecated("gross_margin", base, "metric.gross_margin_v2"),
        gross_margin_v2=_deprecated("gross_margin_v2", v2, "metric.gross_margin"),
    )
    report = verify(layer, StaticCheck())
    cycle_paths = {
        item.path for item in report.diagnostics if item.code == "catalog.deprecation.cycle"
    }
    assert cycle_paths == {"metric.gross_margin", "metric.gross_margin_v2"}
    assert not report.passed


def test_static_valid_same_kind_chain_passes(valid_layer: SemanticLayer) -> None:
    """A valid same-kind replacement chain produces notices but no errors."""
    base = valid_layer.metrics["gross_margin"]
    v2 = replace(base, name="gross_margin_v2")
    v3 = replace(base, name="gross_margin_v3")
    layer = _metric_layer(
        valid_layer,
        gross_margin=_deprecated("gross_margin", base, "metric.gross_margin_v2"),
        gross_margin_v2=_deprecated("gross_margin_v2", v2, "metric.gross_margin_v3"),
        gross_margin_v3=v3,
    )
    report = verify(layer, StaticCheck())
    errors = [item for item in report.diagnostics if item.severity == "error"]
    assert errors == []
    assert report.passed


def test_static_emits_one_notice_per_deprecated_object(valid_layer: SemanticLayer) -> None:
    """Each deprecated object yields exactly one non-blocking notice."""
    base = valid_layer.metrics["gross_margin"]
    v2 = replace(base, name="gross_margin_v2")
    layer = _metric_layer(
        valid_layer,
        gross_margin=_deprecated("gross_margin", base, "metric.gross_margin_v2"),
        gross_margin_v2=v2,
    )
    report = verify(layer, StaticCheck())
    notices = [
        item for item in report.diagnostics if item.code == "catalog.deprecation.notice"
    ]
    assert len(notices) == 1
    assert notices[0].severity == "info"
    assert notices[0].path == "metric.gross_margin"
    # The notice is non-blocking: the report still passes.
    assert report.passed


def test_static_deprecation_outcome_carries_count(valid_layer: SemanticLayer) -> None:
    """The deprecation outcome is present only when objects are deprecated."""
    base = valid_layer.metrics["gross_margin"]
    v2 = replace(base, name="gross_margin_v2")
    layer = _metric_layer(
        valid_layer,
        gross_margin=_deprecated("gross_margin", base, "metric.gross_margin_v2"),
        gross_margin_v2=v2,
    )
    report = verify(layer, StaticCheck())
    outcome = next(
        item for item in report.outcomes if item.check_id == "catalog.deprecation"
    )
    assert outcome.evidence["deprecated_count"] == 1
    # A clean catalog with no deprecations has no deprecation outcome.
    clean = verify(valid_layer, StaticCheck())
    assert all(item.check_id != "catalog.deprecation" for item in clean.outcomes)
