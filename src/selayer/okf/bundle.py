from __future__ import annotations

import hmac
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit

from selayer.catalog import SemanticLayer

from .computation import attested_computation
from .document import (
    OkfControlledMergeError,
    OkfDocumentError,
    generated_fingerprint,
    merge_generated_concept_text,
    parse_concept,
    render_concept,
)
from .model import (
    ContextBudgetError,
    ContextItem,
    ContextLookupError,
    ContextResult,
    Freshness,
    OkfConcept,
    OkfIssue,
    OkfValidationError,
    SyncReport,
    TrustTier,
)
from .validation import (
    _is_iso_datetime,
    validate_concept,
    validate_duplicate_bindings,
    validate_index,
    validate_links,
    validate_log,
)


def _preflight_mutation_path(destination: Path) -> None:
    """Reject symlinks in a mutation path without resolving through them."""
    cursor = Path(destination.anchor) if destination.is_absolute() else Path.cwd()
    parts = destination.parts[1:] if destination.is_absolute() else destination.parts
    symlink: Path | None = cursor if cursor.is_symlink() else None
    for part in parts:
        if symlink is not None:
            break
        if part in ("", "."):
            continue
        if part == "..":
            cursor = cursor.parent
            continue
        cursor /= part
        if cursor.is_symlink():
            symlink = cursor

    if symlink is None and destination.exists():
        symlink = next(
            (
                candidate
                for candidate in sorted(
                    destination.rglob("*"), key=lambda path: path.as_posix()
                )
                if candidate.is_symlink()
            ),
            None,
        )
    if symlink is not None:
        raise FileExistsError(
            f"destination '{destination}' contains symbolic link '{symlink}'"
        )


def _write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    _preflight_mutation_path(path)
    _preflight_mutation_path(temporary)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


_GENERATED_SECTION = "Catalog Definition"
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


def trust_tier(frontmatter: Mapping[str, Any]) -> TrustTier:
    verified = frontmatter.get("verified")
    if isinstance(verified, Mapping):
        events = (verified,)
    elif isinstance(verified, (list, tuple)) and verified:
        events = verified
    else:
        return "unverified"

    actors: list[str] = []
    for event in events:
        if not isinstance(event, Mapping):
            return "unverified"
        actor = event.get("by")
        if (
            not isinstance(actor, str)
            or not actor.strip()
            or not _is_iso_datetime(event.get("at"))
        ):
            return "unverified"
        actors.append(actor)
    if any(actor.startswith("human:") for actor in actors):
        return "human_reviewed"
    return "machine_confirmed"


def freshness(frontmatter: Mapping[str, Any], today: date) -> Freshness:
    value = frontmatter.get("stale_after")
    if isinstance(value, datetime):
        return "unspecified"
    if isinstance(value, date):
        stale_after = value
    elif isinstance(value, str):
        try:
            stale_after = date.fromisoformat(value)
        except ValueError:
            return "unspecified"
    else:
        return "unspecified"
    return "stale" if today >= stale_after else "current"


