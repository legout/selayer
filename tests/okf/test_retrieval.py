from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path, PurePosixPath

import pytest

from selayer.okf import (
    ContextBudgetError,
    ContextLookupError,
    OkfBundle,
    OkfConcept,
)
from selayer.okf.model import OkfSection


def _write_concept(
    root: Path,
    relative_path: str,
    frontmatter: str,
    body: str = "",
) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


@pytest.fixture
def loaded_okf_bundle(tmp_path: Path) -> OkfBundle:
    _write_concept(
        tmp_path,
        "metrics/gross_margin.md",
        "type: Selayer Metric\n"
        "title: Gross Margin\n"
        "description: Margin after item cost.\n"
        "selayer_id: metric.gross_margin\n"
        "verified: {by: 'human:finance', at: '2026-07-20T10:00:00Z'}\n"
        "stale_after: 2026-08-01\n"
        "sources:\n"
        "  - resource: https://example.com/margin-policy\n"
        "  - resource: urn:finance:margin",
        "\nIntroductory guidance.\n\n# Usage Guidance\n\nUse item revenue.\n",
    )
    _write_concept(
        tmp_path,
        "dimensions/mlfb.md",
        "type: Selayer Dimension\n"
        "title: MLFB\n"
        "selayer_id: dimension.mlfb\n"
        "verified: {by: 'process:nightly', at: '2026-07-20T10:00:00Z'}",
        "\n# Related\n\n"
        "[Guide](../references/mlfb_coding_guide.md)\n"
        "[Scheme](../concepts/mlfb_scheme.md)\n"
        "[Decoder](../computations/mlfb_decoder.md)\n"
        "[External](https://example.com/mlfb)\n",
    )
    _write_concept(
        tmp_path,
        "computations/mlfb_decoder.md",
        "type: Attested Computation\nruntime: python",
        "\n# Meaning\n\nDecode MLFB values.\n"
        "[Deep](../references/decoder_details.md)\n",
    )
    _write_concept(
        tmp_path,
        "concepts/mlfb_scheme.md",
        "type: Domain Concept",
        "\n# Meaning\n\nThe MLFB identifier scheme.\n",
    )
    _write_concept(
        tmp_path,
        "references/mlfb_coding_guide.md",
        "type: Reference",
        "\n# Meaning\n\nCanonical coding guide.\n",
    )
    _write_concept(
        tmp_path,
        "references/decoder_details.md",
        "type: Reference",
        "\n# Meaning\n\nDecoder implementation details.\n",
    )
    return OkfBundle.load(tmp_path)


def test_context_for_returns_attributed_direct_concept(
    loaded_okf_bundle: OkfBundle,
) -> None:
    result = loaded_okf_bundle.context_for(
        ["metric.gross_margin"],
        include_linked=False,
        max_chars=12_000,
        today=date(2026, 7, 27),
    )

    assert [item.semantic_refs for item in result.items] == [("metric.gross_margin",)]
    item = result.items[0]
    assert item.provider == "selayer"
    assert item.kind == "Selayer Metric"
    assert item.trust == "human_reviewed"
    assert item.freshness == "current"
    assert item.sources == (
        "https://example.com/margin-policy",
        "urn:finance:margin",
    )
    assert item.content == (
        "# Gross Margin\n\n"
        "Margin after item cost.\n\n"
        "Introductory guidance.\n\n"
        "# Usage Guidance\n\n"
        "Use item revenue.\n\n"
        "## Sources\n\n"
        "- https://example.com/margin-policy\n"
        "- urn:finance:margin"
    )
    assert result.total_chars == len(item.content)
    assert result.total_chars <= 12_000


def test_context_for_follows_internal_links_breadth_first(
    loaded_okf_bundle: OkfBundle,
) -> None:
    result = loaded_okf_bundle.context_for(
        ["dimension.mlfb"],
        include_linked=True,
        max_depth=1,
        max_chars=12_000,
    )

    assert [item.concept_id for item in result.items] == [
        "dimensions/mlfb",
        "computations/mlfb_decoder",
        "concepts/mlfb_scheme",
        "references/mlfb_coding_guide",
    ]


def test_link_traversal_is_depth_bounded_and_breadth_first(
    loaded_okf_bundle: OkfBundle,
) -> None:
    result = loaded_okf_bundle.context_for(
        ["dimension.mlfb"], max_depth=2, max_chars=12_000
    )

    assert [item.concept_id for item in result.items] == [
        "dimensions/mlfb",
        "computations/mlfb_decoder",
        "concepts/mlfb_scheme",
        "references/mlfb_coding_guide",
        "references/decoder_details",
    ]


