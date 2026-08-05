"""Read-only knowledge-provider protocol and the filesystem OKF adapter.

This module owns the Stage 2 external-knowledge foundation for the discovery
companion package:

* :class:`KnowledgeProvider` — a structural protocol with two read-only
  operations (``search`` and ``get``) over immutable, namespaced results.
* Immutable request/result value types (:class:`KnowledgeSearchRequest`,
  :class:`KnowledgeHit`, :class:`KnowledgeGetRequest`, :class:`KnowledgeDocument`).
* :class:`ProviderRegistry` — loads provider *types* from the
  ``selayer_discovery.knowledge_providers`` entry-point group, rejects duplicate
  provider names, supports zero or more configured providers, and dispatches
  search/get across them while enforcing global caps and sanitizing failures.
* :class:`FilesystemOkfProvider` — a read-only adapter backed by core
  :class:`selayer.okf.OkfBundle` that derives revisions from the strict concept
  hash and preserves effective source attribution.

Design rules enforced here (see the approved discovery design):

* Providers are read-only. No provider exposes a write method, and this module
  performs no subprocess, Git, model, or network I/O.
* Provider resource IDs are always namespaced (``<provider-name>:<local-id>``)
  so two providers may expose the same local id without colliding.
* Revisions are immutable content hashes derived from the strict bundle/concept
  text. Identical content yields an identical revision; changed content yields a
  new one.
* Provider configuration stores only non-secret options and environment-variable
  references. Resolved credentials are never persisted or exposed.
* Search and get enforce item and byte caps *before* results are returned.
* Every provider failure (malformed output, timeout, or arbitrary exception)
  surfaces as a sanitized :class:`KnowledgeError` that never echoes a raw cause,
  secret, credential, or document body. The optional ``context`` is stored
  privately and never rendered.
* Provider content is untrusted evidence. Retrieving it never mutates session
  state, invokes a tool, or triggers a CLI action.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

__all__ = [
    "CODE_KNOWLEDGE_DUPLICATE_PROVIDER",
    "CODE_KNOWLEDGE_INVALID_CAP",
    "CODE_KNOWLEDGE_INVALID_OUTPUT",
    "CODE_KNOWLEDGE_INVALID_RESOURCE",
    "CODE_KNOWLEDGE_PROVIDER_FAILED",
    "CODE_KNOWLEDGE_PROVIDER_TIMEOUT",
    "CODE_KNOWLEDGE_PROVIDER_UNKNOWN",
    "CODE_KNOWLEDGE_TOO_LARGE",
    "CODE_KNOWLEDGE_TOO_MANY",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_HITS",
    "DEFAULT_MAX_RESULT_BYTES",
    "ENTRY_POINT_GROUP",
    "FilesystemOkfProvider",
    "KnowledgeDocument",
    "KnowledgeError",
    "KnowledgeGetRequest",
    "KnowledgeHit",
    "KnowledgeProvider",
    "KnowledgeSearchRequest",
    "ProviderRegistry",
]

# --------------------------------------------------------------------------- #
# Constants and bounds                                                        #
# --------------------------------------------------------------------------- #

#: Entry-point group that declares installable provider *types*. Only providers
#: declared in this group may be configured; executable paths and subprocesses
#: are never supported.
ENTRY_POINT_GROUP: str = "selayer_discovery.knowledge_providers"

#: Default maximum number of hits returned by a search (per provider and in the
#: registry's aggregated result).
DEFAULT_MAX_HITS: int = 25

#: Default cumulative byte bound for a search result (summaries + titles).
DEFAULT_MAX_RESULT_BYTES: int = 256 * 1024

#: Default byte bound for a single retrieved document's normalized text.
DEFAULT_MAX_DOCUMENT_BYTES: int = 2 * 1024 * 1024

#: Markdown media type for OKF concept documents.
_MEDIA_MARKDOWN: str = "text/markdown"

# Stable knowledge diagnostic codes (rendered; never leak raw causes).
CODE_KNOWLEDGE_INVALID_CAP: str = "discovery.knowledge.invalid_cap"
CODE_KNOWLEDGE_INVALID_RESOURCE: str = "discovery.knowledge.invalid_resource"
CODE_KNOWLEDGE_INVALID_OUTPUT: str = "discovery.knowledge.invalid_output"
CODE_KNOWLEDGE_TOO_MANY: str = "discovery.knowledge.too_many"
CODE_KNOWLEDGE_TOO_LARGE: str = "discovery.knowledge.too_large"
CODE_KNOWLEDGE_DUPLICATE_PROVIDER: str = "discovery.knowledge.duplicate_provider"
CODE_KNOWLEDGE_PROVIDER_UNKNOWN: str = "discovery.knowledge.provider_unknown"
CODE_KNOWLEDGE_PROVIDER_FAILED: str = "discovery.knowledge.provider_failed"
CODE_KNOWLEDGE_PROVIDER_TIMEOUT: str = "discovery.knowledge.provider_timeout"

#: Stable identifier shape for provider names and namespaced resource ids.
_PROVIDER_NAME_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")

#: Upper bound on a hit summary / title rendered in output and diagnostics.
_MAX_SUMMARY_CHARS: int = 4 * 1024


# --------------------------------------------------------------------------- #
# Sanitized knowledge diagnostics                                             #
# --------------------------------------------------------------------------- #


def _safe_name(value: object) -> str:
    """Return ``value`` only if it is a stable provider-name-shaped string."""

    if type(value) is str and _PROVIDER_NAME_RE.match(value) is not None:
        return value
    return "<provider>"


class KnowledgeError(Exception):
    """Sanitized knowledge diagnostic exception.

    Only a stable ``code`` and validated ``safe_provider``/``safe_resource``
    identifiers are ever rendered by ``__str__``, ``__repr__``, or
    :meth:`to_dict`. Raw causes are never chained or surfaced. The optional
    ``context`` is stored privately (``_context``) and never rendered; it may
    carry secrets, tokens, or document bodies for internal use only.
    """

    def __init__(
        self,
        code: str,
        *,
        safe_provider: str | None = None,
        safe_resource: str | None = None,
        context: object = None,
    ) -> None:
        self.code: str = code
        self.safe_provider: str | None = _safe_name(safe_provider)
        self.safe_resource: str | None = (
            safe_resource
            if type(safe_resource) is str and len(safe_resource) <= 256
            else None
        )
        # Stored privately and never rendered. May carry secrets, tokens, or
        # document bodies for internal logging only.
        self._context: object = context
        super().__init__(self._render())

    def _render(self) -> str:
        parts: list[str] = [self.code]
        if self.safe_provider is not None:
            parts.append(f"provider={self.safe_provider}")
        if self.safe_resource is not None:
            parts.append(f"resource={self.safe_resource}")
        return " ".join(parts)

    def __str__(self) -> str:
        return self._render()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"safe_provider={self.safe_provider!r}, "
            f"safe_resource={self.safe_resource!r})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping containing only safe fields."""

        result: dict[str, object] = {"code": self.code}
        if self.safe_provider is not None:
            result["safe_provider"] = self.safe_provider
        if self.safe_resource is not None:
            result["safe_resource"] = self.safe_resource
        return result


