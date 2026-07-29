"""Non-mutating effective-value helpers for OKF consumer compatibility."""

from __future__ import annotations

from collections.abc import Mapping

from .model import OkfConcept

__all__ = ["effective_generated_at"]


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
