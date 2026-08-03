"""Parse and validate authored Reference documents and concept overlays.

This module produces private loaders (:func:`load_references` and
:func:`load_overlays`) plus the immutable :class:`OkfOverlay` value. References
are ordinary OKF concepts validated strictly. Overlays are curated curation
that will replace the generated concept body for a bound semantic identifier;
they are parsed with duplicate-key detection and a closed frontmatter /
section vocabulary, and their ``selayer_id`` is resolved against the catalog.

Cross-input link existence is intentionally deferred to the composition step
(Task 8) where generated, Reference, and overlay concept sets are known
together; this loader only rejects self-links, duplicate Related Concepts
links, and links that lexically escape the bundle root.
"""

from __future__ import annotations

import os
import posixpath
import stat as stat_module
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import unquote

import yaml

from selayer.catalog import SemanticLayer

from .document import (
    _FRONTMATTER,
    _LINK,
    OkfDocumentError,
    parse_concept_text,
    split_sections,
)
from .generation import concept_path, display_title
from .model import (
    OkfConcept,
    OkfIssue,
    OkfMetadataError,
    OkfSection,
    OkfValidationError,
    _freeze,
)
from .validation import (
    _KIND_TYPES,
    _SELAYER_ID,
    _is_nonempty_string,
    _safe_urlsplit,
    validate_concept,
)

_ALLOWED_OVERLAY_FIELDS = frozenset({"selayer_id", "sources", "stale_after"})
_ALLOWED_OVERLAY_SECTIONS = (
    "Usage Guidance",
    "Examples",
    "Caveats",
    "Related Concepts",
)
_RESERVED_NAMES = frozenset({"index.md", "log.md"})
_RELATED_CONCEPTS_SECTION = "Related Concepts"
_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"
_MAX_FILES = 1_000
_MAX_FILE_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 16_777_216
_MAX_LINKS_PER_FILE = 1_000
# ``O_NOFOLLOW`` refuses to open a path that is a symlink at open time, closing
# the lstat->open window. It is present on every supported (POSIX) platform;
# the ``getattr`` fallback keeps the module importable elsewhere.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_READ_CHUNK = 1 << 16


@dataclass(frozen=True, slots=True)
class OkfOverlay:
    relative_path: Path
    selayer_id: str
    frontmatter: Mapping[str, object]
    sections: tuple[OkfSection, ...]


# ---------------------------------------------------------------------------
# Issue helpers (secret-safe: never echo raw link URLs)
# ---------------------------------------------------------------------------


def _issue(relative: str, message: str) -> OkfIssue:
    return OkfIssue(relative, message, severity="error", code="okf.composition")


def _frontmatter_issue(relative: str, field: str, message: str) -> OkfIssue:
    path = f"{relative}.frontmatter.{field}" if field else f"{relative}.frontmatter"
    return OkfIssue(path, message, severity="error", code="okf.composition")


def _link_issue(relative: str, message: str) -> OkfIssue:
    return OkfIssue(
        f"{relative}.links", message, severity="error", code="okf.composition.link"
    )


def _raise_if_errors(issues: list[OkfIssue]) -> None:
    if issues:
        ordered = tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
        raise OkfValidationError(ordered)