# --------------------------------------------------------------------------- #
# Immutable request / result value types                                      #
# --------------------------------------------------------------------------- #


def _require_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise KnowledgeError(CODE_KNOWLEDGE_INVALID_CAP, context=name)
    return value


@dataclass(frozen=True, slots=True)
class KnowledgeSearchRequest:
    """An immutable, bounded knowledge search request."""

    query: str
    max_items: int = DEFAULT_MAX_HITS
    max_bytes: int = DEFAULT_MAX_RESULT_BYTES

    def __post_init__(self) -> None:
        if type(self.query) is not str:
            raise KnowledgeError(CODE_KNOWLEDGE_INVALID_CAP)
        _require_positive_int(self.max_items, name="max_items")
        _require_positive_int(self.max_bytes, name="max_bytes")


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """An immutable, namespaced knowledge search hit.

    ``resource_id`` is always namespaced as ``<provider-name>:<local-id>``.
    ``revision`` is an immutable content hash. ``source_attribution`` is the
    effective source location (e.g. a bundle-relative path). The hit never
    carries a document body.
    """

    provider_name: str
    resource_id: str
    revision: str
    title: str
    media_type: str
    summary: str
    source_attribution: str
    size: int

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_name": self.provider_name,
            "resource_id": self.resource_id,
            "revision": self.revision,
            "title": self.title,
            "media_type": self.media_type,
            "summary": self.summary,
            "source_attribution": self.source_attribution,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeGetRequest:
    """An immutable, bounded knowledge retrieval request."""

    resource_id: str
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES

    def __post_init__(self) -> None:
        if type(self.resource_id) is not str:
            raise KnowledgeError(CODE_KNOWLEDGE_INVALID_RESOURCE)
        _require_positive_int(self.max_bytes, name="max_bytes")


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """An immutable, namespaced, bounded knowledge document.

    ``text`` is normalized, untrusted evidence text bounded by the request's
    ``max_bytes``. ``revision`` and ``content_hash`` are immutable content
    hashes of the rendered source text.
    """

    provider_name: str
    resource_id: str
    revision: str
    title: str
    media_type: str
    text: str
    size: int
    content_hash: str
    source_attribution: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_name": self.provider_name,
            "resource_id": self.resource_id,
            "revision": self.revision,
            "title": self.title,
            "media_type": self.media_type,
            "size": self.size,
            "content_hash": self.content_hash,
            "source_attribution": self.source_attribution,
        }


