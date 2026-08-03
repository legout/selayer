"""Runtime profile-file parsing into a :class:`MappingProfileResolver`.

A *profile document* is a small version-1 YAML mapping that declares named
profiles of credential/option values.  Each profile is a mapping of *keys* to
*sources*; a source resolves its value from one of exactly two places:

* ``env`` — read from the process environment (or an injected mapping) at load
  time, accepted only as an exact builtin ``str`` so a hostile ``str`` subclass
  (whose own ``__repr__`` could leak a secret) is rejected rather than retained;
* ``literal`` — an inline scalar/structure copied verbatim from the document.

Resolution never retains or renders any resolved value or environment value.
Every failure — a malformed document, a duplicate key, an unsupported version,
an unknown top-level key, an invalid profile name, an invalid source shape, an
invalid environment name, or a missing environment variable — surfaces as a
:class:`ProfileFileValidationError` carrying only a stable ``code``, a
structural document ``path``, and a *constant* message looked up from a fixed
code-to-message mapping.  The error is always raised *outside* an active
``except`` scope, so ``__cause__`` and ``__context__`` remain ``None``.

Runtime resolver failures (an unknown profile *name* at resolve time) remain
the responsibility of :class:`~selayer.sources.errors.SourceProfileError`, which
is unchanged here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from selayer.sources.profiles import MappingProfileResolver

__all__ = ["ProfileFileValidationError", "load_profile_file"]


# Constant, generic messages keyed by stable error code.  Only these constant
# strings are ever stored or rendered by a :class:`ProfileFileValidationError`;
# no resolved value, environment value, or environment name surfaces.
_CODE_MESSAGES: dict[str, str] = {
    "source.profile.malformed": "the profile document is not valid YAML",
    "source.profile.duplicate_key": "the profile document contains a duplicate key",
    "source.profile.not_mapping": "the profile document root must be a mapping",
    "source.profile.unknown_key": "the profile document has an unknown top-level key",
    "source.profile.wrong_version": "the profile document version is not supported",
    "source.profile.profiles_not_mapping": "the profiles section must be a mapping",
    "source.profile.invalid_profile_name": "a profile name is not a valid identifier",
    "source.profile.entry_not_mapping": "a profile entry must be a mapping",
    "source.profile.missing_source": "a profile entry must declare exactly one source",
    "source.profile.ambiguous_source": (
        "a profile entry must declare exactly one source"
    ),
    "source.profile.invalid_environment_name": (
        "a profile environment name is not valid"
    ),
    "source.profile.missing_environment": (
        "a required profile environment variable is missing"
    ),
    "source.profile.invalid_environment_value": (
        "a profile environment value is invalid"
    ),
}

_FALLBACK_MESSAGE = "a profile document could not be loaded"

# Identifier shape shared with the catalog source-name convention
# (lowercase snake_case).  A profile name must match it exactly to be retained
# or interpolated into a structural path; a non-conformant name is rejected
# (and never rendered) so hostile text can never reach a diagnostic.
_PROFILE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")

# Environment-variable-name shape: a leading letter or underscore followed by
# letters, digits, or underscores.  Only an env name matching this shape is
# looked up in the environment; a malformed name is rejected at load time (and
# never rendered) so hostile text can never reach a diagnostic.
_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# The closed set of permitted top-level document keys.
_TOP_LEVEL_KEYS = frozenset({"version", "profiles"})


class ProfileFileValidationError(Exception):
    """Raised when a profile document cannot be loaded or resolved safely.

    Only safe, derived attributes are stored:

    * ``code`` — a stable symbolic code from a fixed allowlist.
    * ``path`` — a structural document path (never a resolved value).
    * ``message`` — a constant generic message looked up from ``code``.

    No resolved profile value or environment value is ever stored or rendered.
    The attributes are set once at construction and are not mutated thereafter.

    Attributes:
        code: stable symbolic error code (constant, no credentials).
        path: structural document path (constant, no credentials).
        message: constant, sanitized human-readable detail.
    """

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        self.message = _CODE_MESSAGES.get(code, _FALLBACK_MESSAGE)
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"ProfileFileValidationError(code={self.code!r}, path={self.path!r})"
        )


def load_profile_file(
    path: str | Path,
    *,
    environ: Mapping[str, str] = os.environ,
) -> MappingProfileResolver:
    """Load a version-1 profile document into a profile resolver.

    Each profile value is resolved from ``env`` (read from *environ*, accepted
    only as an exact builtin ``str``) or ``literal`` (copied verbatim).  Any
    malformed document or unresolvable value raises a sanitized
    :class:`ProfileFileValidationError` outside any ``except`` scope; no
    resolved or environment value is ever retained or rendered.
    """
    document = _compose_without_duplicate_keys(Path(path))
    profiles = _validate_document_shape(document)
    resolved = _resolve_profiles(profiles, environ)
    return MappingProfileResolver(resolved)


def _compose_without_duplicate_keys(path: Path) -> object:
    """Read and parse a profile document, rejecting duplicate mapping keys.

    Duplicate keys (which PyYAML would silently collapse) are detected by
    walking the composed node tree and rejected *before* construction, so a
    silently-shadowed value can never reach resolution.  Returns the
    constructed document, or ``None`` for an empty/null document.  A
    syntactically invalid document is reported as a constant-code validation
    error; the PyYAML diagnostic (which may quote document text) is discarded.
    """
    text = path.read_text(encoding="utf-8")
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _reject_duplicate_keys(node)
        data = loader.construct_document(node)
    except yaml.YAMLError:
        raise ProfileFileValidationError(
            "source.profile.malformed", str(path)
        ) from None
    finally:
        loader.dispose()
    return data


def _reject_duplicate_keys(node: yaml.Node) -> None:
    """Walk a YAML node tree raising on the first duplicate mapping key."""

    _walk_for_duplicate_keys(node, "")


def _walk_for_duplicate_keys(node: yaml.Node, path: str) -> None:
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            child_path = path
            if isinstance(key_node, yaml.ScalarNode) and isinstance(
                key_node.value, str
            ):
                key = key_node.value
                child_path = f"{path}.{key}" if path else key
                if key in seen:
                    raise ProfileFileValidationError(
                        "source.profile.duplicate_key", child_path
                    )
                seen.add(key)
            _walk_for_duplicate_keys(value_node, child_path)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _walk_for_duplicate_keys(item, f"{path}[{index}]")


def _validate_document_shape(
    document: object,
) -> dict[str, Mapping[object, object]]:
    """Validate the top-level document structure and profile-name shape.

    Every structural failure raises a :class:`ProfileFileValidationError`
    before any environment value is read, so a structurally invalid document
    can never cause a resolved value to be retained.  Returns the validated
    profiles section typed as ``{profile_name: entry_mapping}``.
    """
    if not isinstance(document, Mapping):
        raise ProfileFileValidationError("source.profile.not_mapping", "profiles")
    for key in document:
        if key not in _TOP_LEVEL_KEYS:
            raise ProfileFileValidationError("source.profile.unknown_key", str(key))
    if document.get("version") != 1:
        raise ProfileFileValidationError("source.profile.wrong_version", "version")
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, Mapping):
        raise ProfileFileValidationError(
            "source.profile.profiles_not_mapping", "profiles"
        )
    profiles: dict[str, Mapping[object, object]] = {}
    for name in raw_profiles:
        if not (isinstance(name, str) and _PROFILE_NAME_RE.fullmatch(name)):
            # The non-conformant name is never rendered: the structural path is
            # the generic ``"profiles"`` so hostile text cannot reach a
            # diagnostic.
            raise ProfileFileValidationError(
                "source.profile.invalid_profile_name", "profiles"
            )
        entries = raw_profiles[name]
        if not isinstance(entries, Mapping):
            raise ProfileFileValidationError(
                "source.profile.entry_not_mapping", f"profiles.{name}"
            )
        profiles[name] = entries
    return profiles


def _resolve_profiles(
    profiles: Mapping[str, Mapping[object, object]],
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Resolve every profile entry, raising on any unresolvable source."""

    resolved: dict[str, dict[str, object]] = {}
    for name, entries in profiles.items():
        values: dict[str, object] = {}
        for key, source in entries.items():
            _resolve_entry(name, key, source, environ, values)
        resolved[name] = values
    return resolved


