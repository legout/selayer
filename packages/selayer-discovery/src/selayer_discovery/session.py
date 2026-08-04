"""Append-only, hash-chained discovery session store and state machine.

This module owns the deterministic session foundation for the discovery
companion package:

* :class:`SessionCharter` — the immutable, versioned session charter whose
  fingerprint and named-approver identity bind downstream artifacts.
* :class:`SessionStore` — an append-only, hash-chained event journal that
  reconstructs session state, serializes writers through ``filelock``, and
  propagates dependency invalidation transitively.

Design rules enforced here (see the approved discovery design and the global
plan constraints):

* The journal (``events.jsonl``) is the **single authority**. Materialized
  state written under ``state.json`` is a non-authority cache: every read is
  rebuilt from the journal, so editing the cache can never revive stale state.
* Every event carries schema version, event id, previous hash, event hash,
  normalized actor, UTC ISO-8601 timestamp, type, and a bounded payload. The
  event hash covers all of those fields except itself, so tampering,
  truncation, reordering, and duplicate ids are detected on reconstruction.
* Mutations append one line, flush, and ``os.fsync`` the journal **before**
  publishing any materialized state, and run under a ``filelock`` that reports
  only safe owner metadata (session id and actor) on timeout.
* Session directories use mode ``0700`` and files use mode ``0600`` where the
  platform supports it (POSIX).
* An explicit directed dependency index records which artifact nodes a record
  depends on. When a node's content hash changes (a charter/approver revision,
  or any recorded artifact revision), every transitive dependent is emitted as
  a sorted stale target.

No CLI, evidence intake, profiling, knowledge-provider, proposal, approval, or
apply logic lives here — those belong to later tasks. This module exposes the
primitives those tasks build on.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Self, cast

from filelock import FileLock, Timeout

from selayer_discovery import canonical
from selayer_discovery.diagnostics import DiscoveryError, UnsupportedArtifactError
from selayer_discovery.model import (
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    bounded_mapping,
    normalize_actor_identity,
)

if TYPE_CHECKING:
    from types import TracebackType

__all__ = [
    "GENESIS_HASH",
    "MAX_EVENT_PAYLOAD_BYTES",
    "ArtifactRecord",
    "EventType",
    "SessionCharter",
    "SessionError",
    "SessionEvent",
    "SessionLockTimeoutError",
    "SessionSnapshot",
    "SessionState",
    "SessionStore",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Previous-hash sentinel for the genesis event (64-zero hex, hash-shaped).
GENESIS_HASH: str = "0" * 64

#: Default ``filelock`` acquisition timeout in seconds.
_DEFAULT_LOCK_TIMEOUT: float = 30.0

_JOURNAL_NAME = "events.jsonl"
_LOCK_NAME = "session.lock"
_OWNER_NAME = "session.lock.owner"
_CACHE_NAME = "state.json"
_COMMITTED_HEAD_NAME = "committed_head"

#: Maximum size (in UTF-8 bytes) of a serialized event payload. Every payload
#: is recursively text-bounded and size-bounded before append and on replay.
MAX_EVENT_PAYLOAD_BYTES: int = 256 * 1024

# Stable node identifier shape: lowercase letter first, then lowercase letters,
# digits, underscores, dots, or hyphens. Admits ``charter``, ``approver``,
# ``claim-c1``, ``proposal-g1``, ``verification-g1``, catalog-shaped ids, and
# lowercase uuids, while rejecting free text, paths, and secrets.
_NODE_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_OWNER_TEXT_MAX = 128


class _EventType:
    """Stable event type labels recorded in the journal (constants, not enum)."""

    CHARTER_RECORDED = "charter_recorded"
    CHARTER_REVISED = "charter_revised"
    TRANSITION = "state_transition"
    CLOSED = "closed"
    ARTIFACT_RECORDED = "artifact_recorded"


EventType = _EventType

# Stable session diagnostic codes (rendered, never leak raw causes).
CODE_TRANSITION_INVALID = "discovery.session.transition_invalid"
CODE_CLOSED = "discovery.session.closed"
CODE_DUPLICATE_TERMINAL = "discovery.session.duplicate_terminal"
CODE_INTEGRITY = "discovery.session.integrity"
CODE_LOCK = "discovery.session.lock"
CODE_NOT_INITIALIZED = "discovery.session.not_initialized"
CODE_EXISTS = "discovery.session.exists"
CODE_INVALID_NODE = "discovery.session.invalid_node"
CODE_INVALID_HASH = "discovery.session.invalid_hash"
CODE_INVALID_PAYLOAD = "discovery.session.invalid_payload"
CODE_INVALID_CHARTER = "discovery.session.invalid_charter"


# --------------------------------------------------------------------------- #
# Session state machine                                                       #
# --------------------------------------------------------------------------- #


class SessionState(StrEnum):
    """Derived primary lifecycle state of a discovery session."""

    INITIALIZED = "initialized"
    INTAKE = "intake"
    SAMPLE_POLICY_PENDING = "sample_policy_pending"
    INTERVIEWING = "interviewing"
    DRAFTING = "drafting"
    REVIEW_READY = "review_ready"
    APPROVED = "approved"
    APPLIED = "applied"
    CLOSED = "closed"


#: Allowed forward state transitions. ``CLOSED`` is terminal (empty set). A
#: session may return from ``DRAFTING`` or ``REVIEW_READY`` to ``INTERVIEWING``
#: when new questions arise; every non-terminal state may advance to ``CLOSED``.
_ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.INITIALIZED: frozenset({SessionState.INTAKE, SessionState.CLOSED}),
    SessionState.INTAKE: frozenset(
        {SessionState.SAMPLE_POLICY_PENDING, SessionState.CLOSED}
    ),
    SessionState.SAMPLE_POLICY_PENDING: frozenset(
        {SessionState.INTERVIEWING, SessionState.CLOSED}
    ),
    SessionState.INTERVIEWING: frozenset({SessionState.DRAFTING, SessionState.CLOSED}),
    SessionState.DRAFTING: frozenset(
        {SessionState.REVIEW_READY, SessionState.INTERVIEWING, SessionState.CLOSED}
    ),
    SessionState.REVIEW_READY: frozenset(
        {SessionState.APPROVED, SessionState.INTERVIEWING, SessionState.CLOSED}
    ),
    SessionState.APPROVED: frozenset({SessionState.APPLIED, SessionState.CLOSED}),
    SessionState.APPLIED: frozenset({SessionState.CLOSED}),
    SessionState.CLOSED: frozenset(),
}


# --------------------------------------------------------------------------- #
# Sanitized session diagnostics                                               #
# --------------------------------------------------------------------------- #


def _safe_id(value: object) -> str:
    """Return ``value`` only if it is a stable node-shaped id, else ``<id>``."""

    if type(value) is str and _NODE_ID_RE.match(value) is not None:
        return value
    return "<id>"


def _safe_text(value: object) -> str:
    """Return a bounded safe text fragment or ``<unknown>``."""

    if type(value) is str and len(value) <= _SAFE_OWNER_TEXT_MAX:
        return value
    return "<unknown>"


class SessionError(Exception):
    """Sanitized session diagnostic exception.

    Mirrors the secrecy discipline of
    :class:`selayer_discovery.diagnostics.DiscoveryError`: only a stable code,
    an optional constant ``safe_detail``, and validated ``safe_ids`` are ever
    rendered by ``__str__``, ``__repr__``, or :meth:`to_dict`. Raw causes are
    never chained or rendered.
    """

    def __init__(
        self,
        code: str,
        *,
        safe_detail: str | None = None,
        safe_ids: Iterable[str] = (),
        owner: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.safe_detail = safe_detail
        validated: list[str] = []
        for item in safe_ids:
            validated.append(_safe_id(item))
            if len(validated) >= 16:
                break
        self.safe_ids: tuple[str, ...] = tuple(validated)
        # Optional safe owner metadata (session id + actor) for lock timeouts.
        self.owner: Mapping[str, str] | None = owner
        super().__init__(self._render())

    def _render(self) -> str:
        parts: list[str] = [self.code]
        if self.safe_detail is not None:
            parts.append(self.safe_detail)
        if self.safe_ids:
            parts.append("ids=" + ",".join(self.safe_ids))
        return " ".join(parts)

    def __str__(self) -> str:
        return self._render()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"safe_detail={self.safe_detail!r}, safe_ids={self.safe_ids!r})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping containing only safe fields."""

        result: dict[str, object] = {"code": self.code}
        if self.safe_detail is not None:
            result["safe_detail"] = self.safe_detail
        if self.safe_ids:
            result["safe_ids"] = list(self.safe_ids)
        if self.owner is not None:
            result["owner"] = dict(self.owner)
        return result