# --------------------------------------------------------------------------- #
# Provider protocol                                                           #
# --------------------------------------------------------------------------- #


@runtime_checkable
class KnowledgeProvider(Protocol):
    """A read-only knowledge provider.

    Implementations are installed provider *types* declared in the
    :data:`ENTRY_POINT_GROUP` entry-point group and instantiated with non-secret
    options. They expose two read-only operations and no write method.
    """

    @property
    def name(self) -> str:  # pragma: no cover - protocol shape
        ...

    def search(self, request: KnowledgeSearchRequest) -> tuple[KnowledgeHit, ...]:
        """Return bounded, namespaced hits matching ``request.query``."""
        ...

    def get(self, request: KnowledgeGetRequest) -> KnowledgeDocument:
        """Return the bounded document for the namespaced ``request.resource_id``."""
        ...


# --------------------------------------------------------------------------- #
# Output validation                                                           #
# --------------------------------------------------------------------------- #


def _split_namespaced_id(resource_id: object) -> tuple[str, str] | None:
    """Split a namespaced resource id into ``(provider_name, local_id)``.

    Returns ``None`` when the id is not a string, lacks a single namespace
    separator, or the provider name is not a stable identifier. Callers decide
    which diagnostic code to raise for a malformed id.
    """

    if type(resource_id) is not str:
        return None
    if resource_id.count(":") != 1:
        return None
    name, _, local = resource_id.partition(":")
    if not name or not local:
        return None
    if _PROVIDER_NAME_RE.match(name) is None:
        return None
    if len(local) > 256:
        return None
    return name, local


def _parse_namespaced_id(resource_id: object) -> tuple[str, str]:
    """Split a caller-supplied namespaced resource id.

    Raises :class:`KnowledgeError` (``invalid_resource``) for a malformed id;
    a caller supplies a resource id, so a malformed one is an invalid request,
    not invalid provider output.
    """

    split = _split_namespaced_id(resource_id)
    if split is None:
        raise KnowledgeError(CODE_KNOWLEDGE_INVALID_RESOURCE) from None
    return split


