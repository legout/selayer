"""Runtime profile-file parsing into a :class:`MappingProfileResolver`.

A *profile document* is a small version-1 YAML mapping that declares named
profiles of credential/option values.  Each profile is a non-empty mapping of
*keys* to *sources*; a source resolves its value from one of exactly two
places:

* ``env`` — read from the process environment (or an injected mapping) at load
  time, accepted only as an exact builtin ``str`` so a hostile ``str`` subclass
  (whose own ``__repr__`` could leak a secret) is rejected rather than retained;
* ``literal`` — an inline *boolean* copied verbatim from the document.  Only an
  exact builtin ``bool`` is accepted: strings, numbers, ``null``, sequences, and
  mappings are rejected so a credential-bearing value can never be inlined into
  a profile document (and never rendered by an error).

Each entry mapping may declare *only* ``env`` and/or ``literal`` — any other
field (including a YAML merge key ``<<``) is rejected — and exactly one of the
two.  The document version must be the integer ``1`` (``type(value) is int``),
``profiles`` must be a non-empty mapping, and every named profile must be a
non-empty mapping.

Resolution never retains or renders any resolved value or environment value.
Every failure surfaces as an immutable :class:`ProfileFileValidationError`
carrying only a stable ``code``, a structural document ``path`` composed
exclusively of *validated* identifier tokens and fixed redaction tokens, and a
*constant* message looked up from a fixed code-to-message mapping.  No raw YAML
key, value, or secret ever reaches ``repr``, ``str``, ``args``, ``path``,
``message``, the traceback, or ``__cause__``/``__context__``.  The
caller-supplied filesystem path is never stored: a malformed document and any
file I/O failure (a missing or unreadable file, or one that is not valid
UTF-8) use a fixed safe path token.
Every error is constructed and raised *outside* an active ``except`` scope, so
``__cause__`` and ``__context__`` remain ``None`` (a YAML parse failure is
captured, the raw PyYAML exception discarded, and the sanitized error raised
after the ``except``/``finally`` completes; a missing/unreadable file, or one
that is not valid UTF-8, is captured and the raw ``OSError`` or
``UnicodeDecodeError`` discarded the same way).

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
# no resolved value, environment value, or raw YAML key surfaces.
_CODE_MESSAGES: dict[str, str] = {
    "source.profile.malformed": "the profile document is not valid YAML",
    "source.profile.file_missing": "the profile file does not exist",
    "source.profile.file_unreadable": "the profile file could not be read",
    "source.profile.invalid_utf8": "the profile file is not valid UTF-8",
    "source.profile.merge_key": "the profile document uses an unsupported merge key",
    "source.profile.duplicate_key": "the profile document contains a duplicate key",
    "source.profile.not_mapping": "the profile document root must be a mapping",
    "source.profile.unknown_key": "the profile document has an unknown top-level key",
    "source.profile.wrong_version": "the profile document version is not supported",
    "source.profile.profiles_not_mapping": "the profiles section must be a mapping",
    "source.profile.profiles_empty": (
        "the profiles section must declare at least one profile"
    ),
    "source.profile.invalid_profile_name": "a profile name is not a valid identifier",
    "source.profile.entry_not_mapping": "a profile entry must be a mapping",
    "source.profile.profile_empty": "a profile must declare at least one entry",
    "source.profile.unknown_source_field": (
        "a profile entry has an unknown source field"
    ),
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
    "source.profile.invalid_literal": "a profile literal value must be a boolean",
}

_FALLBACK_MESSAGE = "a profile document could not be loaded"

# Identifier shape shared with the catalog source-name convention
# (lowercase snake_case).  A profile name, an entry key, or a structural path
# token must match it exactly to be retained or interpolated into a structural
# path; a non-conformant value is rejected (profile names) or redacted to a
# fixed token (entry/path keys) so hostile text can never reach a diagnostic.
_IDENTIFIER_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")

# Environment-variable-name shape: a leading letter or underscore followed by
# letters, digits, or underscores.  Only an env name matching this shape is
# looked up in the environment; a malformed name is rejected at load time (and
# never rendered) so hostile text can never reach a diagnostic.
_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# The closed set of permitted top-level document keys, and the only permitted
# source-field names within an entry mapping.
_TOP_LEVEL_KEYS = frozenset({"version", "profiles"})
_SOURCE_FIELDS = frozenset({"env", "literal"})

# Fixed structural path tokens used wherever a raw, unvalidated value must not
# be interpolated into a diagnostic path.
_ROOT_PATH = "<document>"
_UNKNOWN_KEY_PATH = "top_level"
_REDACTED_KEY = "<key>"
_FILE_PATH = "<file>"


class ProfileFileValidationError(Exception):
    """Raised when a profile document cannot be loaded or resolved safely.

    Only safe, derived attributes are stored, once, at construction, and are
    immutable thereafter:

    * ``code`` — a stable symbolic code (a constant, never a credential).
    * ``path`` — a structural document path built only from *validated*
      identifier tokens and fixed redaction tokens (never a raw value).
    * ``message`` — a constant generic message looked up from ``code``.

    No resolved profile value, environment value, or raw YAML key/value is ever
    stored or rendered.  The class uses ``__slots__`` and overrides
    ``__setattr__``/``__delattr__`` to reject mutation, so ``code``, ``path``,
    and ``message`` cannot be altered after construction.

    Attributes:
        code: stable symbolic error code (constant, no credentials).
        path: structural document path (constant, no credentials).
        message: constant, sanitized human-readable detail.
    """

    __slots__ = ("code", "message", "path")

    def __init__(self, code: str, path: str) -> None:
        # All attributes are set exactly once via the base ``__setattr__`` so
        # the immutability guard below cannot block construction.  ``code`` and
        # ``path`` are only ever constructed internally from constant codes and
        # structural paths composed of validated tokens.
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "message", _CODE_MESSAGES.get(code, _FALLBACK_MESSAGE))
        # ``BaseException.__init__`` writes ``args`` through the C-level slot
        # and does not route through this class's ``__setattr__``; ``args`` is
        # therefore the single constant ``message`` (never a raw value).
        super().__init__(self.message)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ProfileFileValidationError is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ProfileFileValidationError is immutable")

    def __repr__(self) -> str:
        return (
            f"ProfileFileValidationError(code={self.code!r}, "
            f"path={self.path!r}, message={self.message!r})"
        )


def load_profile_file(
    path: str | Path,
    *,
    environ: Mapping[str, str] = os.environ,
) -> MappingProfileResolver:
    """Load a version-1 profile document into a profile resolver.

    Each profile value is resolved from ``env`` (read from *environ*, accepted
    only as an exact builtin ``str``) or ``literal`` (an exact builtin ``bool``
    copied verbatim).  Any missing/unreadable/non-UTF-8 file, malformed
    document, unresolvable value, or disallowed literal raises an immutable,
    sanitized :class:`ProfileFileValidationError` outside any ``except``
    scope; no
    resolved value, environment value, or raw YAML key is ever retained or
    rendered.
    """
    document = _compose_without_duplicate_keys(Path(path))
    profiles = _validate_document_shape(document)
    resolved = _resolve_profiles(profiles, environ)
    return MappingProfileResolver(resolved)


def _compose_without_duplicate_keys(path: Path) -> object:
    """Read and parse a profile document, rejecting duplicate and merge keys.

    Duplicate keys (which PyYAML would silently collapse) and YAML merge keys
    (``<<``, which PyYAML would silently flatten) are detected by walking the
    composed node tree and rejected *before* construction, so a silently
    shadowed or merged value can never reach resolution.  Returns the
    constructed document, or ``None`` for an empty/null document.

    File-system failures (a missing or unreadable file, or a file that is not
    valid UTF-8) and syntactically invalid documents are reported as
    constant-code validation errors.  In each
    case the raw exception is captured inside the ``except`` and the sanitized
    error is raised *after* the ``except``/``finally`` completes — outside any
    active ``except`` scope — using a fixed safe path token (never the
    caller-supplied filesystem path, which may itself carry a secret), so no
    raw exception reaches ``__cause__``, ``__context__``, the traceback,
    ``args``, ``path``, or ``message``.
    """
    # Read the file first.  A missing or unreadable file, or one that is not
    # valid UTF-8, is captured (never retained) and reported with the fixed safe
    # path token; the caller-supplied filesystem path is never stored.  ``text``
    # is pre-bound so it is always a bound ``str`` even though the placeholder
    # is never used (an I/O failure raises before it can be read).
    text = ""
    io_error: ProfileFileValidationError | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        io_error = ProfileFileValidationError(
            "source.profile.file_missing", _FILE_PATH
        )
    except OSError:
        io_error = ProfileFileValidationError(
            "source.profile.file_unreadable", _FILE_PATH
        )
    except UnicodeDecodeError:
        # ``UnicodeDecodeError`` is a ``ValueError`` (not an ``OSError``), so it
        # is not caught by the ``except OSError`` above and must be matched
        # explicitly.  The raw, undecodable bytes — which may carry a secret
        # and which the default error renders via ``args``/``object`` — are
        # captured and discarded, and the sanitized error is raised below
        # outside any active ``except`` scope using the fixed safe path token.
        io_error = ProfileFileValidationError(
            "source.profile.invalid_utf8", _FILE_PATH
        )
    if io_error is not None:
        # Raised outside the ``except`` so __cause__/__context__ are None and
        # the traceback carries no raw OSError frames.
        raise io_error

    loader = yaml.SafeLoader(text)
    parse_error: ProfileFileValidationError | None = None
    data: object = None
    try:
        node = loader.get_single_node()
        if node is not None:
            # Raises ProfileFileValidationError (not a yaml.YAMLError), so it
            # propagates straight through ``except yaml.YAMLError``; being
            # raised inside the ``try`` (not an ``except``) leaves
            # ``__context__`` None.
            _reject_duplicate_and_merge_keys(node)
            data = loader.construct_document(node)
    except yaml.YAMLError:
        # Capture only the constant code; discard the raw PyYAML exception and
        # use the fixed safe path token (never the filesystem path).  The
        # sanitized error is raised below, outside any active ``except`` scope.
        parse_error = ProfileFileValidationError(
            "source.profile.malformed", _ROOT_PATH
        )
    finally:
        loader.dispose()
    if parse_error is not None:
        # Raised outside the ``except`` so __cause__/__context__ are None and
        # the traceback carries no PyYAML frames.
        raise parse_error
    return data


def _reject_duplicate_and_merge_keys(node: yaml.Node) -> None:
    """Walk a YAML node tree raising on the first merge or duplicate key."""

    _walk_for_duplicate_and_merge_keys(node, "")


def _walk_for_duplicate_and_merge_keys(node: yaml.Node, path: str) -> None:
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                key = key_node.value
                # A merge key is an unsupported construct anywhere in the
                # document; reject it before construction can flatten it.
                if key == "<<":
                    raise ProfileFileValidationError(
                        "source.profile.merge_key", path or _ROOT_PATH
                    )
                if isinstance(key, str):
                    # The error path renders only a validated identifier token;
                    # a hostile key is redacted.  Duplicate detection compares
                    # the raw key (never rendered) so two hostile keys that
                    # redact identically are still distinguished internally.
                    safe = _safe_key_token(key)
                    child_path = f"{path}.{safe}" if path else safe
                    if key in seen:
                        raise ProfileFileValidationError(
                            "source.profile.duplicate_key", child_path
                        )
                    seen.add(key)
                    _walk_for_duplicate_and_merge_keys(value_node, child_path)
                    continue
            _walk_for_duplicate_and_merge_keys(value_node, path)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _walk_for_duplicate_and_merge_keys(item, f"{path}[{index}]")


def _validate_document_shape(
    document: object,
) -> dict[str, Mapping[object, object]]:
    """Validate the top-level document structure and profile-name shape.

    Every structural failure raises a :class:`ProfileFileValidationError`
    before any environment value is read, so a structurally invalid document
    can never cause a resolved value to be retained.  Returns the validated,
    non-empty profiles section typed as ``{profile_name: entry_mapping}``.
    """
    if not isinstance(document, Mapping):
        raise ProfileFileValidationError("source.profile.not_mapping", "profiles")
    for key in document:
        # An unknown top-level key is never rendered: the structural path is
        # the fixed ``top_level`` token so a hostile key cannot reach a
        # diagnostic.
        if key not in _TOP_LEVEL_KEYS:
            raise ProfileFileValidationError(
                "source.profile.unknown_key", _UNKNOWN_KEY_PATH
            )
    version = document.get("version")
    # The version must be the integer 1 exactly: ``type(value) is int`` rejects
    # ``1.0`` (float), ``true`` (bool, which equals 1), and missing/null values.
    if type(version) is not int or version != 1:
        raise ProfileFileValidationError("source.profile.wrong_version", "version")
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, Mapping):
        raise ProfileFileValidationError(
            "source.profile.profiles_not_mapping", "profiles"
        )
    if not raw_profiles:
        raise ProfileFileValidationError(
            "source.profile.profiles_empty", "profiles"
        )
    profiles: dict[str, Mapping[object, object]] = {}
    for name in raw_profiles:
        if not (type(name) is str and _IDENTIFIER_RE.fullmatch(name)):
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
        if not entries:
            raise ProfileFileValidationError(
                "source.profile.profile_empty", f"profiles.{name}"
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
    error; every failure carries only a constant message and a structural path
    whose only interpolated component is a validated profile name plus a
    redacted entry-key token.
    """
    # The entry key is stored verbatim (coerced to ``str``) so adapters can
    # look it up; only the *error path* is sanitized via ``_safe_key_token``.
    key_name = str(key)
    entry_path = f"profiles.{name}.{_safe_key_token(key)}"
    if not isinstance(source, Mapping):
        raise ProfileFileValidationError(
            "source.profile.entry_not_mapping", entry_path
        )
    # An entry may declare only ``env`` and/or ``literal``; any other field is
    # rejected (merge keys are already rejected during composition).
    if not set(source) <= _SOURCE_FIELDS:
        raise ProfileFileValidationError(
            "source.profile.unknown_source_field", entry_path
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
        if not (type(env_name) is str and _ENV_NAME_RE.fullmatch(env_name)):
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
        literal = source["literal"]
        # Only an exact builtin ``bool`` literal is accepted: strings, numbers,
        # ``null``, sequences, and mappings are rejected so a credential-bearing
        # value can never be inlined (and never rendered by an error).
        if type(literal) is not bool:
            raise ProfileFileValidationError(
                "source.profile.invalid_literal", entry_path
            )
        values[key_name] = literal


def _safe_key_token(value: object) -> str:
    """Render a structural-path key token, redacting hostile values.

    Only an *exact* builtin ``str`` that matches the identifier shape is
    rendered; a hostile ``str`` subclass (whose custom ``__repr__`` could leak a
    secret), a non-string, or a non-conformant key is redacted to the fixed
    ``<key>`` token so it can never reach a diagnostic path.
    """
    return (
        value
        if type(value) is str and _IDENTIFIER_RE.fullmatch(value)
        else _REDACTED_KEY
    )
