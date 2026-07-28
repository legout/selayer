import selayer
import selayer.okf


def test_okf_public_api_is_the_approved_boundary() -> None:
    assert set(selayer.okf.__all__) == {
        "ContextBudgetError",
        "ContextItem",
        "ContextLookupError",
        "ContextResult",
        "OkfBundle",
        "OkfConcept",
        "OkfIssue",
        "OkfValidationError",
        "SyncReport",
    }


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