# ---------------------------------------------------------------------------
# Bounded, symlink-safe input walking
# ---------------------------------------------------------------------------


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _walk_inputs(root: Path) -> list[Path]:
    """Return sorted regular files under ``root`` within the size/count limits.

    The root itself is validated with ``lstat`` (never following a symlink): a
    root that is a symlink, special file, or non-directory is rejected before
    the walk begins. Each walked path is then checked for lexical containment
    and read only when it is a regular file. Symbolic links and special files
    are rejected before any content is read, and file count and byte totals are
    accumulated during the walk so a pathological input cannot exhaust memory.
    """
    try:
        info = root.lstat()
    except OSError as error:
        raise FileNotFoundError(f"input root does not exist: '{root}'") from error
    if stat_module.S_ISLNK(info.st_mode):
        raise FileExistsError(f"input root is a symbolic link: '{root}'")
    if not stat_module.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"input root is not a directory: '{root}'")
    candidates = sorted(root.rglob("*.md"), key=lambda path: path.as_posix())
    files: list[Path] = []
    total = 0
    for path in candidates:
        try:
            info = path.lstat()
        except OSError as error:
            raise OkfDocumentError(
                f"cannot stat '{_relative_posix(path, root)}'"
            ) from error
        if stat_module.S_ISLNK(info.st_mode):
            # Echo only the local input-relative path (never a URL secret); a
            # symlink is a structural file-system concern, not a link target.
            raise FileExistsError(
                f"symbolic link is not allowed: '{_relative_posix(path, root)}'"
            )
        # Lexical containment: rglob normalizes, so a real escape requires a
        # symlink (rejected above). The check is defensive belt-and-suspenders.
        try:
            path.relative_to(root)
        except ValueError as error:
            raise OkfDocumentError(
                f"path escapes input root: '{_relative_posix(path, root)}'"
            ) from error
        if not stat_module.S_ISREG(info.st_mode):
            raise OkfDocumentError(
                f"special file is not allowed: '{_relative_posix(path, root)}'"
            )
        size = info.st_size
        if size > _MAX_FILE_BYTES:
            raise OkfDocumentError(
                f"file exceeds {_MAX_FILE_BYTES} bytes: "
                f"'{_relative_posix(path, root)}'"
            )
        if len(files) + 1 > _MAX_FILES:
            raise OkfDocumentError(f"more than {_MAX_FILES} input files")
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise OkfDocumentError(
                f"total input exceeds {_MAX_TOTAL_BYTES} bytes"
            )
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# YAML frontmatter with duplicate-key detection
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(
    node: yaml.Node, _visited: set[int] | None = None
) -> None:
    """Recursively reject duplicate scalar keys and merge keys.

    The walk descends into nested mappings and sequences so a duplicate buried
    inside (for example) a list of sources is still detected, rather than only
    the top-level keys. YAML merge keys (``<<``) are rejected outright: merge
    resolution can silently mask a duplicate and introduce binding ambiguity,
    which defeats the purpose of duplicate-key detection.

    ``_visited`` guards against self-referential alias cycles in the composed
    node graph, which would otherwise make the recursion non-terminating.
    """
    if _visited is None:
        _visited = set()
    if not isinstance(node, yaml.CollectionNode):
        return
    if id(node) in _visited:
        return
    _visited.add(id(node))
    if isinstance(node, yaml.MappingNode):
        seen: set[Any] = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                if key_node.tag == _YAML_MERGE_TAG:
                    raise OkfDocumentError("YAML merge keys are not supported")
                key = key_node.value
                if key in seen:
                    # Fixed message: a key name is attacker-controlled and may
                    # carry a secret token, so it is never interpolated.
                    raise OkfDocumentError("duplicate frontmatter key")
                seen.add(key)
            else:
                # A complex (non-scalar) mapping key; recurse into it too.
                _reject_duplicate_keys(key_node, _visited)
            _reject_duplicate_keys(value_node, _visited)
    else:  # SequenceNode
        for item in node.value:
            _reject_duplicate_keys(item, _visited)


def _compose_frontmatter(text: str) -> dict[str, Any]:
    """Compose YAML frontmatter with recursive duplicate-key detection.

    All YAML parse and construction failures are wrapped into a single fixed,
    secret-safe message: the underlying error text from PyYAML can include
    source snippets, anchors, tags, or file paths, so it is never surfaced.
    A top-level non-mapping and any duplicate or merge key is rejected.
    """
    try:
        node = yaml.compose(text, Loader=cast(type[yaml.Loader], yaml.SafeLoader))
    except yaml.YAMLError:
        raise OkfDocumentError("invalid YAML frontmatter")
    if node is None:
        return {}
    if not isinstance(node, yaml.MappingNode):
        raise OkfDocumentError("frontmatter must be a mapping")
    _reject_duplicate_keys(node)
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        # A YAML that composes but fails construction (e.g. an explicit tag
        # SafeLoader cannot build) must not leak the constructor diagnostic.
        raise OkfDocumentError("invalid YAML frontmatter")
    if not isinstance(loaded, dict):
        raise OkfDocumentError("frontmatter must be a mapping")
    return loaded