def _validate_hit(hit: object, *, expected_provider: str) -> KnowledgeHit:
    """Validate and return a provider-supplied :class:`KnowledgeHit`.

    Rejects non-:class:`KnowledgeHit` values, malformed namespaced ids,
    non-hex revisions, and hits whose ``provider_name`` does not match the
    registering provider (a provider must not impersonate another namespace).
    """

    if not isinstance(hit, KnowledgeHit):
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    # A malformed namespaced id in *provider output* is invalid output, not an
    # invalid caller request, so it surfaces as ``invalid_output``.
    split = _split_namespaced_id(hit.resource_id)
    if split is None:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    name, _ = split
    if name != expected_provider or name != hit.provider_name:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    if type(hit.revision) is not str or _HEX64_RE.match(hit.revision) is None:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    if type(hit.media_type) is not str or not hit.media_type:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    if type(hit.size) is not int or hit.size < 0:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    return hit


def _validate_document(
    document: object, *, expected_provider: str, expected_resource: str | None
) -> KnowledgeDocument:
    """Validate and return a provider-supplied :class:`KnowledgeDocument`."""

    if not isinstance(document, KnowledgeDocument):
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    split = _split_namespaced_id(document.resource_id)
    if split is None:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    name, _ = split
    if name != expected_provider or name != document.provider_name:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    if (
        type(document.revision) is not str
        or _HEX64_RE.match(document.revision) is None
        or type(document.content_hash) is not str
        or _HEX64_RE.match(document.content_hash) is None
    ):
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    if expected_resource is not None and document.resource_id != expected_resource:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    if type(document.size) is not int or document.size < 0:
        raise KnowledgeError(
            CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=expected_provider
        ) from None
    return document


# --------------------------------------------------------------------------- #
# Provider registry                                                           #
# --------------------------------------------------------------------------- #


@dataclass
class _RegistryState:
    """Mutable registry state (rebuilt on every mutation)."""

    providers: dict[str, KnowledgeProvider] = field(default_factory=dict)


