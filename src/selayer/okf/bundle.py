from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import yaml

from selayer.catalog import SemanticLayer

from .document import OkfDocumentError, parse_concept, render_concept
from .model import OkfConcept, OkfIssue, OkfSection, OkfValidationError, SyncReport
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


_LEADING_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_GENERATOR_ID = "process:selayer-okf"
_GENERATED_KEYS = frozenset({"type", "title", "description", "selayer_id", "generated"})
_GENERATED_SECTION = "Catalog Definition"


def _has_generator_ownership(path: Path) -> bool:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = _LEADING_FRONTMATTER.match(text)
    if match is None:
        return False
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return False
    if not isinstance(frontmatter, Mapping):
        return False
    generated = frontmatter.get("generated")
    return isinstance(generated, Mapping) and generated.get("by") == _GENERATOR_ID


def _remove_stale_generated_files(
    destination: Path,
    directories: tuple[str, ...],
    expected: set[str],
) -> None:
    for directory in directories:
        managed = destination / directory
        if not managed.is_dir():
            continue
        for path in sorted(managed.glob("*.md")):
            relative = path.relative_to(destination).as_posix()
            if relative in expected:
                continue
            if path.name == "_index.md" or _has_generator_ownership(path):
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

    def sync(self, path: str | Path, *, dry_run: bool = False) -> SyncReport:
        from .generation import generated_directories, index_documents

        destination = Path(path)
        if destination.is_file():
            raise FileExistsError(f"destination '{destination}' is a file")
        written: list[str] = []
        unchanged: list[str] = []
        conflicts: list[str] = []
        for concept_id in sorted(self.concepts):
            generated = self.concepts[concept_id]
            relative = generated.relative_path.as_posix()
            concept_path = destination / Path(relative)
            if not concept_path.exists():
                if not dry_run:
                    _write_text(concept_path, render_concept(generated))
                written.append(relative)
                continue

            try:
                existing = parse_concept(concept_path, destination)
            except OkfDocumentError:
                conflicts.append(relative)
                continue
            existing_definitions = tuple(
                section
                for section in existing.sections
                if section.title == _GENERATED_SECTION
            )
            if len(existing_definitions) != 1:
                conflicts.append(relative)
                continue
            existing_definition = existing_definitions[0]
            generated_definition = next(
                section
                for section in generated.sections
                if section.title == _GENERATED_SECTION
            )
            frontmatter = dict(existing.frontmatter)
            for key in _GENERATED_KEYS - generated.frontmatter.keys():
                frontmatter.pop(key, None)
            for key, value in generated.frontmatter.items():
                if key in _GENERATED_KEYS:
                    frontmatter[key] = value
            if existing_definition.content != generated_definition.content:
                frontmatter.pop("verified", None)
            sections = tuple(
                OkfSection(section.title, generated_definition.content)
                if section is existing_definition
                else section
                for section in existing.sections
            )
            merged = OkfConcept.create(
                concept_id=existing.concept_id,
                relative_path=existing.relative_path,
                frontmatter=frontmatter,
                preamble=existing.preamble,
                sections=sections,
                links=existing.links,
            )
            content = render_concept(merged)
            if content == concept_path.read_text(encoding="utf-8"):
                unchanged.append(relative)
            else:
                if not dry_run:
                    _write_text(concept_path, content)
                written.append(relative)

        classified = set(written) | set(unchanged) | set(conflicts)
        semantic_ids = {
            concept.frontmatter["selayer_id"] for concept in self.concepts.values()
        }
        orphaned: list[str] = []
        for directory in generated_directories():
            managed = destination / directory
            if not managed.is_dir():
                continue
            for concept_path in sorted(managed.rglob("*.md")):
                if concept_path.name == "_index.md":
                    continue
                relative = concept_path.relative_to(destination).as_posix()
                if relative in classified:
                    continue
                try:
                    existing = parse_concept(concept_path, destination)
                except OkfDocumentError:
                    continue
                selayer_id = existing.frontmatter.get("selayer_id")
                if isinstance(selayer_id, str) and selayer_id not in semantic_ids:
                    orphaned.append(relative)
                    unchanged.append(relative)

        if not dry_run:
            for relative_path, content in index_documents(
                self.layer, self.concepts
            ).items():
                _write_text(destination / Path(relative_path.as_posix()), content)

        return SyncReport(
            written=tuple(sorted(written)),
            unchanged=tuple(sorted(unchanged)),
            conflicts=tuple(sorted(conflicts)),
            orphaned=tuple(sorted(orphaned)),
        )

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