def _safe_read_text(path: Path, root: Path, relative: str) -> str:
    """Read a trusted input file with an immediate re-validation of the lstat.

    ``_walk_inputs`` lstat-checks every entry during enumeration, but the file
    is read later. This closes that time-of-check/time-of-use window: the path
    is re-checked with ``lstat`` immediately before opening, the open refuses
    to follow a symlink (``O_NOFOLLOW``) so a regular file swapped for a
    symlink after the walk cannot be followed out of the root, and ``fstat``
    on the opened descriptor confirms it is still a regular file. Any detected
    replacement raises a safe domain error rather than reading
    attacker-controlled content. Lexical containment is re-checked as a
    defensive string-level guard.
    """
    try:
        path.relative_to(root)
    except ValueError as error:
        raise OkfDocumentError(f"path escapes input root: '{relative}'") from error
    try:
        info = path.lstat()
    except OSError as error:
        raise OkfDocumentError(f"cannot stat '{relative}'") from error
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
        raise OkfDocumentError(f"input file was replaced: '{relative}'")
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except OSError as error:
        # A symlink swapped in between the lstat above and the open surfaces
        # here (ELOOP under O_NOFOLLOW); never follow it.
        raise OkfDocumentError(f"input file was replaced: '{relative}'") from error
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise OkfDocumentError(f"input file was replaced: '{relative}'")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise OkfDocumentError(f"invalid UTF-8 in '{relative}'") from error


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def load_references(root: Path) -> Mapping[str, OkfConcept]:
    """Parse authored Reference documents as ordinary, strictly-validated concepts.

    ``root`` is the references directory. Concept relative paths are computed
    against ``root.parent`` (the future composed-bundle root) so a reference at
    ``<root>/guide.md`` composes at ``references/guide.md``. References must
    declare a non-empty ``type`` and ``title`` and must not bind a
    ``selayer_id``.
    """
    input_root = Path(root)
    files = _walk_inputs(input_root)
    base = input_root.parent
    concepts: dict[str, OkfConcept] = {}
    issues: list[OkfIssue] = []
    for path in files:
        relative = PurePosixPath(path.relative_to(base).as_posix())
        relative_posix = relative.as_posix()
        if path.name in _RESERVED_NAMES:
            issues.append(
                _issue(relative_posix, f"reserved path '{path.name}' is not allowed")
            )
            continue
        # Read once with an immediate re-validation of the walk's lstat
        # (TOCTOU-safe: O_NOFOLLOW + fstat refuse a symlink or file-type swap
        # made after the walk), then parse the concept from this in-memory text
        # so parse_concept never re-opens the path.
        text = _safe_read_text(path, input_root, relative_posix)
        # Duplicate-key-safe composition before parsing, so malformed or
        # duplicate-keyed frontmatter is reported with a fixed, secret-safe
        # message and never reaches parse_concept_text's source-bearing path
        # (which would otherwise forward PyYAML's diagnostic).
        frontmatter_match = _FRONTMATTER.match(text)
        if frontmatter_match is not None:
            try:
                _compose_frontmatter(frontmatter_match.group(1))
            except OkfDocumentError as error:
                issues.append(_issue(relative_posix, str(error)))
                continue
        try:
            concept = parse_concept_text(text, path, base)
        except OkfDocumentError as error:
            issues.append(_issue(relative_posix, str(error)))
            continue
        if len(concept.links) > _MAX_LINKS_PER_FILE:
            raise OkfDocumentError(
                f"more than {_MAX_LINKS_PER_FILE} links in '{relative_posix}'"
            )
        frontmatter = concept.frontmatter
        if "selayer_id" in frontmatter:
            issues.append(
                _frontmatter_issue(
                    relative_posix,
                    "selayer_id",
                    "references must not declare a selayer_id",
                )
            )
        if not _is_nonempty_string(frontmatter.get("type")):
            issues.append(
                _frontmatter_issue(
                    relative_posix, "type", "reference must declare a non-empty type"
                )
            )
        if not _is_nonempty_string(frontmatter.get("title")):
            issues.append(
                _frontmatter_issue(
                    relative_posix, "title", "reference must declare a non-empty title"
                )
            )
        issues.extend(validate_concept(concept, None, strict=True))
        concepts[relative_posix] = concept
    _raise_if_errors(issues)
    return MappingProxyType(dict(sorted(concepts.items())))


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------


def _parse_overlay(text: str) -> tuple[dict[str, Any], str, tuple[OkfSection, ...], list[str]]:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise OkfDocumentError("missing YAML frontmatter")
    frontmatter = _compose_frontmatter(match.group(1))
    body = text[match.end() :].lstrip("\n")
    preamble, sections = split_sections(body)
    links = _LINK.findall(body)
    return frontmatter, preamble, sections, links