class SessionLockTimeoutError(SessionError):
    """Raised when session lock acquisition times out.

    Carries safe owner metadata (owning session id, actor, acquired-at) read
    from the lock sidecar file so the operator knows who holds the lock without
    leaking process arguments or secrets.
    """

    def __init__(self, *, owner: Mapping[str, str]) -> None:
        safe_owner: dict[str, str] = {
            "session_id": _safe_id(owner.get("session_id")),
            "actor": _safe_text(owner.get("actor")),
            "acquired_at": _safe_text(owner.get("acquired_at")),
        }
        super().__init__(
            CODE_LOCK,
            safe_ids=(safe_owner["session_id"],),
            owner=safe_owner,
        )


# --------------------------------------------------------------------------- #
# Charter                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class SessionCharter:
    """Immutable, versioned discovery session charter.

    The charter binds the stable session id, the target catalog fingerprint,
    one business question, the named approver identity, and the in-scope
    inclusions, exclusions, and acceptance questions. Its canonical
    :meth:`fingerprint` (and the separate approver hash) are dependency nodes:
    changing the charter or the approver invalidates every transitive
    dependent.

    The approver is normalized at construction. Identity matching is workflow
    enforcement, not authentication.
    """

    # Re-declared (not subclassing ``Artifact``) so the charter stays a pure,
    # self-describing value type without the base-class artifact-id validator;
    # the schema version is carried explicitly for fingerprint stability.
    schema_version: int = SCHEMA_VERSION
    session_id: str = ""
    business_question: str = ""
    catalog_fingerprint: str = ""
    approver: str = ""
    inclusions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    acceptance_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SCHEMA_VERSION
        ):
            raise SessionError(CODE_INVALID_CHARTER)
        if (
            type(self.session_id) is not str
            or _NODE_ID_RE.match(self.session_id) is None
        ):
            raise SessionError(CODE_INVALID_CHARTER)
        if (
            type(self.business_question) is not str
            or not self.business_question.strip()
        ):
            raise SessionError(CODE_INVALID_CHARTER)
        _validate_hash(self.catalog_fingerprint)
        # Frozen dataclass: normalize the approver in place.
        object.__setattr__(self, "approver", normalize_actor_identity(self.approver))
        # Bound every free-text charter field (including the named approver
        # and the scope/acceptance collections) so a charter payload can never
        # carry unbounded text into the journal or through reconstruction.
        if len(self.business_question) > MAX_TEXT_LENGTH:
            raise SessionError(CODE_INVALID_CHARTER)
        if len(self.approver) > MAX_TEXT_LENGTH:
            raise SessionError(CODE_INVALID_CHARTER)
        for collection in (
            self.inclusions,
            self.exclusions,
            self.acceptance_questions,
        ):
            if not isinstance(collection, tuple) or not all(
                type(item) is str for item in collection
            ):
                raise SessionError(CODE_INVALID_CHARTER)
            for item in collection:
                if len(item) > MAX_TEXT_LENGTH:
                    raise SessionError(CODE_INVALID_CHARTER)

    @property
    def fingerprint(self) -> str:
        """Canonical SHA-256 fingerprint of the whole charter."""

        return canonical.fingerprint(self)

    @property
    def approver_hash(self) -> str:
        """Canonical SHA-256 fingerprint of the normalized approver identity."""

        return canonical.fingerprint(self.approver)