def test_stale_and_unverified_concepts_are_visible_and_diagnosed(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "dimensions/mlfb.md",
        "type: Selayer Dimension\nselayer_id: dimension.mlfb\nstale_after: 2026-07-27",
        "\n# Meaning\n\nMLFB.\n",
    )
    bundle = OkfBundle.load(tmp_path)

    result = bundle.context_for(["dimension.mlfb"], today=date(2026, 7, 27))

    assert result.items[0].freshness == "stale"
    assert result.items[0].trust == "unverified"
    assert any("stale" in issue.message for issue in result.diagnostics)
    assert any("unverified" in issue.message for issue in result.diagnostics)
    assert all(issue.severity == "warning" for issue in result.diagnostics)


def test_machine_verification_is_visible(loaded_okf_bundle: OkfBundle) -> None:
    item = loaded_okf_bundle.context_for(
        ["dimension.mlfb"], include_linked=False
    ).items[0]

    assert item.trust == "machine_confirmed"
    assert item.freshness == "unspecified"


@pytest.mark.parametrize(
    ("verified", "expected_trust"),
    [
        ([], "unverified"),
        ("invalid", "unverified"),
        ([{"by": "human:finance"}], "unverified"),
        ([{"by": "human:finance", "at": "not-a-datetime"}], "unverified"),
        (
            [{"by": "human:finance", "at": "2026-07-20T10:00:00Z"}],
            "human_reviewed",
        ),
        (
            [{"by": "process:nightly", "at": "2026-07-20T10:00:00Z"}],
            "machine_confirmed",
        ),
        (
            {"by": "human:finance", "at": "2026-07-20T10:00:00Z"},
            "human_reviewed",
        ),
    ],
    ids=[
        "empty-list",
        "wrong-type",
        "missing-at",
        "invalid-at",
        "human-list",
        "machine-list",
        "human-mapping",
    ],
)
def test_in_memory_verification_is_defensive_and_preserves_valid_forms(
    verified: object,
    expected_trust: str,
) -> None:
    concept = OkfConcept.create(
        concept_id="metrics/gross_margin",
        relative_path=PurePosixPath("metrics/gross_margin.md"),
        frontmatter={
            "type": "Selayer Metric",
            "selayer_id": "metric.gross_margin",
            "verified": verified,
        },
    )
    bundle = OkfBundle(root=None, concepts={concept.concept_id: concept})

    result = bundle.context_for(["metric.gross_margin"], include_linked=False)

    assert result.items[0].trust == expected_trust
    has_unverified_warning = any(
        "unverified" in issue.message for issue in result.diagnostics
    )
    assert has_unverified_warning is (expected_trust == "unverified")


def test_mandatory_concept_must_fit_budget(
    loaded_okf_bundle: OkfBundle,
) -> None:
    with pytest.raises(ContextBudgetError) as caught:
        loaded_okf_bundle.context_for(
            ["metric.gross_margin"],
            include_linked=False,
            max_chars=10,
        )

    assert caught.value.max_chars == 10
    assert caught.value.required_chars > 10


def test_optional_context_stops_at_budget_with_one_diagnostic(
    loaded_okf_bundle: OkfBundle,
) -> None:
    ample = loaded_okf_bundle.context_for(
        ["dimension.mlfb"], max_depth=1, max_chars=100_000
    )

    # Budget that fits the direct concept plus exactly the first linked item.
    # Derived from the bundle's own total_chars accounting so it tracks the
    # shared sizing contract (rendered content plus any structured contract);
    # the trailing linked items carry no attested computation, so their content
    # length is their full size.
    trailing = ample.items[2:]
    budget = ample.total_chars - sum(len(item.content) for item in trailing)

    result = loaded_okf_bundle.context_for(
        ["dimension.mlfb"], max_depth=1, max_chars=budget
    )

    assert [item.concept_id for item in result.items] == [
        "dimensions/mlfb",
        "computations/mlfb_decoder",
    ]
    assert result.total_chars == budget
    omitted = [
        issue
        for issue in result.diagnostics
        if "omitted linked context" in issue.message
    ]
    assert len(omitted) == 1


def test_unknown_semantic_id_is_explicit(
    loaded_okf_bundle: OkfBundle,
) -> None:
    with pytest.raises(ContextLookupError, match="dimension.product_color"):
        loaded_okf_bundle.context_for(["dimension.product_color"])


def test_duplicate_semantic_binding_is_explicit(
    loaded_okf_bundle: OkfBundle,
) -> None:
    original = loaded_okf_bundle.concepts["dimensions/mlfb"]
    duplicate = OkfConcept.create(
        concept_id="dimensions/mlfb_duplicate",
        relative_path=PurePosixPath("dimensions/mlfb_duplicate.md"),
        frontmatter=original.frontmatter,
    )
    bundle = OkfBundle(
        root=None,
        concepts={**loaded_okf_bundle.concepts, duplicate.concept_id: duplicate},
    )

    with pytest.raises(ContextLookupError, match="duplicate.*dimension.mlfb"):
        bundle.context_for(["dimension.mlfb"])


