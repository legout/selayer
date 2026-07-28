from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from selayer.catalog import SemanticLayer

from .document import OkfDocumentError, parse_concept, render_concept
from .model import OkfConcept, OkfIssue, OkfValidationError
from .validation import (
    validate_concept,
    validate_duplicate_bindings,
    validate_index,
    validate_links,
    validate_log,
)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_stale_generated_files(
    destination: Path,
    directories: tuple[str, ...],
    expected: set[str],
) -> None:
    marker = "generated:\n  by: process:selayer-okf"
    for directory in directories:
        managed = destination / directory
        if not managed.is_dir():
            continue
        for path in managed.glob("*.md"):
            relative = path.relative_to(destination).as_posix()
            if relative in expected:
                continue
            if path.name == "_index.md" or marker in path.read_text(encoding="utf-8"):
                path.unlink()


@dataclass(frozen=True, slots=True)
class OkfBundle:
    root: Path | None
    concepts: Mapping[str, OkfConcept]
    diagnostics: tuple[OkfIssue, ...] = ()
    layer: SemanticLayer | None = None

    @classmethod
    def from_layer(
        cls,
        layer: SemanticLayer,
        generated_at: datetime | None = None,
        *,
        include_descriptive: bool = True,
    ) -> OkfBundle:
        from .generation import concepts_from_layer

        return cls(
            root=None,
            concepts=concepts_from_layer(
                layer,
                generated_at=generated_at,
                include_descriptive=include_descriptive,
            ),
            layer=layer,
        )

    @classmethod
    def generate(
        cls,
        layer: SemanticLayer,
        output_dir: str | Path,
        *,
        include_descriptive: bool = False,
    ) -> OkfBundle:
        from .generation import generated_directories, index_documents

        destination = Path(output_dir)
        if destination.is_file():
            raise FileExistsError(f"destination '{destination}' is a file")
        destination.mkdir(parents=True, exist_ok=True)
        bundle = cls.from_layer(layer, include_descriptive=include_descriptive)
        for concept_id in sorted(bundle.concepts):
            concept = bundle.concepts[concept_id]
            _write_text(
                destination / Path(concept.relative_path.as_posix()),
                render_concept(concept),
            )
        indexes = index_documents(layer, bundle.concepts)
        for relative_path, content in indexes.items():
            _write_text(destination / Path(relative_path.as_posix()), content)
        change_log = destination / "_change_log.md"
        if not change_log.exists():
            _write_text(change_log, "# Change Log\n")
        expected = {
            concept.relative_path.as_posix() for concept in bundle.concepts.values()
        }
        expected.update(path.as_posix() for path in indexes)
        _remove_stale_generated_files(
            destination,
            generated_directories(),
            expected,
        )
        return cls(
            root=destination,
            concepts=bundle.concepts,
            diagnostics=bundle.diagnostics,
            layer=layer,
        )

    def write(self, path: str | Path) -> None:
        from .generation import index_documents

        destination = Path(path)
        if destination.is_file() or (
            destination.exists()
            and any(candidate.is_file() for candidate in destination.rglob("*"))
        ):
            raise FileExistsError(
                f"destination '{destination}' contains files; use sync"
            )
        destination.mkdir(parents=True, exist_ok=True)
        for concept_id in sorted(self.concepts):
            concept = self.concepts[concept_id]
            _write_text(
                destination / Path(concept.relative_path.as_posix()),
                render_concept(concept),
            )
        for relative_path, content in index_documents(
            self.layer, self.concepts
        ).items():
            _write_text(destination / Path(relative_path.as_posix()), content)
        _write_text(destination / "_change_log.md", "# Change Log\n")

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
            if concept_path.name in {"_index.md", "index.md"}:
                issues.extend(validate_index(concept_path, root))
                continue
            if concept_path.name in {"_change_log.md", "log.md"}:
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
