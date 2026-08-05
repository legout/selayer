"""Workspace policy and session CLI for ``selayer-discovery``.

The CLI is the only mutation surface for discovery sessions: it parses
structured charter inputs, enforces the workspace path policy, and delegates
persistence to the append-only :class:`~selayer_discovery.session.SessionStore`.

Workspace policy
----------------

* Sessions live under ``<project>/.selayer/discovery/sessions/<id>/`` and are
  Git-ignored (see the repository ``.gitignore``).
* Apply transactions live under ``<project>/.selayer/discovery/transactions/``
  and are Git-ignored.
* ``<project>/.selayer/discovery.yaml`` (project configuration) and
  ``<project>/semantic_changes/`` (approved summaries) remain Git-visible.
* The catalog path and summary root must resolve inside the project root.

Diagnostics
-----------

Errors render as stable, sorted JSON on stderr and never print a traceback by
default. Raw charter values, credentials, and paths outside the project are
never echoed; only stable codes, constant generic details, and validated safe
identifiers surface. ``--debug`` re-raises unexpected errors for development.

Structured inputs are parsed and validated before any mutation lock is
acquired, so an invalid charter or an escaping path fails before the session
store touches the filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import duckdb
from ruamel.yaml import YAML, YAMLError

from selayer.catalog import SemanticLayer
from selayer.sources.errors import SourceError
from selayer.sources.profiles import (
    MappingArrowProviderResolver,
    MappingProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer_discovery.diagnostics import DiscoveryError
from selayer_discovery.evidence import (
    MEDIA_TEXT_MARKDOWN,
    MEDIA_TEXT_PLAIN,
    EvidenceError,
    EvidenceStore,
)
from selayer_discovery.model import SCHEMA_VERSION, normalize_actor_identity
from selayer_discovery.profiling import (
    PolicyActivation,
    ProfilePolicyError,
    ProfileRunner,
    SamplePolicy,
    SourceProfile,
    activate_policy,
    build_context_export,
    propose_policy,
    verify_activation,
)
from selayer_discovery.session import (
    CODE_NOT_INITIALIZED,
    SessionCharter,
    SessionError,
    SessionStore,
)

__all__ = ["main", "run"]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Success exit code.
EXIT_OK: int = 0

#: Operational error exit code (charter, path, or session failure).
EXIT_ERROR: int = 1

#: Project-relative session workspace root.
_DISCOVERY_SESSIONS_REL = ".selayer/discovery/sessions"

#: Default approved-summary root (Git-visible), relative to the project.
_DEFAULT_SUMMARY_ROOT = "semantic_changes"

# Stable CLI diagnostic codes (rendered; never leak raw causes).
CODE_CHARTER_INVALID = "discovery.cli.charter_invalid"
CODE_CHARTER_LOAD = "discovery.cli.charter_load_failed"
CODE_PATH_NOT_CONTAINED = "discovery.cli.path_not_contained"
CODE_SESSION_ID_INVALID = "discovery.cli.session_id_invalid"
CODE_PROFILE_LOAD = "discovery.profile.load_failed"
CODE_PROFILE_SALT = "discovery.profile.salt_missing"
CODE_PROFILE_STALE = "discovery.profile.policy_stale"
CODE_INTERNAL = "discovery.cli.internal"

# Stable session-id grammar (mirrors the session node-id shape so an id is safe
# to use as a single filesystem path component — no '/', no leading '..').
_SESSION_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")

#: Charter text fields that must be present and non-blank.
_REQUIRED_TEXT_FIELDS: tuple[str, ...] = (
    "business_question",
    "approver",
    "catalog_fingerprint",
)

#: Charter list fields that must be present and non-empty string lists.
_REQUIRED_LIST_FIELDS: tuple[str, ...] = (
    "inclusions",
    "exclusions",
    "acceptance_questions",
)


# --------------------------------------------------------------------------- #
# Safe CLI diagnostics                                                        #
# --------------------------------------------------------------------------- #


class _CliError(Exception):
    """Sanitized CLI diagnostic carrying only safe, stable fields.

    Only a stable ``code``, an optional *constant* generic ``safe_detail``,
    and validated ``safe_ids`` are ever rendered. Raw causes are never chained
    or surfaced (``from None`` at every raise site).
    """

    def __init__(
        self,
        code: str,
        *,
        safe_detail: str | None = None,
        safe_ids: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.safe_detail = safe_detail
        self.safe_ids = tuple(_safe_id(item) for item in safe_ids)
        super().__init__(code)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code}
        if self.safe_detail is not None:
            payload["safe_detail"] = self.safe_detail
        if self.safe_ids:
            payload["safe_ids"] = list(self.safe_ids)
        return payload


def _safe_id(value: object) -> str:
    """Return ``value`` only if it is a stable session-id-shaped string."""

    if type(value) is str and _SESSION_ID_RE.match(value) is not None:
        return value
    return "<id>"


def _error_payload(exc: BaseException) -> dict[str, object]:
    """Build a JSON-safe payload from a sanitized CLI, session, or evidence error."""

    if isinstance(exc, _CliError):
        return exc.to_dict()
    if isinstance(exc, SessionError):
        return exc.to_dict()
    if isinstance(exc, EvidenceError):
        return exc.to_dict()
    if isinstance(exc, SourceError):
        return {"code": exc.code, "source_id": _safe_id(exc.source_id)}
    if isinstance(exc, (ProfilePolicyError, DiscoveryError)):
        return exc.to_dict()
    return {"code": CODE_INTERNAL}


def _emit_error(exc: BaseException) -> None:
    """Write a stable, sorted JSON diagnostic to stderr (never a traceback)."""

    payload = _error_payload(exc)
    # Sort every list-valued diagnostic field so output is deterministic.
    for key, value in list(payload.items()):
        if isinstance(value, list):
            payload[key] = sorted(value)
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Workspace policy                                                            #
# --------------------------------------------------------------------------- #


def _project_root(value: str | None) -> Path:
    """Resolve the project root (defaults to the current directory)."""

    return Path(value).resolve() if value else Path.cwd().resolve()


def _session_dir(project: Path, session_id: str) -> Path:
    """Return the absolute session directory for ``session_id``."""

    return project / _DISCOVERY_SESSIONS_REL / session_id


def _workspace_rel(session_id: str) -> str:
    """Return the project-relative workspace path (forward slashes)."""

    return f"{_DISCOVERY_SESSIONS_REL}/{session_id}"


def _assert_contained(project: Path, candidate: Path, *, kind: str) -> Path:
    """Resolve ``candidate`` against the project and assert it stays inside.

    Relative candidates are resolved against the project root; absolute
    candidates must already be inside it. Symlink targets are resolved so a
    link pointing outside the project is rejected.
    """

    project_abs = project.resolve()
    target = candidate if candidate.is_absolute() else project_abs / candidate
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(project_abs)
    except ValueError:
        raise _CliError(CODE_PATH_NOT_CONTAINED, safe_detail=kind) from None
    return resolved


def _validate_session_id(value: object) -> str:
    """Validate a CLI-supplied session id before using it as a path component."""

    if type(value) is not str or _SESSION_ID_RE.match(value) is None:
        raise _CliError(CODE_SESSION_ID_INVALID) from None
    return value


# --------------------------------------------------------------------------- #
# Charter loading and validation                                              #
# --------------------------------------------------------------------------- #


def _load_charter_mapping(path: Path) -> dict[str, object]:
    """Load a charter YAML file into a mapping (never leaks parse details)."""

    yaml = YAML(typ="safe")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle)
    except (OSError, YAMLError):
        raise _CliError(CODE_CHARTER_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_CHARTER_INVALID) from None
    return dict(data)


def _require_text(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if type(value) is not str or not value.strip():
        raise _CliError(CODE_CHARTER_INVALID) from None
    return value


def _require_string_list(data: Mapping[str, object], field: str) -> list[str]:
    value = data.get(field)
    if not isinstance(value, list) or not value:
        raise _CliError(CODE_CHARTER_INVALID) from None
    result: list[str] = []
    for item in value:
        if type(item) is not str or not item.strip():
            raise _CliError(CODE_CHARTER_INVALID) from None
        result.append(item)
    return result


def _build_charter(
    data: Mapping[str, object],
    *,
    explicit_session_id: str | None,
    catalog_path: str,
) -> SessionCharter:
    """Validate charter fields and construct a :class:`SessionCharter`.

    All charter validation failures surface as :data:`CODE_CHARTER_INVALID`;
    the :class:`SessionCharter` constructor is defense in depth. The session id
    is the explicit CLI/charter id when provided, else an auto-generated value.
    The ``catalog_path`` is the canonical project-relative POSIX path already
    resolved and validated as project-contained by the caller.
    """

    for field in _REQUIRED_TEXT_FIELDS:
        _require_text(data, field)
    # The catalog fingerprint is a text field but must also be a 64-hex hash.
    if _HEX64_RE.match(data["catalog_fingerprint"]) is None:  # type: ignore[arg-type]
        raise _CliError(CODE_CHARTER_INVALID) from None
    inclusions = _require_string_list(data, "inclusions")
    exclusions = _require_string_list(data, "exclusions")
    acceptance_questions = _require_string_list(data, "acceptance_questions")

    session_id: str
    if explicit_session_id is not None:
        session_id = explicit_session_id
    else:
        raw = data.get("session_id")
        if raw is None:
            # Prefix so the id always satisfies the node-id grammar (first
            # character must be a lowercase letter; a bare uuid hex may start
            # with a digit).
            session_id = f"session-{uuid.uuid4().hex}"
        elif type(raw) is str and raw.strip():
            session_id = raw
        else:
            raise _CliError(CODE_CHARTER_INVALID) from None

    return SessionCharter(
        session_id=session_id,
        catalog_path=catalog_path,
        business_question=data["business_question"],  # type: ignore[arg-type]
        catalog_fingerprint=data["catalog_fingerprint"],  # type: ignore[arg-type]
        approver=data["approver"],  # type: ignore[arg-type]
        inclusions=tuple(inclusions),
        exclusions=tuple(exclusions),
        acceptance_questions=tuple(acceptance_questions),
    )


# --------------------------------------------------------------------------- #
# Output helpers                                                              #
# --------------------------------------------------------------------------- #


def _emit_json(payload: Mapping[str, object]) -> None:
    """Write a deterministic JSON object (sorted keys) to stdout."""

    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Command handlers                                                            #
# --------------------------------------------------------------------------- #


def _handle_init(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    project_abs = project.resolve()
    # The catalog path is required and validated as project-contained before
    # the mutation lock is acquired, then normalized to the canonical
    # project-relative POSIX path persisted alongside the charter fingerprint.
    catalog_abs = _assert_contained(project, Path(args.catalog_path), kind="catalog")
    catalog_path = catalog_abs.relative_to(project_abs).as_posix()
    summary_root = args.summary_root or _DEFAULT_SUMMARY_ROOT
    _assert_contained(project, Path(summary_root), kind="summary_root")

    data = _load_charter_mapping(Path(args.charter))
    explicit_id = args.session_id
    if explicit_id is None:
        charter_id = data.get("session_id")
        explicit_id = charter_id if type(charter_id) is str and charter_id else None
    charter = _build_charter(
        data, explicit_session_id=explicit_id, catalog_path=catalog_path
    )
    session_dir = _session_dir(project, charter.session_id)
    actor = args.actor or charter.approver
    store = SessionStore.create(session_dir, charter=charter, actor=actor)
    snapshot = store.reconstruct()
    _emit_json(
        {
            "session_id": charter.session_id,
            "state": snapshot.state.value,
            "charter_fingerprint": charter.fingerprint,
            "workspace": _workspace_rel(charter.session_id),
        }
    )
    return EXIT_OK


def _handle_status(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _session_dir(project, session_id)
    if not (session_dir / "events.jsonl").exists():
        raise SessionError(CODE_NOT_INITIALIZED, safe_ids=(session_id,))
    store = SessionStore.open(session_dir)
    snapshot = store.reconstruct()
    charter_fingerprint: str | None = (
        snapshot.charter.fingerprint if snapshot.charter is not None else None
    )
    _emit_json(
        {
            "session_id": store.session_id,
            "state": snapshot.state.value,
            "charter_fingerprint": charter_fingerprint,
            "head_hash": snapshot.head_hash,
            "schema_version": SCHEMA_VERSION,
            "event_count": len(snapshot.events),
            "stale_nodes": list(snapshot.stale_nodes),
            "workspace": _workspace_rel(store.session_id),
        }
    )
    return EXIT_OK


def _handle_close(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _session_dir(project, session_id)
    if not (session_dir / "events.jsonl").exists():
        raise SessionError(CODE_NOT_INITIALIZED, safe_ids=(session_id,))
    store = SessionStore.open(session_dir)
    actor = args.actor or store.charter.approver
    store.close(actor=actor)
    snapshot = store.reconstruct()
    _emit_json(
        {
            "session_id": session_id,
            "state": snapshot.state.value,
            "head_hash": snapshot.head_hash,
        }
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Evidence intake handlers                                                   #
# --------------------------------------------------------------------------- #

#: Evidence store lives inside the Git-ignored session workspace.
_EVIDENCE_REL = "evidence"


def _evidence_root(session_dir: Path) -> Path:
    """Return the evidence store root for a session directory."""

    return session_dir / _EVIDENCE_REL


def _require_session(project: Path, session_id: str) -> Path:
    """Validate the session exists and return its directory."""

    session_dir = _session_dir(project, session_id)
    if not (session_dir / "events.jsonl").exists():
        raise SessionError(CODE_NOT_INITIALIZED, safe_ids=(session_id,))
    return session_dir


def _handle_intake_add_document(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = EvidenceStore.create(_evidence_root(session_dir))
    record = store.add_document(
        args.path,
        allowed_roots=(project,),
        source=args.source,
    )
    _emit_json(record.to_dict())
    return EXIT_OK


def _handle_profile_scan(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    charter = SessionStore.open(session_dir).charter
    catalog_abs = _assert_contained(
        project, project / charter.catalog_path, kind="catalog"
    )
    layer = SemanticLayer.load(catalog_abs)
    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        layer,
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({}),
    )
    try:
        with registry.open_scan_session(
            args.source_id,
            columns=tuple(args.columns),
            batch_size=args.batch_size,
        ) as session:
            profile = ProfileRunner(
                session,
                session_dir / "profile" / "spill",
                timeout=args.timeout,
                grain=tuple(args.grain),
            ).run()
        _emit_json(profile.to_dict())
    finally:
        registry.close()
    return EXIT_OK


def _handle_intake_snapshot(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    media_type = args.media_type
    if media_type not in (MEDIA_TEXT_MARKDOWN, MEDIA_TEXT_PLAIN):
        raise EvidenceError("discovery.evidence.invalid_media") from None
    raw = _read_stdin_bytes()
    store = EvidenceStore.create(_evidence_root(session_dir))
    record = store.add_snapshot(
        raw,
        media_type=media_type,
        source=args.source,
    )
    _emit_json(record.to_dict())
    return EXIT_OK


def _read_stdin_bytes() -> bytes:
    """Read raw bytes from stdin (never decodes or echoes content)."""

    handle = sys.stdin.buffer if sys.stdin is not None else None
    if handle is None:
        raise EvidenceError("discovery.evidence.invalid_encoding") from None
    return handle.read()


# --------------------------------------------------------------------------- #
# Sample-policy helpers                                                       #
# --------------------------------------------------------------------------- #

#: Policy artifacts (session salt, context export) live inside the Git-ignored
#: session workspace and never surface in Git-visible output. The salt itself
#: is never rendered; only its SHA-256 identifier (``salt_id``) appears in a
#: proposed policy.
_POLICY_REL = "policy"
_SALT_NAME = "salt"
_CONTEXT_PREFIX = "context"
_ACTIVATION_PREFIX = "activation"

#: A single safe filesystem path component (letters, digits, dot, hyphen,
#: underscore). Used to derive an export filename from a source id without
#: ever trusting it as a raw path.
_SAFE_FILE_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]")


def _policy_dir(session_dir: Path) -> Path:
    """Return the policy artifact directory for a session."""

    return session_dir / _POLICY_REL


def _restrict_file(path: Path) -> None:
    """Apply owner-only permissions on POSIX (best-effort elsewhere)."""

    if os.name == "posix":
        os.chmod(path, 0o600)


def _salt_path(session_dir: Path) -> Path:
    """Return the session salt path (inside the ignored workspace)."""

    return _policy_dir(session_dir) / _SALT_NAME


def _load_or_create_salt(session_dir: Path) -> bytes:
    """Load the session salt, generating and persisting it on first use.

    The salt stays in the Git-ignored workspace; only its SHA-256 identifier
    ever appears in policy output. Created atomically with owner-only
    permissions so a concurrent reader never observes a partial write.
    """

    path = _salt_path(session_dir)
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    _restrict_file(path)
    return salt


def _load_salt(session_dir: Path) -> bytes:
    """Load an existing session salt (errors if absent)."""

    path = _salt_path(session_dir)
    if not path.exists():
        raise _CliError(CODE_PROFILE_SALT) from None
    return path.read_bytes()


def _safe_file_name(value: str) -> str:
    """Return a filesystem-safe single path component from ``value``."""

    cleaned = _SAFE_FILE_RE.sub("-", value).strip("-")[:128]
    return cleaned or "source"


def _activation_path(session_dir: Path, source_id: str) -> Path:
    """Return the owner-only activation artifact path for one source.

    The activation binds the activated policy to its exact profile/schema/
    session/approver inputs; ``export-context`` loads and re-verifies it so a
    missing or changed binding fails closed.
    """

    return _policy_dir(session_dir) / f"{_ACTIVATION_PREFIX}-{_safe_file_name(source_id)}.json"


def _write_activation(
    session_dir: Path, source_id: str, activation: PolicyActivation
) -> Path:
    """Persist an activation artifact atomically with owner-only permissions."""

    path = _activation_path(session_dir, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(activation.to_dict(), sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_file(path)
    return path


def _load_activation(
    session_dir: Path, source_id: str
) -> PolicyActivation | None:
    """Load a persisted activation for ``source_id`` (``None`` if absent)."""

    path = _activation_path(session_dir, source_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise _CliError(CODE_PROFILE_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_PROFILE_LOAD) from None
    try:
        return PolicyActivation.from_dict(dict(data))
    except ProfilePolicyError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _normalized_approver(session_dir: Path, override: str | None) -> str:
    """Return the normalized current approver (override or charter approver)."""

    charter = SessionStore.open(session_dir).charter
    raw = override if override else charter.approver
    return normalize_actor_identity(raw)


def _context_export_path(session_dir: Path, source_id: str) -> Path:
    """Return the context export artifact path for one source."""

    return _policy_dir(session_dir) / f"{_CONTEXT_PREFIX}-{_safe_file_name(source_id)}.json"


def _session_context_bytes_used(session_dir: Path) -> int:
    """Sum the bytes of every persisted context export in the session.

    Derives prior session export usage from the Git-ignored workspace so the
    ``bytes_per_session`` cap is enforced across multiple exports. Best-effort:
    a corrupt or unreadable file contributes zero (it cannot inflate the cap).
    """

    policy_dir = _policy_dir(session_dir)
    if not policy_dir.exists():
        return 0
    total = 0
    for entry in policy_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.startswith(f"{_CONTEXT_PREFIX}-") or not entry.name.endswith(".json"):
            continue
        try:
            with entry.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping):
            raw = data.get("bytes")
            if type(raw) is int:
                total += raw
    return total


def _load_json_mapping(project: Path, raw_path: str, *, kind: str) -> dict[str, object]:
    """Load a JSON mapping from a project-contained path (never leaks content)."""

    abs_path = _assert_contained(project, Path(raw_path), kind=kind)
    try:
        with abs_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise _CliError(CODE_PROFILE_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_PROFILE_LOAD) from None
    return dict(data)


def _load_profile_file(project: Path, raw_path: str) -> SourceProfile:
    """Load a :class:`SourceProfile` from a project-contained JSON file."""

    data = _load_json_mapping(project, raw_path, kind="profile")
    try:
        return SourceProfile.from_dict(data)
    except ProfilePolicyError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _load_policy_file(project: Path, raw_path: str) -> SamplePolicy:
    """Load a :class:`SamplePolicy` from a project-contained JSON file."""

    data = _load_json_mapping(project, raw_path, kind="policy")
    try:
        return SamplePolicy.from_dict(data)
    except ProfilePolicyError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _open_registry(
    project: Path, session_dir: Path
) -> tuple[SemanticLayer, duckdb.DuckDBPyConnection, SourceRegistry]:
    """Open the source catalog and registry for a session."""

    charter = SessionStore.open(session_dir).charter
    catalog_abs = _assert_contained(
        project, project / charter.catalog_path, kind="catalog"
    )
    layer = SemanticLayer.load(catalog_abs)
    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        layer,
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({}),
    )
    return layer, connection, registry


# --------------------------------------------------------------------------- #
# Sample-policy command handlers                                             #
# --------------------------------------------------------------------------- #


def _handle_profile_propose_policy(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    profile = _load_profile_file(project, args.profile)
    grain = tuple(args.grain)
    salt = _load_or_create_salt(session_dir)
    salt_id = hashlib.sha256(salt).hexdigest()
    policy, classifications = propose_policy(profile, grain, salt_id=salt_id)
    payload = policy.to_dict()
    payload["classifications"] = [item.to_dict() for item in classifications]
    _emit_json(payload)
    return EXIT_OK


def _handle_profile_activate_policy(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    profile = _load_profile_file(project, args.profile)
    policy = _load_policy_file(project, args.policy)
    charter = SessionStore.open(session_dir).charter
    approver = normalize_actor_identity(args.approver or charter.approver)
    activated_at = args.activated_at or ""
    activation = activate_policy(
        policy,
        profile,
        session_id=session_id,
        approver=approver,
        activated_at=activated_at,
    )
    # Persist a safe activation artifact under the ignored session policy
    # directory so export-context can load and re-verify the current bindings.
    _write_activation(session_dir, profile.source_id, activation)
    _emit_json(activation.to_dict())
    return EXIT_OK


def _handle_profile_export_context(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    project_abs = project.resolve()
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    profile = _load_profile_file(project, args.profile)
    policy = _load_policy_file(project, args.policy)
    salt = _load_salt(session_dir)
    # Fail closed when no activation artifact exists for this source, or when
    # any current binding (normalized charter approver, session/source ids,
    # policy/profile/schema fingerprints, snapshot, grain) no longer matches the
    # persisted activation.
    activation = _load_activation(session_dir, args.source_id)
    if activation is None:
        raise ProfilePolicyError(
            CODE_PROFILE_STALE, safe_detail="activation"
        ) from None
    approver = _normalized_approver(session_dir, None)
    verify_activation(
        activation,
        policy,
        profile,
        session_id=session_id,
        source_id=args.source_id,
        approver=approver,
    )
    # Enforce the session cap across multiple exports by summing the bytes of
    # every prior context export persisted in the ignored session workspace.
    session_bytes_used = _session_context_bytes_used(session_dir)
    _, _, registry = _open_registry(project, session_dir)
    try:
        with registry.open_scan_session(
            args.source_id,
            columns=tuple(args.columns),
            batch_size=args.batch_size,
        ) as session:
            export = build_context_export(
                session,
                policy,
                profile,
                salt,
                session_id=session_id,
                session_bytes_used=session_bytes_used,
            )
    finally:
        registry.close()
    out_path = _context_export_path(session_dir, args.source_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(export.to_dict(), sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_file(out_path)
    # Emit only a safe path plus binding fingerprints/hashes. No source/session
    # identifiers, counts, byte count, or canary status reach stdout.
    _emit_json(
        {
            "path": out_path.relative_to(project_abs).as_posix(),
            "fingerprint": export.fingerprint,
            "policy_fingerprint": export.policy_fingerprint,
            "profile_fingerprint": export.profile_fingerprint,
            "schema_fingerprint": export.schema_fingerprint,
        }
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parser                                                             #
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selayer-discovery",
        description="Deterministic agent-assisted semantic discovery for selayer.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    session_parser = subparsers.add_parser("session", help="Manage discovery sessions.")
    session_sub = session_parser.add_subparsers(dest="session_command", required=True)

    init_parser = session_sub.add_parser(
        "init", help="Initialize a new discovery session."
    )
    init_parser.add_argument("--charter", required=True, help="Charter YAML path.")
    init_parser.add_argument("--project", help="Project root (default: cwd).")
    init_parser.add_argument(
        "--session-id", dest="session_id", help="Explicit session id."
    )
    init_parser.add_argument(
        "--catalog-path",
        dest="catalog_path",
        required=True,
        help="Catalog file path (must be project-contained).",
    )
    init_parser.add_argument(
        "--summary-root",
        dest="summary_root",
        help="Approved-summary root (must be project-contained).",
    )
    init_parser.add_argument("--actor", help="Actor identity (default: approver).")
    init_parser.set_defaults(func=_handle_init)

    status_parser = session_sub.add_parser(
        "status", help="Rebuild and report session status from events."
    )
    status_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    status_parser.add_argument("--project", help="Project root (default: cwd).")
    status_parser.set_defaults(func=_handle_status)

    close_parser = session_sub.add_parser(
        "close", help="Close a discovery session (terminal)."
    )
    close_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    close_parser.add_argument("--project", help="Project root (default: cwd).")
    close_parser.add_argument("--actor", help="Actor identity (default: approver).")
    close_parser.set_defaults(func=_handle_close)

    intake_parser = subparsers.add_parser(
        "intake", help="Capture normalized evidence into a session."
    )
    intake_sub = intake_parser.add_subparsers(dest="intake_command", required=True)

    add_document_parser = intake_sub.add_parser(
        "add-document", help="Ingest a Markdown or plain-text document."
    )
    add_document_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    add_document_parser.add_argument("--project", help="Project root (default: cwd).")
    add_document_parser.add_argument(
        "--path", required=True, help="Document path (must be project-contained)."
    )
    add_document_parser.add_argument(
        "--source",
        help="Source label (default: project-relative POSIX path).",
    )
    add_document_parser.set_defaults(func=_handle_intake_add_document)

    snapshot_parser = intake_sub.add_parser(
        "snapshot", help="Store normalized text content read from stdin."
    )
    snapshot_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    snapshot_parser.add_argument("--project", help="Project root (default: cwd).")
    snapshot_parser.add_argument(
        "--source", required=True, help="Source label for the snapshot."
    )
    snapshot_parser.add_argument(
        "--media-type",
        dest="media_type",
        required=True,
        help="Media type (text/markdown or text/plain).",
    )
    snapshot_parser.set_defaults(func=_handle_intake_snapshot)

    profile_parser = subparsers.add_parser(
        "profile", help="Profile a bounded source scan."
    )
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)
    scan_parser = profile_sub.add_parser(
        "scan", help="Compute exact aggregate profile metadata."
    )
    scan_parser.add_argument("--session-id", required=True)
    scan_parser.add_argument("--source-id", required=True)
    scan_parser.add_argument("--project", help="Project root (default: cwd).")
    scan_parser.add_argument("--timeout", type=float, default=900.0)
    scan_parser.add_argument("--batch-size", type=int, default=1024)
    scan_parser.add_argument("--grain", action="append", default=[])
    scan_parser.add_argument("--columns", action="append", default=[])
    scan_parser.set_defaults(func=_handle_profile_scan)

    propose_parser = profile_sub.add_parser(
        "propose-policy", help="Propose a conservative sample policy."
    )
    propose_parser.add_argument("--session-id", required=True)
    propose_parser.add_argument("--project", help="Project root (default: cwd).")
    propose_parser.add_argument("--profile", required=True)
    propose_parser.add_argument("--grain", action="append", default=[])
    propose_parser.set_defaults(func=_handle_profile_propose_policy)

    activate_parser = profile_sub.add_parser(
        "activate-policy", help="Activate a named sample policy."
    )
    activate_parser.add_argument("--session-id", required=True)
    activate_parser.add_argument("--project", help="Project root (default: cwd).")
    activate_parser.add_argument("--profile", required=True)
    activate_parser.add_argument("--policy", required=True)
    activate_parser.add_argument("--approver")
    activate_parser.add_argument("--activated-at", default="")
    activate_parser.set_defaults(func=_handle_profile_activate_policy)

    export_parser = profile_sub.add_parser(
        "export-context", help="Export bounded transformed context metadata."
    )
    export_parser.add_argument("--session-id", required=True)
    export_parser.add_argument("--source-id", required=True)
    export_parser.add_argument("--project", help="Project root (default: cwd).")
    export_parser.add_argument("--profile", required=True)
    export_parser.add_argument("--policy", required=True)
    export_parser.add_argument("--timeout", type=float, default=900.0)
    export_parser.add_argument("--batch-size", type=int, default=1024)
    export_parser.add_argument("--columns", action="append", default=[])
    export_parser.set_defaults(func=_handle_profile_export_context)

    return parser


# --------------------------------------------------------------------------- #
# Entry points                                                                #
# --------------------------------------------------------------------------- #


def _run(handler: object, args: argparse.Namespace) -> int:
    """Invoke a handler, rendering safe diagnostics on any failure."""

    try:
        return handler(args)  # type: ignore[operator]
    except SystemExit:
        raise
    except (
        SessionError,
        EvidenceError,
        _CliError,
        ProfilePolicyError,
        DiscoveryError,
    ) as exc:
        _emit_error(exc)
        return EXIT_ERROR
    except Exception:
        if getattr(args, "debug", False):
            raise
        _emit_error(_CliError(CODE_INTERNAL))
        return EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    # parse_args raises SystemExit(0) for --help and SystemExit(2) for usage
    # errors; both propagate so callers observe the conventional codes.
    args = parser.parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return _run(handler, args)


def run() -> None:
    raise SystemExit(main())