@pytest.mark.parametrize("max_chars", [0, -1])
def test_character_budget_must_be_positive(
    loaded_okf_bundle: OkfBundle,
    max_chars: int,
) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        loaded_okf_bundle.context_for(["dimension.mlfb"], max_chars=max_chars)


def test_depth_budget_must_not_be_negative(
    loaded_okf_bundle: OkfBundle,
) -> None:
    with pytest.raises(ValueError, match="max_depth"):
        loaded_okf_bundle.context_for(["dimension.mlfb"], max_depth=-1)


def test_required_order_is_preserved_without_duplicates(
    loaded_okf_bundle: OkfBundle,
) -> None:
    result = loaded_okf_bundle.context_for(
        ["dimension.mlfb", "metric.gross_margin", "dimension.mlfb"],
        include_linked=False,
    )

    assert [item.concept_id for item in result.items] == [
        "dimensions/mlfb",
        "metrics/gross_margin",
    ]


def test_context_result_is_immutable(loaded_okf_bundle: OkfBundle) -> None:
    result = loaded_okf_bundle.context_for(
        ["metric.gross_margin"], include_linked=False
    )

    assert isinstance(result.items, tuple)
    assert isinstance(result.diagnostics, tuple)
    with pytest.raises(FrozenInstanceError):
        result.total_chars = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.items[0].content = "changed"  # type: ignore[misc]


def test_attested_computation_contract_is_surfaced_on_linked_context(
    tmp_path: Path,
) -> None:
    decoder = tmp_path / "computations" / "decoder.md"
    decoder.parent.mkdir(parents=True)
    decoder.write_text(
        "---\n"
        "type: Attested Computation\n"
        "runtime: python\n"
        "parameters:\n"
        "  - {name: mlfb, type: string, required: true}\n"
        "executor:\n"
        "  resource: run.md\n"
        "  receipt: [decoded]\n"
        "attester:\n"
        "  resource: check.py\n"
        "---\n\n"
        "# Computation\n\n"
        "    def decode(mlfb): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "m.md").write_text(
        "---\ntype: Selayer Metric\nselayer_id: metric.gross_margin\n---\n\n"
        "# Definition\n\nDecoded by [the decoder](../computations/decoder.md).\n",
        encoding="utf-8",
    )

    bundle = OkfBundle.load(tmp_path)
    result = bundle.context_for(["metric.gross_margin"], max_depth=1)

    decoder_item = next(
        item for item in result.items if item.concept_id == "computations/decoder"
    )
    contract = decoder_item.attested_computation
    assert contract is not None
    assert contract.runtime == "python"
    assert contract.parameters[0].name == "mlfb"
    assert contract.executor_resource == "run.md"
    assert contract.attester_resource == "check.py"
    assert "def decode" in contract.computation_body


def test_attested_computation_structured_contract_is_bounded() -> None:
    """A budget covering only rendered content must still reject an oversized
    structured Attested Computation contract, reporting the full required count."""
    oversized = "x" * 400
    body = "y" * 400
    concept = OkfConcept.create(
        concept_id="computations/decoder",
        relative_path=PurePosixPath("computations/decoder.md"),
        frontmatter={
            "type": "Attested Computation",
            "selayer_id": "computation.decoder",
            "runtime": oversized,
            "parameters": [
                {"name": oversized, "type": oversized, "required": True}
            ],
            "computation": oversized,
            "executor": {"resource": oversized, "receipt": [oversized]},
            "attester": {"resource": oversized},
        },
        sections=(OkfSection("Computation", body),),
    )
    bundle = OkfBundle(root=None, concepts={concept.concept_id: concept})

    ample = bundle.context_for(
        ["computation.decoder"], include_linked=False, max_chars=10_000
    )
    item = ample.items[0]
    assert item.attested_computation is not None
    content_chars = len(item.content)

    # Seven oversized scalar fields (runtime, param name, param type, path,
    # executor resource, one receipt field, attester resource) plus the
    # computation body live outside the rendered content budget.
    contract_chars = 7 * len(oversized) + len(body)
    required = content_chars + contract_chars

    budget = content_chars
    with pytest.raises(ContextBudgetError) as caught:
        bundle.context_for(
            ["computation.decoder"], include_linked=False, max_chars=budget
        )

    assert caught.value.max_chars == budget
    assert caught.value.required_chars == required