class ProviderRegistry:
    """Registry of configured read-only knowledge providers.

    Provider *types* are discovered from the :data:`ENTRY_POINT_GROUP`
    entry-point group via :meth:`discover_types`. Configured provider
    *instances* are added via :meth:`add`, which rejects duplicate names and
    validates the name shape. A registry supports zero providers (search returns
    an empty tuple) and any number of providers (resource ids are namespaced so
    two providers may expose the same local id without colliding).

    All provider failures — malformed output, timeouts, or arbitrary exceptions
    — are caught and re-raised as sanitized :class:`KnowledgeError` values that
    never echo a raw cause, secret, or credential.
    """

    def __init__(
        self,
        providers: Mapping[str, KnowledgeProvider] | None = None,
    ) -> None:
        self._state = _RegistryState()
        if providers is not None:
            for name, provider in providers.items():
                self.add(name, provider)

    # -- type discovery ----------------------------------------------------- #

    @classmethod
    def discover_types(cls) -> dict[str, type[KnowledgeProvider]]:
        """Return the provider *types* declared in the entry-point group.

        Only entry points in :data:`ENTRY_POINT_GROUP` are considered; the
        resolved target must be a class. A dangling or unimportable entry point
        is skipped (it never registers a usable type).
        """

        types: dict[str, type[KnowledgeProvider]] = {}
        try:
            entries = entry_points(group=ENTRY_POINT_GROUP)
        except Exception:  # noqa: BLE001 - metadata backend
            return types
        for entry in entries:
            try:
                resolved = entry.load()
            except Exception:  # noqa: BLE001, S112 - skip dangling points
                # A dangling or unimportable entry point is ignored rather than
                # crashing the whole registry; it cannot be configured.
                continue
            if isinstance(resolved, type):
                types[entry.name] = cast("type[KnowledgeProvider]", resolved)
        return types

    # -- registration ------------------------------------------------------- #

    def add(self, name: str, provider: KnowledgeProvider) -> None:
        """Register ``provider`` under ``name`` (rejects duplicates/bad names)."""

        if type(name) is not str or _PROVIDER_NAME_RE.match(name) is None:
            raise KnowledgeError(CODE_KNOWLEDGE_INVALID_RESOURCE, safe_provider=name)
        if name in self._state.providers:
            raise KnowledgeError(
                CODE_KNOWLEDGE_DUPLICATE_PROVIDER, safe_provider=name
            )
        self._state.providers[name] = provider

    def get(self, name: str) -> KnowledgeProvider:
        """Return the provider registered under ``name``."""

        provider = self._state.providers.get(name)
        if provider is None:
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_UNKNOWN, safe_provider=name
            ) from None
        return provider

    def names(self) -> tuple[str, ...]:
        """Return the registered provider names in sorted order."""

        return tuple(sorted(self._state.providers))

    def __len__(self) -> int:
        return len(self._state.providers)

    def __contains__(self, name: object) -> bool:
        return type(name) is str and name in self._state.providers

    # -- dispatch ----------------------------------------------------------- #

    def search(self, request: KnowledgeSearchRequest) -> tuple[KnowledgeHit, ...]:
        """Aggregate bounded, validated hits from every registered provider.

        Enforces the global ``max_items`` and ``max_bytes`` caps across the
        aggregated result by truncation and sanitizes every provider failure.
        """

        capped: list[KnowledgeHit] = []
        cumulative_bytes = 0
        for name in self.names():
            provider = self._state.providers[name]
            hits = self._safe_search(provider, name, request)
            for hit in hits:
                if len(capped) >= request.max_items:
                    return tuple(capped)
                validated = _validate_hit(hit, expected_provider=name)
                contribution = self._hit_bytes(validated)
                if cumulative_bytes + contribution > request.max_bytes:
                    return tuple(capped)
                capped.append(validated)
                cumulative_bytes += contribution
        return tuple(capped)

    def get_document(self, resource_id: str) -> KnowledgeDocument:
        """Dispatch a namespaced resource id to its provider and validate it."""

        name, _ = _parse_namespaced_id(resource_id)
        provider = self._state.providers.get(name)
        if provider is None:
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_UNKNOWN,
                safe_provider=name,
                safe_resource=resource_id,
            ) from None
        request = KnowledgeGetRequest(resource_id=resource_id)
        document = self._safe_get(provider, name, request)
        return _validate_document(
            document, expected_provider=name, expected_resource=resource_id
        )

    # -- sanitized provider invocation -------------------------------------- #

    @staticmethod
    def _hit_bytes(hit: KnowledgeHit) -> int:
        """Approximate cumulative byte contribution of a hit's rendered fields."""

        return len(hit.title.encode("utf-8")) + len(hit.summary.encode("utf-8"))

    @staticmethod
    def _safe_search(
        provider: KnowledgeProvider,
        name: str,
        request: KnowledgeSearchRequest,
    ) -> tuple[KnowledgeHit, ...]:
        try:
            result = provider.search(request)
        except KnowledgeError:
            raise
        except TimeoutError:
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_TIMEOUT, safe_provider=name
            ) from None
        except Exception:  # noqa: BLE001 - sanitize any provider failure
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_FAILED, safe_provider=name
            ) from None
        if not isinstance(result, tuple):
            raise KnowledgeError(
                CODE_KNOWLEDGE_INVALID_OUTPUT, safe_provider=name
            ) from None
        return result

    @staticmethod
    def _safe_get(
        provider: KnowledgeProvider,
        name: str,
        request: KnowledgeGetRequest,
    ) -> KnowledgeDocument:
        try:
            result = provider.get(request)
        except KnowledgeError:
            raise
        except TimeoutError:
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_TIMEOUT,
                safe_provider=name,
                safe_resource=request.resource_id,
            ) from None
        except Exception:  # noqa: BLE001 - sanitize any provider failure
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_FAILED,
                safe_provider=name,
                safe_resource=request.resource_id,
            ) from None
        return result