def _classify_link(
    source: PurePosixPath, link: str
) -> tuple[str, PurePosixPath | None]:
    """Classify an internal link without echoing attacker-controlled text.

    Returns ``("path", target)`` for a normalizable internal concept link,
    ``("external", None)`` for URLs/fragments that are out of scope, and
    ``("broken", None)`` for links that escape the bundle root or cannot be
    parsed. ``urlsplit`` failures (which surface before scheme/query/fragment
    can be separated) are caught so a malformed URL cannot crash the loader.
    """
    split = _safe_urlsplit(link)
    if split is None:
        return "broken", None
    if split.scheme or split.netloc:
        return "external", None
    path_text = unquote(split.path)
    if not path_text:
        return "external", None
    if path_text.startswith("/"):
        normalized = posixpath.normpath(path_text.lstrip("/"))
    else:
        normalized = posixpath.normpath(
            posixpath.join(source.parent.as_posix(), path_text)
        )
    if normalized == ".." or normalized.startswith("../"):
        return "broken", None
    return "path", PurePosixPath(normalized)


def _validate_related_links(
    relative: PurePosixPath, sections: tuple[OkfSection, ...]
) -> list[OkfIssue]:
    related = next(
        (section for section in sections if section.title == _RELATED_CONCEPTS_SECTION),
        None,
    )
    if related is None:
        return []
    issues: list[OkfIssue] = []
    seen: set[PurePosixPath] = set()
    for link in _LINK.findall(related.content):
        kind, target = _classify_link(relative, link)
        if kind == "external":
            continue
        if kind == "broken":
            issues.append(
                _link_issue(relative.as_posix(), "Related Concepts link is broken")
            )
            continue
        target_path = cast(PurePosixPath, target)
        if target_path == relative:
            issues.append(
                _link_issue(relative.as_posix(), "self-link in Related Concepts")
            )
            continue
        if target_path in seen:
            issues.append(
                _link_issue(relative.as_posix(), "duplicate Related Concepts link")
            )
            continue
        seen.add(target_path)
    return issues


def load_overlays(root: Path, layer: SemanticLayer) -> tuple[OkfOverlay, ...]:
    """Parse and validate curated concept overlays under ``root``.

    ``root`` is the overlays directory, which mirrors the composed-bundle
    semantic-kind layout (e.g. ``metrics/gross_margin.md``). Each overlay must
    bind a catalog-resolvable ``selayer_id`` whose generated concept path
    exactly matches its relative path, must use only the allowed frontmatter
    fields (``selayer_id``, ``sources``, ``stale_after``) and section headings,
    and must not carry generator-owned content (Catalog Definition, generated
    metadata, ``verified``, ``title``, ...). ``sources`` and ``stale_after``
    reuse the existing OKF field validators via a temporary concept.
    """
    input_root = Path(root)
    files = _walk_inputs(input_root)
    overlays: list[OkfOverlay] = []
    issues: list[OkfIssue] = []
    all_ids: list[tuple[str, str]] = []
    for path in files:
        relative = PurePosixPath(path.relative_to(input_root).as_posix())
        relative_posix = relative.as_posix()
        if path.name in _RESERVED_NAMES:
            issues.append(
                _issue(relative_posix, f"reserved path '{path.name}' is not allowed")
            )
            continue
        text = _safe_read_text(path, input_root, relative_posix)
        try:
            frontmatter, preamble, sections, links = _parse_overlay(text)
        except OkfDocumentError as error:
            issues.append(_issue(relative_posix, str(error)))
            continue
        if len(links) > _MAX_LINKS_PER_FILE:
            raise OkfDocumentError(
                f"more than {_MAX_LINKS_PER_FILE} links in '{relative_posix}'"
            )

        extra = set(frontmatter) - _ALLOWED_OVERLAY_FIELDS
        if extra:
            issues.append(
                _frontmatter_issue(
                    relative_posix,
                    "",
                    "overlay frontmatter allows only selayer_id, sources, "
                    "and stale_after",
                )
            )

        selayer_id = frontmatter.get("selayer_id")
        valid_id = False
        if not _is_nonempty_string(selayer_id):
            issues.append(
                _frontmatter_issue(
                    relative_posix, "selayer_id", "overlay must declare a selayer_id"
                )
            )
        elif _SELAYER_ID.fullmatch(cast(str, selayer_id)) is None:
            issues.append(
                _frontmatter_issue(
                    relative_posix,
                    "selayer_id",
                    "selayer_id must use a canonical semantic kind and local name",
                )
            )
        else:
            identifier = cast(str, selayer_id)
            # Track every valid-format identifier for duplicate detection,
            # independent of path validation (reported separately). A list
            # (not a dict keyed by id) preserves repeated IDs so a true
            # duplicate-ID diagnostic is emitted even when paths mismatch.
            all_ids.append((identifier, relative_posix))
            valid_id = _validate_bound_overlay(
                identifier,
                layer,
                relative,
                relative_posix,
                frontmatter,
                sections,
                issues,
            )

        if preamble:
            issues.append(
                _issue(relative_posix, "text before the first section is not allowed")
            )

        seen_titles: set[str] = set()
        for section in sections:
            if section.title not in _ALLOWED_OVERLAY_SECTIONS:
                issues.append(
                    _issue(relative_posix, f"disallowed section '{section.title}'")
                )
            if section.title in seen_titles:
                issues.append(
                    _issue(relative_posix, f"duplicate section '{section.title}'")
                )
            seen_titles.add(section.title)

        issues.extend(_validate_related_links(relative, sections))

        if valid_id:
            identifier = cast(str, selayer_id)
            try:
                frozen_frontmatter = _freeze(frontmatter)
            except OkfMetadataError as error:
                issues.append(_issue(relative_posix, str(error)))
                continue
            overlays.append(
                OkfOverlay(
                    relative_path=Path(relative.as_posix()),
                    selayer_id=identifier,
                    frontmatter=frozen_frontmatter,
                    sections=sections,
                )
            )

    _report_duplicate_ids(all_ids, issues)
    _raise_if_errors(issues)
    return tuple(
        sorted(overlays, key=lambda overlay: overlay.relative_path.as_posix())
    )


