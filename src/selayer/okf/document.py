from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .model import OkfConcept, OkfMetadataError, OkfSection

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_HEADING = re.compile(r"^# ([^#].*)$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,}).*$")
_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


class OkfDocumentError(ValueError):
    pass


def parse_concept(path: Path, root: Path) -> OkfConcept:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = _FRONTMATTER.match(text)
    if match is None:
        raise OkfDocumentError("missing YAML frontmatter")
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise OkfDocumentError(f"invalid YAML frontmatter: {error}") from error
    if not isinstance(loaded, dict):
        raise OkfDocumentError("frontmatter must be a mapping")
    body = text[match.end() :].lstrip("\n")
    preamble, sections = split_sections(body)
    relative_path = PurePosixPath(path.relative_to(root).as_posix())
    try:
        return OkfConcept.create(
            concept_id=relative_path.with_suffix("").as_posix(),
            relative_path=relative_path,
            frontmatter=loaded,
            preamble=preamble,
            sections=sections,
            links=tuple(_LINK.findall(body)),
        )
    except OkfMetadataError as error:
        raise OkfDocumentError("cyclic YAML frontmatter is not supported") from error


def _fence_marker(line: str) -> str | None:
    match = _FENCE_OPEN.match(line)
    return match.group(1) if match is not None else None


def _closes_fence(line: str, marker: str) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith(marker[0]):
        return False
    run_length = len(stripped) - len(stripped.lstrip(marker[0]))
    return run_length >= len(marker) and not stripped[run_length:].strip()


def split_sections(body: str) -> tuple[str, tuple[OkfSection, ...]]:
    preamble: list[str] = []
    sections: list[OkfSection] = []
    title: str | None = None
    content: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        if fence is None:
            fence = _fence_marker(line)
            heading = None if fence is not None else _HEADING.match(line)
        else:
            if _closes_fence(line, fence):
                fence = None
            heading = None
        if heading is not None:
            if title is None:
                preamble = content
            else:
                sections.append(OkfSection(title, "\n".join(content).strip()))
            title = heading.group(1).strip()
            content = []
        else:
            content.append(line)
    if title is None:
        preamble = content
    else:
        sections.append(OkfSection(title, "\n".join(content).strip()))
    return "\n".join(preamble).strip(), tuple(sections)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw(item) for item in value}
    return value


def render_concept(concept: OkfConcept) -> str:
    dumped = yaml.safe_dump(
        _thaw(concept.frontmatter),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if dumped is None:
        raise AssertionError("YAML string serialization returned no content")
    frontmatter = dumped.rstrip()
    parts = [f"---\n{frontmatter}\n---"]
    if concept.preamble:
        parts.append(concept.preamble.rstrip())
    parts.extend(
        f"# {section.title}\n\n{section.content.rstrip()}".rstrip()
        for section in concept.sections
    )
    return "\n\n".join(parts).rstrip() + "\n"


__all__ = [
    "OkfDocumentError",
    "parse_concept",
    "render_concept",
    "split_sections",
]