# --------------------------------------------------------------------------- #
# Events                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """A single immutable, hash-chained journal event."""

    schema_version: int
    event_id: str
    previous_hash: str
    actor: str
    timestamp: str
    type: str
    payload: Mapping[str, object]
    event_hash: str


class ArtifactRecord(NamedTuple):
    """Result of recording or revising an artifact or charter.

    ``stale_targets`` is the sorted tuple of artifact node ids that became stale
    (transitive dependents) as a direct result of this revision.
    """

    event: SessionEvent
    stale_targets: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Reconstructed state                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class _Reconstructed:
    """In-memory projection rebuilt from the journal on every load/mutation."""

    charter: SessionCharter | None
    state: SessionState
    head_hash: str
    events: list[SessionEvent] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    dependencies: dict[str, frozenset[str]] = field(default_factory=dict)
    reverse: dict[str, set[str]] = field(default_factory=dict)
    stale: set[str] = field(default_factory=set)

    @classmethod
    def empty(cls) -> _Reconstructed:
        return cls(
            charter=None,
            state=SessionState.INITIALIZED,
            head_hash=GENESIS_HASH,
        )


@dataclass(frozen=True)
class SessionSnapshot:
    """Read-only view of reconstructed session state for callers/tests."""

    state: SessionState
    charter: SessionCharter | None
    head_hash: str
    events: tuple[SessionEvent, ...]
    stale_nodes: tuple[str, ...]
    artifact_hashes: Mapping[str, str]
    dependencies: Mapping[str, frozenset[str]]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    """Return the current UTC time as a microsecond ISO-8601 string."""

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _validate_timestamp(value: object) -> None:
    """Validate that ``value`` is an ISO-8601 string in UTC."""

    if type(value) is not str:
        raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SessionError(CODE_INTEGRITY, safe_detail="malformed event") from None
    offset = parsed.tzinfo.utcoffset(parsed) if parsed.tzinfo is not None else None
    if offset != timedelta(0):
        raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")


def _validate_node_id(node_id: object) -> str:
    """Validate and return a stable node identifier."""

    if type(node_id) is not str or _NODE_ID_RE.match(node_id) is None:
        raise SessionError(CODE_INVALID_NODE)
    return node_id


def _validate_hash(value: object) -> str:
    """Validate and return a 64-character lowercase hex content hash."""

    if type(value) is not str or _HEX64_RE.match(value) is None:
        raise SessionError(CODE_INVALID_HASH)
    return value


def _normalize_payload(payload: object) -> dict[str, object]:
    """Return a JSON-native bounded mapping for an event payload."""

    try:
        mapping = bounded_mapping(payload)
        normalized = canonical.normalize_artifact(mapping)
    except (DiscoveryError, UnsupportedArtifactError):
        raise SessionError(CODE_INVALID_PAYLOAD) from None
    if not isinstance(normalized, dict):
        raise SessionError(CODE_INVALID_PAYLOAD) from None
    return cast("dict[str, object]", normalized)


def _bound_text_recursive(value: object, *, code: str) -> None:
    """Recursively enforce the bounded-text limit on every string in ``value``.

    Walks mappings and (non-string) sequences recursively so a nested payload
    can never smuggle an unbounded text field past the append or replay guards.
    Both mapping keys and values are bounded: JSON object keys are strings, so a
    deeply nested mapping key is just as capable of carrying an unbounded text
    field as a value and must not be exempted.
    """

    if type(value) is str:
        if len(value) > MAX_TEXT_LENGTH:
            raise SessionError(code)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _bound_text_recursive(key, code=code)
            _bound_text_recursive(item, code=code)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _bound_text_recursive(item, code=code)


