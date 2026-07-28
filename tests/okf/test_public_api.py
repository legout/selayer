import selayer
import selayer.okf


def test_okf_public_api_is_the_approved_boundary() -> None:
    assert set(selayer.okf.__all__) == {
        "AttestedComputation",
        "ContextBudgetError",
        "ContextItem",
        "ContextLookupError",
        "ContextResult",
        "OkfBundle",
        "OkfConcept",
        "OkfIssue",
        "OkfParameter",
        "OkfValidationError",
        "SyncReport",
    }


def test_okf_exports_attested_computation_types() -> None:
    from selayer.okf import AttestedComputation, ContextItem, OkfParameter

    assert AttestedComputation.__slots__ == (
        "runtime",
        "parameters",
        "computation_path",
        "computation_body",
        "executor_resource",
        "executor_receipt",
        "attester_resource",
    )
    assert OkfParameter.__slots__ == ("name", "type", "required")
    item = ContextItem(
        concept_id="c",
        kind="Attested Computation",
        content="",
        provider="selayer",
        semantic_refs=(),
        trust="unverified",
        freshness="unspecified",
        sources=(),
    )
    assert item.attested_computation is None


def test_package_root_exposes_only_okf_bundle_from_okf_api() -> None:
    assert selayer.OkfBundle is selayer.okf.OkfBundle
    assert "OkfBundle" in selayer.__all__
    assert not (
        {
            "ContextBudgetError",
            "ContextItem",
            "ContextLookupError",
            "ContextResult",
            "OkfConcept",
            "OkfIssue",
            "OkfValidationError",
            "SyncReport",
        }
        & set(selayer.__all__)
    )