def _sources(frontmatter: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(source["resource"] for source in frontmatter.get("sources", ()))


def _render_context(concept: OkfConcept, sources: tuple[str, ...]) -> str:
    frontmatter = concept.frontmatter
    parts: list[str] = []
    title = frontmatter.get("title")
    if isinstance(title, str) and title:
        parts.append(f"# {title}")
    description = frontmatter.get("description")
    if isinstance(description, str) and description:
        parts.append(description)
    if concept.preamble:
        parts.append(concept.preamble)
    parts.extend(
        f"# {section.title}\n\n{section.content}".rstrip()
        for section in concept.sections
    )
    if sources:
        parts.append("## Sources\n\n" + "\n".join(f"- {source}" for source in sources))
    return "\n\n".join(parts)


def _context_item(concept: OkfConcept, today: date) -> ContextItem:
    frontmatter = concept.frontmatter
    sources = _sources(frontmatter)
    semantic_id = frontmatter.get("selayer_id")
    return ContextItem(
        concept_id=concept.concept_id,
        kind=frontmatter["type"],
        content=_render_context(concept, sources),
        provider="selayer",
        semantic_refs=(semantic_id,) if isinstance(semantic_id, str) else (),
        trust=trust_tier(frontmatter),
        freshness=freshness(frontmatter, today),
        sources=sources,
        attested_computation=attested_computation(concept),
    )


def _resolved_link(
    source: OkfConcept,
    link: str,
    concepts_by_path: Mapping[PurePosixPath, OkfConcept],
) -> OkfConcept | None:
    try:
        split = urlsplit(link)
    except ValueError:
        return None
    if split.scheme or split.netloc or (not split.path and split.fragment):
        return None
    path_text = unquote(split.path)
    if not path_text:
        return None
    if path_text.startswith("/"):
        normalized = posixpath.normpath(path_text.lstrip("/"))
    else:
        normalized = posixpath.normpath(
            posixpath.join(source.relative_path.parent.as_posix(), path_text)
        )
    if normalized == ".." or normalized.startswith("../"):
        return None
    return concepts_by_path.get(PurePosixPath(normalized))


def _linked_concepts(
    source: OkfConcept,
    concepts_by_path: Mapping[PurePosixPath, OkfConcept],
) -> tuple[OkfConcept, ...]:
    resolved = {
        concept.concept_id: concept
        for link in source.links
        if (concept := _resolved_link(source, link, concepts_by_path)) is not None
    }
    return tuple(resolved[concept_id] for concept_id in sorted(resolved))


def _context_warning(concept: OkfConcept, message: str) -> OkfIssue:
    return OkfIssue(
        path=concept.relative_path.as_posix(),
        message=message,
        severity="warning",
    )


def _item_diagnostics(item: ContextItem, concept: OkfConcept) -> tuple[OkfIssue, ...]:
    issues: list[OkfIssue] = []
    if item.freshness == "stale":
        issues.append(_context_warning(concept, "returned context is stale"))
    if item.trust == "unverified":
        issues.append(_context_warning(concept, "returned context is unverified"))
    return tuple(issues)


def _item_chars(item: ContextItem) -> int:
    """Explicit character size of a context item.

    Counts the rendered content plus every string carried by a non-null
    Attested Computation contract. An authored contract can carry
    arbitrary-length valid parameter and receipt strings, so the structured
    data a context item returns is bounded alongside its rendered text.
    """
    total = len(item.content)
    contract = item.attested_computation
    if contract is None:
        return total
    total += len(contract.runtime)
    for parameter in contract.parameters:
        total += len(parameter.name)
        total += len(parameter.type)
        # Bound the serialized `required` flag token (true/false), not repr.
        total += len("true") if parameter.required else len("false")
    if contract.computation_path is not None:
        total += len(contract.computation_path)
    total += len(contract.computation_body)
    if contract.executor_resource is not None:
        total += len(contract.executor_resource)
    total += sum(len(receipt) for receipt in contract.executor_receipt)
    if contract.attester_resource is not None:
        total += len(contract.attester_resource)
    return total


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
        destination = Path(output_dir)
        bundle = cls.from_layer(layer, include_descriptive=include_descriptive)
        bundle.write(destination)
        return cls.load(destination, layer=layer)

    def write(self, path: str | Path) -> None:
        from .generation import index_documents

        destination = Path(path)
        _preflight_mutation_path(destination)
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
        _write_text(destination / "log.md", "# Change Log\n")

    def sync(self, path: str | Path, *, dry_run: bool = False) -> SyncReport:
        from .generation import generated_directories, index_documents

        destination = Path(path)
        _preflight_mutation_path(destination)
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
                existing_bytes = concept_path.read_bytes()
                existing_text = existing_bytes.decode("utf-8")
            except (OkfDocumentError, UnicodeError):
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
            existing_generated = existing.frontmatter.get("generated")
            fingerprint = (
                existing_generated.get("fingerprint")
                if isinstance(existing_generated, Mapping)
                else None
            )
            if (
                not isinstance(fingerprint, str)
                or _SHA256_HEX.fullmatch(fingerprint) is None
            ):
                conflicts.append(relative)
                continue
            try:
                baseline_fingerprint = generated_fingerprint(
                    existing.frontmatter, existing_definition.content
                )
            except (TypeError, ValueError):
                conflicts.append(relative)
                continue
            if not hmac.compare_digest(fingerprint.lower(), baseline_fingerprint):
                conflicts.append(relative)
                continue
            generated_definitions = tuple(
                section
                for section in generated.sections
                if section.title == _GENERATED_SECTION
            )
            if len(generated_definitions) != 1:
                raise AssertionError("generated concept has no unique definition")
            generated_definition = generated_definitions[0]
            definition_changed = (
                existing_definition.content != generated_definition.content
            )
            try:
                content = merge_generated_concept_text(
                    existing_text,
                    generated,
                    definition_changed=definition_changed,
                )
            except OkfControlledMergeError:
                conflicts.append(relative)
                continue
            encoded = content.encode("utf-8")
            if encoded == existing_bytes:
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
                if concept_path.name == "index.md":
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
            indexes = index_documents(self.layer, self.concepts)
            for relative_path, content in indexes.items():
                index_path = destination / Path(relative_path.as_posix())
                if index_path.is_file() and index_path.read_bytes() == content.encode(
                    "utf-8"
                ):
                    continue
                _write_text(index_path, content)
            expected_indexes = {path.as_posix() for path in indexes}
            for directory in generated_directories():
                index_path = destination / directory / "index.md"
                relative = index_path.relative_to(destination).as_posix()
                if index_path.is_file() and relative not in expected_indexes:
                    index_path.unlink()
            log_path = destination / "log.md"
            if not log_path.exists():
                _write_text(log_path, "# Change Log\n")

        return SyncReport(
            written=tuple(sorted(written)),
            unchanged=tuple(sorted(unchanged)),
            conflicts=tuple(sorted(conflicts)),
            orphaned=tuple(sorted(orphaned)),
        )

    def context_for(
        self,
        semantic_ids: Iterable[str],
        *,
        include_linked: bool = True,
        max_chars: int = 12_000,
        max_depth: int = 1,
        today: date | None = None,
    ) -> ContextResult:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if max_depth < 0:
            raise ValueError("max_depth must not be negative")

        concepts_by_semantic_id: dict[str, OkfConcept] = {}
        for concept_id in sorted(self.concepts):
            concept = self.concepts[concept_id]
            semantic_id = concept.frontmatter.get("selayer_id")
            if not isinstance(semantic_id, str):
                continue
            if semantic_id in concepts_by_semantic_id:
                raise ContextLookupError(
                    f"duplicate concept binding for semantic identifier '{semantic_id}'"
                )
            concepts_by_semantic_id[semantic_id] = concept

        required: list[OkfConcept] = []
        seen_semantic_ids: set[str] = set()
        for semantic_id in semantic_ids:
            if semantic_id in seen_semantic_ids:
                continue
            seen_semantic_ids.add(semantic_id)
            try:
                required.append(concepts_by_semantic_id[semantic_id])
            except KeyError:
                raise ContextLookupError(
                    f"unknown semantic identifier '{semantic_id}'"
                ) from None

        effective_today = (
            today if today is not None else datetime.now(timezone(timedelta(0))).date()
        )
        items = [_context_item(concept, effective_today) for concept in required]
        total_chars = sum(_item_chars(item) for item in items)
        if total_chars > max_chars:
            raise ContextBudgetError(total_chars, max_chars)

        dynamic_diagnostics = [
            issue
            for item, concept in zip(items, required, strict=True)
            for issue in _item_diagnostics(item, concept)
        ]
        if include_linked and max_depth > 0:
            concepts_by_path = {
                concept.relative_path: concept for concept in self.concepts.values()
            }
            visited = {concept.concept_id for concept in required}
            queue = [(concept, 0) for concept in required]
            omitted = False
            position = 0
            while position < len(queue) and not omitted:
                source, depth = queue[position]
                position += 1
                if depth >= max_depth:
                    continue
                for linked in _linked_concepts(source, concepts_by_path):
                    if linked.concept_id in visited:
                        continue
                    visited.add(linked.concept_id)
                    linked_item = _context_item(linked, effective_today)
                    linked_chars = _item_chars(linked_item)
                    if total_chars + linked_chars > max_chars:
                        omitted = True
                        break
                    items.append(linked_item)
                    total_chars += linked_chars
                    dynamic_diagnostics.extend(_item_diagnostics(linked_item, linked))
                    queue.append((linked, depth + 1))
            if omitted:
                dynamic_diagnostics.append(
                    OkfIssue(
                        path="context",
                        message=(
                            "omitted linked context because it exceeds "
                            f"the max_chars budget of {max_chars}"
                        ),
                        severity="warning",
                    )
                )

        return ContextResult(
            items=tuple(items),
            diagnostics=self.diagnostics + tuple(dynamic_diagnostics),
            total_chars=total_chars,
        )

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        layer: SemanticLayer | None = None,
        strict: bool = True,
    ) -> OkfBundle:
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"bundle root does not exist: '{root}'")
        if not root.is_dir():
            raise NotADirectoryError(f"bundle root is not a directory: '{root}'")
        concepts: dict[str, OkfConcept] = {}
        issues: list[OkfIssue] = []
        for concept_path in sorted(root.rglob("*.md")):
            if concept_path.name == "index.md":
                issues.extend(validate_index(concept_path, root))
                continue
            if concept_path == root / "log.md":
                issues.extend(validate_log(concept_path, root))
                continue
            try:
                concept = parse_concept(concept_path, root)
            except OkfDocumentError as error:
                relative = concept_path.relative_to(root).as_posix()
                issues.append(OkfIssue(relative, str(error)))
                continue
            concepts[concept.concept_id] = concept
            issues.extend(validate_concept(concept, layer, strict=strict))
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