def _validate_bound_overlay(
    identifier: str,
    layer: SemanticLayer,
    relative: PurePosixPath,
    relative_posix: str,
    frontmatter: Mapping[str, Any],
    sections: tuple[OkfSection, ...],
    issues: list[OkfIssue],
) -> bool:
    """Validate a format-valid, bound overlay; return whether it is buildable."""
    kind, name = identifier.split(".", 1)
    try:
        layer.resolve(identifier)
    except KeyError:
        issues.append(
            _frontmatter_issue(
                relative_posix,
                "selayer_id",
                f"unknown selayer_id '{identifier}'",
            )
        )
    expected = concept_path(identifier)
    if relative != expected:
        issues.append(
            _issue(
                relative_posix,
                f"path must be '{expected.as_posix()}' for selayer_id '{identifier}'",
            )
        )
    # Reuse the existing OKF field validators for sources/stale_after by
    # constructing a temporary concept whose type/title/selayer_id are
    # guaranteed valid, so only sources/stale_after defects surface.
    temp_frontmatter: dict[str, Any] = {
        "type": _KIND_TYPES[kind],
        "title": display_title(name),
        "selayer_id": identifier,
    }
    if "sources" in frontmatter:
        temp_frontmatter["sources"] = frontmatter["sources"]
    if "stale_after" in frontmatter:
        temp_frontmatter["stale_after"] = frontmatter["stale_after"]
    temp_concept = OkfConcept.create(
        concept_id=relative.with_suffix("").as_posix(),
        relative_path=relative,
        frontmatter=temp_frontmatter,
        sections=sections,
        links=(),
    )
    issues.extend(validate_concept(temp_concept, None, strict=True))
    return True


def _report_duplicate_ids(
    all_ids: list[tuple[str, str]], issues: list[OkfIssue]
) -> None:
    """Emit a duplicate-ID diagnostic for every repeated overlay identifier.

    ``all_ids`` carries every valid-format selayer_id alongside its path, so a
    repeated identifier is detected even when its paths also mismatch (the
    duplicate binding is the defect, independent of path validation). Each
    occurrence of a duplicated identifier is reported.
    """
    by_id: dict[str, list[str]] = {}
    for identifier, relative_posix in all_ids:
        by_id.setdefault(identifier, []).append(relative_posix)
    for identifier in sorted(by_id):
        paths = by_id[identifier]
        if len(paths) > 1:
            for relative_posix in sorted(paths):
                issues.append(
                    _frontmatter_issue(
                        relative_posix,
                        "selayer_id",
                        f"duplicate overlay selayer_id '{identifier}'",
                    )
                )


__all__ = ["OkfOverlay", "load_overlays", "load_references"]