# --------------------------------------------------------------------------- #
# Filesystem OKF provider                                                     #
# --------------------------------------------------------------------------- #


def _okf_concepts(bundle: object) -> Mapping[str, object]:
    """Return the concept mapping from a loaded ``OkfBundle``."""

    concepts = getattr(bundle, "concepts", None)
    if not isinstance(concepts, Mapping):
        raise KnowledgeError(CODE_KNOWLEDGE_PROVIDER_FAILED) from None
    return concepts


def _concept_semantic_id(concept: object) -> str | None:
    """Return the ``selayer_id`` frontmatter value if it is a non-empty string."""

    frontmatter = getattr(concept, "frontmatter", None)
    if not isinstance(frontmatter, Mapping):
        return None
    value = frontmatter.get("selayer_id")
    if type(value) is not str or not value:
        return None
    return value


def _concept_title(concept: object) -> str:
    frontmatter = getattr(concept, "frontmatter", None)
    if isinstance(frontmatter, Mapping):
        title = frontmatter.get("title")
        if type(title) is str and title:
            return title
    semantic = _concept_semantic_id(concept)
    return semantic if semantic is not None else "Untitled"


def _concept_summary(concept: object) -> str:
    """Return a bounded summary derived from the concept description/preamble."""

    parts: list[str] = []
    frontmatter = getattr(concept, "frontmatter", None)
    if isinstance(frontmatter, Mapping):
        description = frontmatter.get("description")
        if type(description) is str and description:
            parts.append(description)
    preamble = getattr(concept, "preamble", "")
    if type(preamble) is str and preamble:
        parts.append(preamble)
    summary = " ".join(parts).strip()
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS]
    return summary


def _concept_source_attribution(concept: object) -> str:
    """Return the effective bundle-relative source path for a concept."""

    relative = getattr(concept, "relative_path", None)
    if relative is not None:
        text = getattr(relative, "as_posix", None)
        if callable(text):
            path = text()
            if type(path) is str and path:
                return path
        if type(relative) is str and relative:
            return relative
    return "<okf-concept>"


def _matches_query(query: str, concept: object) -> bool:
    """Return ``True`` when the (possibly empty) query matches a concept.

    An empty query matches every concept (the caller bounds the result set). A
    non-empty query matches case-insensitively against the title, semantic id,
    description, and preamble.
    """

    query = query.strip().lower()
    if not query:
        return True
    haystack = " ".join(
        [
            _concept_title(concept),
            _concept_semantic_id(concept) or "",
            _concept_summary(concept),
        ]
    ).lower()
    return query in haystack