def _assert_payload_bounded(
    payload: Mapping[str, object],
    *,
    code: str = CODE_INVALID_PAYLOAD,
) -> None:
    """Enforce recursive text bounds and the serialized payload size bound.

    Every event payload is bounded before it is appended to the journal and
    again when it is replayed from the journal, so neither an oversized single
    text field nor an oversized aggregate payload can enter or persist.
    """

    _bound_text_recursive(payload, code=code)
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    if len(serialized) > MAX_EVENT_PAYLOAD_BYTES:
        raise SessionError(code)


def _charter_from_payload(payload: Mapping[str, object]) -> SessionCharter:
    """Reconstruct a :class:`SessionCharter` from a recorded event payload."""

    record = payload.get("charter")
    if not isinstance(record, Mapping):
        raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
    schema_version = record.get("schema_version")
    if type(schema_version) is not int:
        raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")

    def _text(name: str) -> str:
        value = record.get(name)
        if not isinstance(value, str):
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
        return value

    def _text_tuple(name: str) -> tuple[str, ...]:
        value = record.get(name, ())
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
        return tuple(str(item) for item in value)

    return SessionCharter(
        schema_version=schema_version,
        session_id=_text("session_id"),
        business_question=_text("business_question"),
        catalog_fingerprint=_text("catalog_fingerprint"),
        approver=_text("approver"),
        inclusions=_text_tuple("inclusions"),
        exclusions=_text_tuple("exclusions"),
        acceptance_questions=_text_tuple("acceptance_questions"),
    )


def _register_artifact(
    r: _Reconstructed,
    artifact_id: str,
    content_hash: str,
    depends_on: Iterable[str],
) -> bool:
    """Register/update an artifact node and its dependency edges.

    Returns ``True`` when this is a *revision* (the node already had a different
    content hash recorded), which the caller uses to propagate staleness. The
    reverse-edge index is rebuilt from the full dependency map so deletions and
    re-parenting are reflected deterministically.
    """

    previous = r.artifact_hashes.get(artifact_id)
    changed = previous is not None and previous != content_hash
    r.artifact_hashes[artifact_id] = content_hash
    r.dependencies[artifact_id] = frozenset(_validate_node_id(d) for d in depends_on)
    reverse: dict[str, set[str]] = {}
    for node, deps in r.dependencies.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(node)
    r.reverse = reverse
    return changed


def _transitive_dependents(r: _Reconstructed, changed: set[str]) -> set[str]:
    """Return the set of nodes that transitively depend on ``changed``.

    The changed nodes themselves are excluded unless they are reachable as
    dependents through a cycle (the index is expected to be acyclic; cycle
    detection belongs to the proposal task).
    """

    result: set[str] = set()
    stack: list[str] = list(changed)
    while stack:
        node = stack.pop()
        for dependent in r.reverse.get(node, ()):
            if dependent not in result:
                result.add(dependent)
                stack.append(dependent)
    return result


# --------------------------------------------------------------------------- #
# SessionStore                                                                #
# --------------------------------------------------------------------------- #


