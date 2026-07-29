from datetime import UTC, datetime
from pathlib import PurePosixPath

from selayer.okf import OkfConcept
from selayer.okf.compatibility import effective_generated_at


def _concept(frontmatter: dict[str, object]) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
    )


def test_effective_generated_at_prefers_generated_at() -> None:
    stamped = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)

    concept = _concept(
        {
            "type": "Metric",
            "generated": {"by": "process:selayer-okf", "at": stamped},
            "timestamp": "2026-01-01",
        }
    )

    assert effective_generated_at(concept) is stamped


def test_effective_generated_at_falls_back_to_legacy_timestamp() -> None:
    concept = _concept({"type": "Metric", "timestamp": "2026-01-01"})

    assert effective_generated_at(concept) == "2026-01-01"


def test_effective_generated_at_returns_none_without_metadata() -> None:
    assert effective_generated_at(_concept({"type": "Metric"})) is None


def test_malformed_generated_does_not_fall_back_to_timestamp() -> None:
    concept = _concept(
        {"type": "Metric", "generated": "not-a-mapping", "timestamp": "2026-01-01"}
    )

    assert effective_generated_at(concept) is None


def test_generated_mapping_without_at_returns_none_without_fallback() -> None:
    concept = _concept(
        {"type": "Metric", "generated": {"by": "process:x"}, "timestamp": "2026-01-01"}
    )

    assert effective_generated_at(concept) is None


def test_effective_generated_at_does_not_mutate_frontmatter() -> None:
    concept = _concept({"type": "Metric", "timestamp": "2026-01-01"})

    effective_generated_at(concept)

    assert dict(concept.frontmatter) == {"type": "Metric", "timestamp": "2026-01-01"}