class FilesystemOkfProvider:
    """A read-only filesystem OKF v0.2+ knowledge provider.

    Backed by core :class:`selayer.okf.OkfBundle`. Loads the bundle in strict
    mode on every call so a changed directory yields new immutable revisions.
    Each concept is exposed under its provider-namespaced semantic identifier
    (``<name>:<selayer_id>``). The revision and content hash are the SHA-256 of
    the rendered concept text, so identical content yields an identical revision
    and changed content yields a new one. Effective source attribution is the
    bundle-relative concept path.

    The adapter is strictly read-only: it exposes only ``search``/``get`` and a
    ``name`` property, and performs no subprocess, Git, model, or network I/O.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        name: str = "okf-filesystem",
    ) -> None:
        if type(name) is not str or _PROVIDER_NAME_RE.match(name) is None:
            raise KnowledgeError(CODE_KNOWLEDGE_INVALID_RESOURCE, safe_provider=name)
        self._name = name
        self._root = Path(root)

    @property
    def name(self) -> str:
        return self._name

    # -- public read-only operations ---------------------------------------- #

    def search(self, request: KnowledgeSearchRequest) -> tuple[KnowledgeHit, ...]:
        concepts = self._load_concepts()
        hits: list[KnowledgeHit] = []
        cumulative_bytes = 0
        for concept_id in sorted(concepts):
            concept = concepts[concept_id]
            semantic_id = _concept_semantic_id(concept)
            if semantic_id is None:
                continue
            if not _matches_query(request.query, concept):
                continue
            resource_id = f"{self._name}:{semantic_id}"
            revision = self._concept_revision(concept)
            title = _concept_title(concept)
            summary = _concept_summary(concept)
            attribution = _concept_source_attribution(concept)
            size = self._concept_size(concept)
            contribution = len(title.encode("utf-8")) + len(summary.encode("utf-8"))
            if len(hits) >= request.max_items:
                break
            if cumulative_bytes + contribution > request.max_bytes:
                break
            hits.append(
                KnowledgeHit(
                    provider_name=self._name,
                    resource_id=resource_id,
                    revision=revision,
                    title=title,
                    media_type=_MEDIA_MARKDOWN,
                    summary=summary,
                    source_attribution=attribution,
                    size=size,
                )
            )
            cumulative_bytes += contribution
        return tuple(hits)

    def get(self, request: KnowledgeGetRequest) -> KnowledgeDocument:
        name, local = _parse_namespaced_id(request.resource_id)
        if name != self._name:
            raise KnowledgeError(
                CODE_KNOWLEDGE_INVALID_RESOURCE,
                safe_provider=self._name,
                safe_resource=request.resource_id,
            ) from None
        concepts = self._load_concepts()
        concept = self._find_concept(concepts, local)
        if concept is None:
            raise KnowledgeError(
                CODE_KNOWLEDGE_INVALID_RESOURCE,
                safe_provider=self._name,
                safe_resource=request.resource_id,
            ) from None
        text = self._render_concept_text(concept)
        encoded = text.encode("utf-8")
        if len(encoded) > request.max_bytes:
            raise KnowledgeError(
                CODE_KNOWLEDGE_TOO_LARGE,
                safe_provider=self._name,
                safe_resource=request.resource_id,
            ) from None
        digest = hashlib.sha256(encoded).hexdigest()
        return KnowledgeDocument(
            provider_name=self._name,
            resource_id=request.resource_id,
            revision=digest,
            title=_concept_title(concept),
            media_type=_MEDIA_MARKDOWN,
            text=text,
            size=len(encoded),
            content_hash=digest,
            source_attribution=_concept_source_attribution(concept),
        )

    # -- bundle loading and rendering --------------------------------------- #

    def _load_concepts(self) -> Mapping[str, object]:
        """Load the OKF bundle in strict mode and return its concept mapping."""

        from selayer.okf import OkfBundle  # local import keeps the module light

        try:
            bundle = OkfBundle.load(self._root, strict=True)
        except Exception:  # noqa: BLE001 - any bundle-load failure is sanitized
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_FAILED, safe_provider=self._name
            ) from None
        return _okf_concepts(bundle)

    @staticmethod
    def _find_concept(
        concepts: Mapping[str, object], semantic_id: str
    ) -> object | None:
        """Return the concept bound to ``semantic_id`` (by selayer_id)."""

        for concept in concepts.values():
            if _concept_semantic_id(concept) == semantic_id:
                return concept
        return None

    @staticmethod
    def _concept_revision(concept: object) -> str:
        """Return the immutable SHA-256 revision of the rendered concept text."""

        text = FilesystemOkfProvider._render_concept_text(concept)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _concept_size(concept: object) -> int:
        """Return the rendered concept text size in UTF-8 bytes."""

        return len(FilesystemOkfProvider._render_concept_text(concept).encode("utf-8"))

    @staticmethod
    def _render_concept_text(concept: object) -> str:
        """Render a concept to deterministic markdown via the OKF renderer."""

        from selayer.okf.document import render_concept

        return render_concept(concept)  # type: ignore[arg-type]
