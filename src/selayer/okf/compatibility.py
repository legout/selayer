"""Non-mutating effective-value helpers for OKF consumer compatibility."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from .model import OkfConcept

_CITATIONS_SECTION = "Citations"
_LIST_ITEM = re.compile(r"^[-*][ \t]+(.+)$")
_MARKDOWN_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]*)\)$")

__all__ = ["effective_generated_at", "effective_sources"]


def effective_generated_at(concept: OkfConcept) -> object | None:
    """Return the effective generation timestamp without mutating the concept.

    Precedence:
      1. ``generated.at`` when ``generated`` is a mapping that carries ``at``;
      2. top-level ``timestamp`` when ``generated`` is absent;
      3. ``None`` otherwise.

    A present-but-malformed ``generated`` field never falls back to the legacy
    ``timestamp``: explicit v0.2 metadata always wins. The frozen frontmatter
    value is returned unchanged.
    """
    frontmatter = concept.frontmatter
    if "generated" in frontmatter:
        generated = frontmatter["generated"]
        if isinstance(generated, Mapping) and "at" in generated:
            return generated["at"]
        return None
    if "timestamp" in frontmatter:
        return frontmatter["timestamp"]
    return None


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_frontmatter_sources(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        source
        for source in value
        if isinstance(source, Mapping) and _is_nonempty_string(source.get("resource"))
    )


def _citations_section(concept: OkfConcept) -> str | None:
    for section in concept.sections:
        if section.title == _CITATIONS_SECTION:
            return section.content
    return None


def _parse_citations(content: str) -> tuple[Mapping[str, object], ...]:
    sources: list[Mapping[str, object]] = []
    for line in content.splitlines():
        match = _LIST_ITEM.match(line)
        if match is None:
            continue
        text = match.group(1).strip()
        if not text:
            continue
        link = _MARKDOWN_LINK.match(text)
        if link is not None:
            resource = link.group(2)
            if not resource:
                continue
            sources.append(
                MappingProxyType({"title": link.group(1), "resource": resource})
            )
        else:
            sources.append(MappingProxyType({"resource": text}))
    return tuple(sources)


def effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]:
    """Return effective source mappings without mutating the concept.

    Precedence:
      1. valid entries from frontmatter ``sources`` when the key is present;
      2. entries parsed from an exact ``# Citations`` section when ``sources``
         is absent;
      3. an empty tuple otherwise.

    Frontmatter entries that are not mappings or lack a non-empty ``resource``
    are omitted so malformed optional metadata cannot crash later retrieval.
    A legacy ``# Citations`` section is an unordered Markdown list whose
    ``-``/``*`` items become ``{"title", "resource"}`` (Markdown links) or
    ``{"resource"}`` (plain) mappings. Indented/nested items, non-list prose,
    empty items, and empty-resource links are ignored. Source order follows
    body order and duplicate resources are preserved.
    """
    frontmatter = concept.frontmatter
    if "sources" in frontmatter:
        return _valid_frontmatter_sources(frontmatter["sources"])
    citations = _citations_section(concept)
    if citations is None:
        return ()
    return _parse_citations(citations)