class SessionStore:
    """Append-only, hash-chained discovery session store.

    The store owns one session directory. The journal (``events.jsonl``) is the
    single source of truth; ``state.json`` is a non-authority cache. Mutations
    append one JSON line, flush, and ``os.fsync`` the journal before publishing
    materialized state, and they serialize through a ``filelock`` whose timeout
    reports only safe owner metadata.

    Construct with :meth:`create` (new session) or :meth:`open` (existing).
    """

    def __init__(self, root: Path, *, lock_timeout: float = _DEFAULT_LOCK_TIMEOUT) -> None:
        self._root = root
        self._lock_timeout = lock_timeout
        self._journal = root / _JOURNAL_NAME
        self._lock_path = root / _LOCK_NAME
        self._owner_path = root / _OWNER_NAME
        self._cache_path = root / _CACHE_NAME
        self._committed_head_path = root / _COMMITTED_HEAD_NAME
        self._lock = FileLock(str(self._lock_path))
        self._session_id: str | None = None
        self._last_actor: str | None = None
        self._reconstructed: _Reconstructed = _Reconstructed.empty()

    # -- construction ------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        charter: SessionCharter,
        actor: str,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> SessionStore:
        """Create a new session at ``root`` and record its genesis charter."""

        store = cls(root, lock_timeout=lock_timeout)
        store._create(charter, actor)
        return store

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> SessionStore:
        """Open an existing session and rebuild state from its journal."""

        store = cls(root, lock_timeout=lock_timeout)
        store._load()
        return store

    def _create(self, charter: SessionCharter, actor: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._restrict_dir(self._root)
        self._session_id = charter.session_id
        self._last_actor = normalize_actor_identity(actor)
        with self._mutation():
            if self._journal.exists():
                raise SessionError(
                    CODE_EXISTS,
                    safe_ids=(self._safe_session_id(),),
                )
            # A fresh session discards any orphaned committed-head sidecar
            # left by a prior, removed journal so it cannot block genesis.
            self._committed_head_path.unlink(missing_ok=True)
            payload = self._charter_payload(charter)
            self._append(EventType.CHARTER_RECORDED, payload=payload, actor=actor)

    def _load(self) -> None:
        with self._mutation():
            if self._reconstructed.charter is None:
                raise SessionError(CODE_NOT_INITIALIZED)
            self._session_id = self._reconstructed.charter.session_id
            # Refresh the committed-head sidecar so any crash-induced lag is
            # caught up before the session is handed back to the caller.
            self._write_committed_head()
        self._write_cache()

    # -- context managers --------------------------------------------------- #

    def __enter__(self) -> Self:
        self._acquire()
        self._reconstructed = self._reconstruct()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._release()

    @contextmanager
    def lock(self) -> Iterator[SessionStore]:
        """Hold the session lock for a batch of mutations."""

        self._acquire()
        try:
            yield self
        finally:
            self._release()

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """Acquire the lock, refresh from the journal, then release."""

        self._acquire()
        try:
            self._reconstructed = self._reconstruct()
            yield
        finally:
            self._release()

    # -- properties --------------------------------------------------------- #

    @property
    def root(self) -> Path:
        return self._root

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise SessionError(CODE_NOT_INITIALIZED)
        return self._session_id

    @property
    def state(self) -> SessionState:
        return self._reconstructed.state

    @property
    def charter(self) -> SessionCharter:
        if self._reconstructed.charter is None:
            raise SessionError(CODE_NOT_INITIALIZED)
        return self._reconstructed.charter

    @property
    def head_hash(self) -> str:
        return self._reconstructed.head_hash

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._reconstructed.events)

    @property
    def stale_nodes(self) -> tuple[str, ...]:
        """Sorted tuple of artifact node ids currently marked stale."""

        return tuple(sorted(self._reconstructed.state and self._reconstructed.stale))

    def _safe_session_id(self) -> str:
        return _safe_id(self._session_id)

    # -- state transitions -------------------------------------------------- #

    def transition(
        self,
        target: SessionState | str,
        *,
        actor: str,
        payload: Mapping[str, object] | None = None,
    ) -> SessionEvent:
        """Validate and append a state transition (or close) event."""

        target_state = SessionState(target)
        self._last_actor = normalize_actor_identity(actor)
        with self._mutation():
            self._assert_open()
            current = self._reconstructed.state
            if target_state not in _ALLOWED_TRANSITIONS[current]:
                if target_state is SessionState.CLOSED and current is SessionState.CLOSED:
                    raise SessionError(
                        CODE_DUPLICATE_TERMINAL,
                        safe_ids=(self._safe_session_id(),),
                    )
                raise SessionError(
                    CODE_TRANSITION_INVALID,
                    safe_ids=(self._safe_session_id(),),
                )
            event_type = (
                EventType.CLOSED
                if target_state is SessionState.CLOSED
                else EventType.TRANSITION
            )
            normalized = _normalize_payload(payload or {})
            event_payload: dict[str, object] = {"target": target_state.value}
            event_payload.update(normalized)
            return self._append(event_type, payload=event_payload, actor=actor)

    def close(
        self,
        *,
        actor: str,
        payload: Mapping[str, object] | None = None,
    ) -> SessionEvent:
        """Convenience for :meth:`transition` to the terminal ``CLOSED`` state."""

        return self.transition(SessionState.CLOSED, actor=actor, payload=payload)

    def _assert_open(self) -> None:
        if self._reconstructed.state is SessionState.CLOSED:
            raise SessionError(CODE_CLOSED, safe_ids=(self._safe_session_id(),))

    # -- charter revision --------------------------------------------------- #

    def revise_charter(
        self,
        charter: SessionCharter,
        *,
        actor: str,
    ) -> ArtifactRecord:
        """Record a charter revision, emitting stale dependents if changed.

        The session id is immutable; only the other charter fields (including
        the named approver) may change.
        """

        self._last_actor = normalize_actor_identity(actor)
        with self._mutation():
            self._assert_open()
            current = self._reconstructed.charter
            if current is None:
                raise SessionError(CODE_NOT_INITIALIZED)
            if charter.session_id != current.session_id:
                raise SessionError(
                    CODE_INVALID_CHARTER,
                    safe_detail="session id is immutable",
                    safe_ids=(_safe_id(current.session_id),),
                )
            before = set(self._reconstructed.stale)
            payload = self._charter_payload(charter)
            payload["previous_charter_fingerprint"] = current.fingerprint
            event = self._append(
                EventType.CHARTER_REVISED, payload=payload, actor=actor
            )
            emitted = tuple(sorted(self._reconstructed.stale - before))
            return ArtifactRecord(event, emitted)

    # -- artifact / dependency index ---------------------------------------- #

    def record_artifact(
        self,
        artifact_id: str,
        *,
        content_hash: str,
        depends_on: Iterable[str] = (),
        actor: str,
    ) -> ArtifactRecord:
        """Record (or revise) an artifact node and its dependency edges.

        On a revision (the node already had a different ``content_hash``), the
        sorted tuple of transitive dependents is emitted as stale targets.
        """

        node_id = _validate_node_id(artifact_id)
        _validate_hash(content_hash)
        deps = tuple(_validate_node_id(dep) for dep in depends_on)
        self._last_actor = normalize_actor_identity(actor)
        with self._mutation():
            self._assert_open()
            before = set(self._reconstructed.stale)
            payload: dict[str, object] = {
                "artifact_id": node_id,
                "content_hash": content_hash,
                "depends_on": list(deps),
            }
            event = self._append(
                EventType.ARTIFACT_RECORDED, payload=payload, actor=actor
            )
            emitted = tuple(sorted(self._reconstructed.stale - before))
            return ArtifactRecord(event, emitted)

    def compute_stale(self, *changed: str) -> tuple[str, ...]:
        """Return the sorted transitive dependents of ``changed`` (pure query).

        This does not mutate the session; it reports what *would* become stale.
        """

        nodes = {_validate_node_id(node) for node in changed}
        return tuple(sorted(_transitive_dependents(self._reconstructed, nodes)))

    # -- reconstruction ----------------------------------------------------- #

    def reconstruct(self) -> SessionSnapshot:
        """Force a full rebuild from the journal under the lock and return it."""

        with self._mutation():
            r = self._reconstructed
            return SessionSnapshot(
                state=r.state,
                charter=r.charter,
                head_hash=r.head_hash,
                events=tuple(r.events),
                stale_nodes=tuple(sorted(r.stale)),
                artifact_hashes=dict(r.artifact_hashes),
                dependencies=dict(r.dependencies),
            )

    def _reconstruct(self, *, validate_head: bool = True) -> _Reconstructed:
        if not self._journal.exists():
            return _Reconstructed.empty()
        reconstructed = _Reconstructed.empty()
        seen_ids: set[str] = set()
        expected_prev = GENESIS_HASH
        with self._journal.open("r", encoding="utf-8") as handle:
            for lineno, raw in enumerate(handle, start=1):
                line = raw.rstrip("\n")
                if line == "":
                    continue
                event = self._parse_event(line, expected_prev, lineno)
                if event.event_id in seen_ids:
                    raise SessionError(
                        CODE_INTEGRITY,
                        safe_detail="duplicate event id",
                    )
                seen_ids.add(event.event_id)
                self._apply_event(reconstructed, event)
                expected_prev = event.event_hash
                reconstructed.events.append(event)
                reconstructed.head_hash = event.event_hash
        # The journal is the base authority, but an independent committed-head
        # sidecar records the last durably-appended head so silent truncation
        # of a complete trailing event is detected (the cache cannot be this
        # authority because it is explicitly non-authority and rebuildable).
        # The in-append reconstruct skips this check: the append itself is the
        # authority for the freshly-fsynced trailing event, and the sidecar
        # legitimately lags (or is still absent during genesis).
        if validate_head:
            self._validate_committed_head(reconstructed)
        return reconstructed

    def _parse_event(
        self,
        line: str,
        expected_prev: str,
        lineno: int,
    ) -> SessionEvent:
        try:
            obj = json.loads(line)
        # pi-lens-ignore: bare-except (false positive: this catches the
        # specific json.JSONDecodeError, not a bare except)
        except json.JSONDecodeError:
            raise SessionError(
                CODE_INTEGRITY,
                safe_detail="malformed event",
            ) from None
        if not isinstance(obj, dict):
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
        required = {
            "schema_version",
            "event_id",
            "previous_hash",
            "actor",
            "timestamp",
            "type",
            "payload",
            "event_hash",
        }
        if not required.issubset(obj):
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
        schema_version = obj["schema_version"]
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise SessionError(CODE_INTEGRITY, safe_detail="schema version mismatch")
        if obj["previous_hash"] != expected_prev:
            raise SessionError(CODE_INTEGRITY, safe_detail="broken hash chain")
        payload = obj["payload"]
        if not isinstance(payload, dict):
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
        record = {
            "schema_version": schema_version,
            "event_id": obj["event_id"],
            "previous_hash": obj["previous_hash"],
            "actor": obj["actor"],
            "timestamp": obj["timestamp"],
            "type": obj["type"],
            "payload": payload,
        }
        recomputed = canonical.fingerprint(record)
        if recomputed != obj["event_hash"]:
            raise SessionError(CODE_INTEGRITY, safe_detail="event hash mismatch")
        _validate_timestamp(obj["timestamp"])
        # Re-enforce the recursive text/size bounds on replay so an unbounded
        # payload recorded by a non-conforming writer is rejected on load.
        _assert_payload_bounded(payload, code=CODE_INTEGRITY)
        return SessionEvent(
            schema_version=schema_version,
            event_id=str(obj["event_id"]),
            previous_hash=str(obj["previous_hash"]),
            actor=str(obj["actor"]),
            timestamp=str(obj["timestamp"]),
            type=str(obj["type"]),
            payload=payload,
            event_hash=str(obj["event_hash"]),
        )

    def _apply_event(self, r: _Reconstructed, event: SessionEvent) -> None:
        event_type = event.type
        payload = event.payload
        if event_type == EventType.CHARTER_RECORDED:
            # The charter-recorded event is the genesis: it must be the first
            # event (no charter recorded yet, no events applied yet).
            if r.charter is not None or r.events:
                raise SessionError(
                    CODE_INTEGRITY,
                    safe_detail="duplicate genesis event",
                )
            r.charter = _charter_from_payload(payload)
            r.state = SessionState.INITIALIZED
            _register_artifact(
                r,
                "charter",
                _validate_hash(payload.get("charter_fingerprint")),
                (),
            )
            _register_artifact(
                r,
                "approver",
                _validate_hash(payload.get("approver_hash")),
                (),
            )
            return
        # Every non-genesis event requires an established charter and an open
        # (non-terminal) session; reconstruction enforces the same transition
        # and terminal rules as live mutations, so a hash-valid journal that
        # skips a state, re-opens a closed session, or appends after close is
        # rejected exactly as a live mutation would be.
        if r.charter is None:
            raise SessionError(CODE_INTEGRITY, safe_detail="event before genesis")
        if r.state is SessionState.CLOSED:
            raise SessionError(CODE_INTEGRITY, safe_detail="event after close")
        if event_type == EventType.TRANSITION:
            target = self._state_from_payload(payload)
            if target not in _ALLOWED_TRANSITIONS[r.state]:
                raise SessionError(CODE_INTEGRITY, safe_detail="invalid transition")
            r.state = target
        elif event_type == EventType.CLOSED:
            if SessionState.CLOSED not in _ALLOWED_TRANSITIONS[r.state]:
                raise SessionError(CODE_INTEGRITY, safe_detail="invalid transition")
            r.state = SessionState.CLOSED
        elif event_type == EventType.CHARTER_REVISED:
            new_charter = _charter_from_payload(payload)
            new_cf = _validate_hash(payload.get("charter_fingerprint"))
            new_ah = _validate_hash(payload.get("approver_hash"))
            if _register_artifact(r, "charter", new_cf, ()):
                r.stale |= _transitive_dependents(r, {"charter"})
            if _register_artifact(r, "approver", new_ah, ()):
                r.stale |= _transitive_dependents(r, {"approver"})
            r.charter = new_charter
        elif event_type == EventType.ARTIFACT_RECORDED:
            artifact_id = _validate_node_id(payload.get("artifact_id"))
            content_hash = _validate_hash(payload.get("content_hash"))
            raw_deps = payload.get("depends_on", ())
            if isinstance(raw_deps, str) or not isinstance(raw_deps, Sequence):
                raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
            deps = tuple(_validate_node_id(dep) for dep in raw_deps)
            if _register_artifact(r, artifact_id, content_hash, deps):
                r.stale |= _transitive_dependents(r, {artifact_id})
        else:
            raise SessionError(CODE_INTEGRITY, safe_detail="unknown event type")

    @staticmethod
    def _state_from_payload(payload: Mapping[str, object]) -> SessionState:
        target = payload.get("target")
        if not isinstance(target, str):
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event")
        try:
            return SessionState(target)
        except ValueError:
            raise SessionError(CODE_INTEGRITY, safe_detail="malformed event") from None

    # -- append + durability ------------------------------------------------ #

    def _append(
        self,
        event_type: str,
        *,
        payload: Mapping[str, object],
        actor: str,
    ) -> SessionEvent:
        actor_n = normalize_actor_identity(actor)
        self._last_actor = actor_n
        previous_hash = self._reconstructed.head_hash
        event_id = uuid.uuid4().hex
        timestamp = _utc_now_iso()
        payload_obj = dict(payload)
        # Bound every text field and the serialized payload size before the
        # event is hashed or written, so an unbounded payload can never enter
        # the journal.
        _assert_payload_bounded(payload_obj)
        record: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "previous_hash": previous_hash,
            "actor": actor_n,
            "timestamp": timestamp,
            "type": event_type,
            "payload": payload_obj,
        }
        event_hash = canonical.fingerprint(record)
        line_obj = {**record, "event_hash": event_hash}
        encoded = json.dumps(line_obj, ensure_ascii=False, sort_keys=True) + "\n"
        # Append, flush, and fsync the journal before publishing any state.
        with self._journal.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._restrict_file(self._journal)
        # The journal is authority: rebuild from it after every append. The
        # in-append reconstruct bypasses the committed-head check because the
        # append is the authority for the freshly-fsynced trailing event and
        # the sidecar legitimately lags it (genesis has no sidecar yet).
        self._reconstructed = self._reconstruct(validate_head=False)
        # Record the durably-committed head independent of the non-authority
        # cache so a later deletion of a complete trailing event is detected.
        self._write_committed_head()
        self._write_cache()
        return SessionEvent(
            schema_version=SCHEMA_VERSION,
            event_id=event_id,
            previous_hash=previous_hash,
            actor=actor_n,
            timestamp=timestamp,
            type=event_type,
            payload=payload_obj,
            event_hash=event_hash,
        )

    def _charter_payload(self, charter: SessionCharter) -> dict[str, object]:
        record = canonical.normalize_artifact(charter)
        if not isinstance(record, dict):
            raise SessionError(CODE_INVALID_CHARTER)
        return {
            "charter": record,
            "charter_fingerprint": charter.fingerprint,
            "approver_hash": charter.approver_hash,
        }

    def _write_cache(self) -> None:
        """Write the non-authority materialized state cache."""

        if self._reconstructed.charter is None:
            return
        cache: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "state": self._reconstructed.state.value,
            "head_hash": self._reconstructed.head_hash,
            "charter_fingerprint": self._reconstructed.charter.fingerprint,
            "stale_nodes": sorted(self._reconstructed.stale),
        }
        tmp = self._cache_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._cache_path)
        self._restrict_file(self._cache_path)

    def _write_committed_head(self) -> None:
        """Durably record the last committed head hash and event count.

        This sidecar is independent of the non-authority ``state.json`` cache:
        it is written after the journal is fsynced (never before) so it can
        never claim an event that is not durably in the journal, and it lets
        reconstruction detect when a complete trailing event has been removed
        even though the remaining hash chain stays intact.
        """

        if self._reconstructed.charter is None:
            return
        data = {
            "schema_version": SCHEMA_VERSION,
            "head_hash": self._reconstructed.head_hash,
            "event_count": len(self._reconstructed.events),
        }
        tmp = self._committed_head_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._committed_head_path)
        self._restrict_file(self._committed_head_path)

    def _read_committed_head(self) -> tuple[str, int] | None:
        """Return the recorded ``(head_hash, event_count)`` or ``None``.

        A missing or unparseable sidecar yields ``None`` (lenient: the journal
        remains the base authority and reconstruction falls back to it).
        """

        try:
            data = json.loads(self._committed_head_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        head = data.get("head_hash")
        count = data.get("event_count")
        if type(head) is not str or _HEX64_RE.match(head) is None:
            return None
        if type(count) is not int or count < 0:
            return None
        return head, count

    def _validate_committed_head(self, r: _Reconstructed) -> None:
        """Detect silent truncation or tampering of a complete trailing event.

        Because the committed head is written only after the journal is
        fsynced, a valid journal never holds fewer events than the committed
        count. A smaller rebuilt count therefore proves a trailing event was
        removed; an equal count with a different head proves tampering.

        This check fails **closed**: every durable append atomically writes
        the sidecar, so a non-empty journal with a missing, unreadable, or
        malformed sidecar proves the sidecar was removed or corrupted
        (typically alongside the trailing event it would have flagged). An
        empty journal legitimately has no sidecar, and the in-append
        reconstruct bypasses this check entirely (the append is the authority
        for the trailing event, and the sidecar lags it).
        """

        rebuilt_count = len(r.events)
        if rebuilt_count == 0:
            # An empty journal has never committed an event, so no committed
            # head is expected (fresh directory or all-blank journal).
            return
        committed = self._read_committed_head()
        if committed is None:
            # Non-empty journal but no readable sidecar: the journal is the
            # base authority and every append writes the sidecar, so a
            # missing/corrupt sidecar proves tampering. Fail closed rather
            # than silently accepting a journal whose trailing event and
            # sidecar were both removed.
            raise SessionError(
                CODE_INTEGRITY, safe_detail="committed head missing"
            )
        committed_head, committed_count = committed
        if rebuilt_count < committed_count:
            raise SessionError(CODE_INTEGRITY, safe_detail="journal truncated")
        if rebuilt_count == committed_count and r.head_hash != committed_head:
            raise SessionError(CODE_INTEGRITY, safe_detail="head hash mismatch")
        # A rebuilt count greater than the committed count means the journal
        # holds a freshly fsynced event whose committed head has not yet caught
        # up (crash recovery after a journal fsync that preceded the sidecar
        # write); the journal is authority, so this is accepted and the next
        # mutation/open refreshes the sidecar.

    # -- locking + permissions --------------------------------------------- #

    def _acquire(self) -> None:
        try:
            self._lock.acquire(timeout=self._lock_timeout)
        except Timeout:
            raise SessionLockTimeoutError(owner=self._read_owner()) from None
        if self._lock.lock_counter == 1:
            # Restrict the lock file created by filelock to owner-only access.
            self._restrict_file(self._lock_path)
            self._write_owner()

    def _release(self) -> None:
        if self._lock.lock_counter <= 0:
            return
        last = self._lock.lock_counter == 1
        if last:
            self._clear_owner()
        self._lock.release()

    def _write_owner(self) -> None:
        data = {
            "session_id": _safe_id(self._session_id),
            "actor": _safe_text(self._last_actor),
            "acquired_at": _utc_now_iso(),
        }
        tmp = self._owner_path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._owner_path)
            self._restrict_file(self._owner_path)
        except OSError:
            # Owner metadata is best-effort reporting; never fail a mutation
            # because the sidecar could not be written.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _clear_owner(self) -> None:
        try:
            self._owner_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _read_owner(self) -> dict[str, str]:
        try:
            data = json.loads(self._owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "session_id": "<unknown>",
                "actor": "<unknown>",
                "acquired_at": "<unknown>",
            }
        if not isinstance(data, dict):
            return {
                "session_id": "<unknown>",
                "actor": "<unknown>",
                "acquired_at": "<unknown>",
            }
        return {
            "session_id": _safe_id(data.get("session_id")),
            "actor": _safe_text(data.get("actor")),
            "acquired_at": _safe_text(data.get("acquired_at")),
        }

    @staticmethod
    def _restrict_file(path: Path) -> None:
        if os.name == "posix":
            os.chmod(path, 0o600)

    @staticmethod
    def _restrict_dir(path: Path) -> None:
        if os.name == "posix":
            os.chmod(path, 0o700)
