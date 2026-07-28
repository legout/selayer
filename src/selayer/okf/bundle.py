from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from selayer.catalog import SemanticLayer

from .document import OkfDocumentError, parse_concept
from .model import OkfConcept, OkfIssue, OkfValidationError
from .validation import (
    validate_concept,
    validate_duplicate_bindings,
    validate_index,
    validate_links,
    validate_log,
)


@dataclass(frozen=True, slots=True)
class OkfBundle:
    root: Path | None
    concepts: Mapping[str, OkfConcept]
    diagnostics: tuple[OkfIssue, ...] = ()
    layer: SemanticLayer | None = None

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        layer: SemanticLayer | None = None,
    ) -> OkfBundle:
        root = Path(path)
        concepts: dict[str, OkfConcept] = {}
        issues: list[OkfIssue] = []
        for concept_path in sorted(root.rglob("*.md")):
            if concept_path.name == "index.md":
                issues.extend(validate_index(concept_path, root))
                continue
            if concept_path.name == "log.md":
                issues.extend(validate_log(concept_path, root))
                continue
            try:
                concept = parse_concept(concept_path, root)
            except OkfDocumentError as error:
                relative = concept_path.relative_to(root).as_posix()
                issues.append(OkfIssue(relative, str(error)))
                continue
            concepts[concept.concept_id] = concept
            issues.extend(validate_concept(concept, layer))
        issues.extend(validate_duplicate_bindings(concepts))
        issues.extend(validate_links(root, concepts))
        ordered = tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
        fatal = tuple(issue for issue in ordered if issue.severity == "error")
        if fatal:
            raise OkfValidationError(fatal)
        return cls(
            root=root,
            concepts=MappingProxyType(dict(sorted(concepts.items()))),
            diagnostics=ordered,
            layer=layer,
        )


__all__ = ["OkfBundle"]