def _resolve_entry(
    name: str,
    key: object,
    source: object,
    environ: Mapping[str, str],
    values: dict[str, object],
) -> None:
    """Resolve a single profile entry source into *values*.

    The resolved value is stored only in the local *values* mapping (which the
    caller hands to :class:`MappingProfileResolver`, whose
    :class:`~selayer.sources.profiles.RuntimeProfile` excludes values from its
    repr).  No resolved value or environment value is ever interpolated into an
    error; every failure carries only a constant message and a structural path.
    The entry key is a YAML string in every well-formed profile document and is
    rendered only through a structural path.
    """
    key_name = str(key)
    entry_path = f"profiles.{name}.{key_name}"
    if not isinstance(source, Mapping):
        raise ProfileFileValidationError(
            "source.profile.entry_not_mapping", entry_path
        )
    has_env = "env" in source
    has_literal = "literal" in source
    if has_env and has_literal:
        raise ProfileFileValidationError(
            "source.profile.ambiguous_source", entry_path
        )
    if not has_env and not has_literal:
        raise ProfileFileValidationError("source.profile.missing_source", entry_path)
    if has_env:
        env_name = source["env"]
        if not (isinstance(env_name, str) and _ENV_NAME_RE.fullmatch(env_name)):
            # A malformed env name is never rendered: the path is the structural
            # ``...env`` location, not the name itself.
            raise ProfileFileValidationError(
                "source.profile.invalid_environment_name", f"{entry_path}.env"
            )
        if env_name not in environ:
            raise ProfileFileValidationError(
                "source.profile.missing_environment", f"{entry_path}.env"
            )
        value = environ[env_name]
        # Only an exact builtin ``str`` is accepted: a hostile ``str`` subclass
        # (whose ``__repr__`` could leak a secret when rendered) is rejected,
        # and the value itself is never retained in the error.
        if type(value) is not str:
            raise ProfileFileValidationError(
                "source.profile.invalid_environment_value", f"{entry_path}.env"
            )
        values[key_name] = value
    else:
        values[key_name] = source["literal"]
