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
``profiles`` must be a non-empty mapping, every named profile must be a
non-empty mapping, and every entry *key* must be an exact builtin ``str``
(a YAML int/float/bool/null key is rejected *before* resolution rather than
str-coerced, so a numeric key can never collide with its string spelling).

Resolution never retains or renders any resolved value or environment value.
Every failure surfaces as an immutable :class:`ProfileFileValidationError`
carrying only a stable ``code``, a structural document ``path`` composed
exclusively of *validated* identifier tokens and fixed redaction tokens, and a
*constant* message looked up from a fixed code-to-message mapping.  No raw YAML
key, value, or secret ever reaches ``repr``, ``str``, ``args``, ``path``,
``message``, the traceback, or ``__cause__``/``__context__``.  The
caller-supplied filesystem path is never stored: a malformed document and
any file I/O failure (a missing or unreadable file, one that is not valid
UTF-8, a path that cannot be encoded to the filesystem encoding, or a path
with an embedded NUL) use a fixed safe path token.
Every error is constructed and raised *outside* an active ``except`` scope, so
``__cause__`` and ``__context__`` remain ``None`` (a YAML parse failure is
captured, the raw PyYAML exception discarded, and the sanitized error raised
after the ``except``/``finally`` completes; a missing/unreadable file, one
that is not valid UTF-8, a path that cannot be encoded to the filesystem
encoding, or a path with an embedded NUL, is captured and the raw
``OSError``, ``UnicodeDecodeError``, ``UnicodeEncodeError``, or ``ValueError``
discarded the same way).

Duplicate and merge keys are detected during node-tree composition, *before*
construction.  Duplicate keys are compared by their **constructed semantic**
value (what PyYAML would build and use as a Python mapping key), not their raw
spelling, so spellings that collapse to one key -- ``true`` and ``True``, or
``1``, ``1.0`` and ``true`` -- are rejected before construction silently
overwrites one with another.  The composition walk is bounded: a path-based
visited set rejects cyclic aliases, a depth limit and a node budget bound
deep/huge documents, and any escaping ``RecursionError`` (from the walker or
from PyYAML's own compose/construct) is converted to the sanitized
``source.profile.too_complex`` error -- never a raw traceback.

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
    "source.profile.too_complex": (
        "the profile document is too complex or recursive to load safely"
    ),
    "source.profile.file_missing": "the profile file does not exist",
    "source.profile.file_unreadable": "the profile file could not be read",
    "source.profile.invalid_utf8": "the profile file is not valid UTF-8",
    "source.profile.invalid_path": "the profile file path is not valid",
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
    "source.profile.invalid_entry_key": "a profile entry key must be a string",
    # The fixed code an arbitrary/unknown caller-supplied code is normalized to
    # by the public constructor (so an unknown code can never leak).
    "source.profile.error": "a profile document could not be loaded",
}

# The fixed code an arbitrary/unknown caller-supplied code is normalized to.
_FALLBACK_CODE = "source.profile.error"

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
# Fixed token an arbitrary caller-supplied path is normalized to when it does
# not match the structural-path grammar below.
_REDACTED_PATH = "<path>"

# Grammar every retained structural ``path`` must match: the whole path is one
# of the fixed tokens (``<document>``/``<file>``), or a dot-joined sequence of
# name segments (a lowercase identifier or the ``<key>`` redaction token), each
# optionally suffixed with ``[<index>]`` sequence indices, with an optional
# leading run of indices (for a path rooted in a sequence).  Every retained
# path is therefore built only from a fixed safe alphabet (lowercase letters,
# digits, ``_``, ``.``, ``[``, ``]``, and the literal ``<key>``/``<document>``/
# ``<file>`` tokens), so an arbitrary/secret-bearing path supplied by a public
# caller can never be stored or rendered.
_PATH_NAME = r"[a-z][a-z0-9_]*|<key>"
_PATH_INDEX = r"\[\d+\]"
# A structural path is: an initial name or index, then any run of (".", name)
# pairs and bare indices -- a NAME is always dot-prefixed except the very first
# atom, while an INDEX is appended directly (e.g. ``a[0]`` or ``[0][1].a``).
# Every retained path is therefore built only from a fixed safe alphabet
# (lowercase letters, digits, ``_``, ``.``, ``[``, ``]``, and the literal
# ``<key>``/``<document>``/``<file>`` tokens), so an arbitrary/secret-bearing
# path supplied by a public caller can never be stored or rendered.
_PATH_RE = re.compile(
    rf"\A(?:<document>|<file>|(?:{_PATH_NAME}|{_PATH_INDEX})"
    rf"(?:\.(?:{_PATH_NAME})|{_PATH_INDEX})*)\Z"
)

# Bounds for the duplicate/merge node-tree walker.  A path-based visited set
# rejects cyclic aliases the instant they revisit a node on the current
# descent; a depth limit and a total-node budget bound deeply nested or huge
# documents.  Any escaping ``RecursionError`` (from the walker or from PyYAML's
# own compose/construct of a pathological document) is converted to a
# sanitized ``source.profile.too_complex`` error, never a raw traceback.
_MAX_DEPTH = 200
_MAX_NODES = 100_000


class ProfileFileValidationError(Exception):
    """Raised when a profile document cannot be loaded or resolved safely.

    Only safe, derived attributes are stored, once, at construction, and are
    immutable thereafter:

    * ``code`` — a stable symbolic code (a constant, never a credential).
    * ``path`` — a structural document path built only from *validated*
      identifier tokens and fixed redaction tokens (never a raw value).
    * ``message`` — a constant generic message looked up from ``code``.

    Because the class is public, the caller-supplied ``code`` and ``path`` are
    normalized at construction: an unknown/non-string ``code`` is replaced by
    the fixed ``source.profile.error`` code, and a ``path`` that does not match
    the structural-path grammar is replaced by the fixed ``<path>`` token.  An
    arbitrary caller string can therefore never reach ``code``/``message``/
    ``path``/``repr``/``str``/``args``; internal known codes and grammar-valid
    structural paths are preserved verbatim.

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

    def __init__(self, code: object, path: object) -> None:
        # All attributes are set exactly once via the base ``__setattr__`` so
        # the immutability guard below cannot block construction.  Because this
        # class is public, the caller-supplied ``code`` and ``path`` are never
        # trusted verbatim: an unknown/non-string ``code`` is normalized to the
        # fixed ``_FALLBACK_CODE``, and a ``path`` that does not match the
        # structural-path grammar is normalized to the fixed ``_REDACTED_PATH``
        # token (see ``_normalize_path``).  Only the normalized, constant
        # code/path/message are ever stored, so no arbitrary caller string can
        # reach ``repr``/``str``/``args``/``path``/``message``.  Internal
        # callers always pass a known code and a grammar-valid structural path,
        # so their behavior is preserved.
        known_code = (
            code if type(code) is str and code in _CODE_MESSAGES else _FALLBACK_CODE
        )
        object.__setattr__(self, "code", known_code)
        object.__setattr__(self, "path", _normalize_path(path))
        object.__setattr__(self, "message", _CODE_MESSAGES[known_code])
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
    copied verbatim).  Any missing/unreadable/non-UTF-8 file, a path that
    cannot be encoded to the filesystem encoding or contains an embedded NUL,
    a malformed document, a non-string entry key, an unresolvable value, or a
    disallowed literal raises an immutable, sanitized
    :class:`ProfileFileValidationError` outside any ``except`` scope; no
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

    File-system failures (a missing or unreadable file, a file that is not
    valid UTF-8, or a path that cannot be encoded to the filesystem encoding)
    and syntactically invalid documents are reported as constant-code
    validation errors.  In each case the raw exception is captured inside the
    ``except`` and the sanitized error is raised *after* the
    ``except``/``finally`` completes — outside any active ``except`` scope —
    using a fixed safe path token (never the caller-supplied filesystem path,
    which may itself carry a secret), so no raw exception reaches
    ``__cause__``, ``__context__``, the traceback, ``args``, ``path``, or
    ``message``.
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
        io_error = ProfileFileValidationError("source.profile.file_missing", _FILE_PATH)
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
        io_error = ProfileFileValidationError("source.profile.invalid_utf8", _FILE_PATH)
    except UnicodeEncodeError:
        # ``UnicodeEncodeError`` is a ``ValueError`` (not an ``OSError``): it is
        # raised by ``os.fsencode`` — reached via ``open()`` inside
        # ``read_text`` — when the caller-supplied filesystem path contains a
        # lone surrogate (e.g. ``\ud800``) that cannot be encoded to the
        # filesystem encoding.  The raw exception's ``args``/``object`` carry
        # the offending path string — which may itself carry a secret — so it
        # is captured and discarded, and the sanitized error is raised below
        # outside any active ``except`` scope using the fixed safe path token
        # (never the caller-supplied path).
        io_error = ProfileFileValidationError("source.profile.invalid_path", _FILE_PATH)
    except ValueError:
        # A plain ``ValueError`` — e.g. an embedded NUL (``\x00``) in the
        # caller-supplied filesystem path raised by ``open``/``os.stat`` — is
        # not an ``OSError`` (nor the ``UnicodeDecodeError``/``UnicodeEncodeError``
        # subclasses of ``ValueError`` matched above), so without this handler
        # it would escape sanitization and leak the path via its message/
        # traceback.  It is captured and reported with the fixed safe path
        # token (never the caller-supplied path, which may itself carry a
        # secret).
        io_error = ProfileFileValidationError("source.profile.invalid_path", _FILE_PATH)
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
            _reject_duplicate_and_merge_keys(node, loader)
            data = loader.construct_document(node)
    except yaml.YAMLError:
        # Capture only the constant code; discard the raw PyYAML exception and
        # use the fixed safe path token (never the filesystem path).  The
        # sanitized error is raised below, outside any active ``except`` scope.
        parse_error = ProfileFileValidationError("source.profile.malformed", _ROOT_PATH)
    except RecursionError:
        # A pathologically deep or recursive document can exhaust the stack
        # inside PyYAML's own compose/construct (a cyclic alias the walker has
        # not yet reached, or nesting beyond Python's recursion limit).  The
        # raw ``RecursionError`` is captured and discarded; the sanitized error
        # is raised below, outside any active ``except`` scope.
        parse_error = ProfileFileValidationError(
            "source.profile.too_complex", _ROOT_PATH
        )
    finally:
        loader.dispose()
    if parse_error is not None:
        # Raised outside the ``except`` so __cause__/__context__ are None and
        # the traceback carries no PyYAML frames.
        raise parse_error
    return data


class _NodeBudget:
    """Counts nodes visited by the duplicate/merge walker, capping the total.

    Exceeding ``_MAX_NODES`` (a pathologically large document) raises a
    sanitized :class:`ProfileFileValidationError` rather than letting the walk
    consume unbounded memory or time.
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def tick(self) -> None:
        self.count += 1
        if self.count > _MAX_NODES:
            raise ProfileFileValidationError("source.profile.too_complex", _ROOT_PATH)


def _reject_duplicate_and_merge_keys(node: yaml.Node, loader: yaml.SafeLoader) -> None:
    """Walk a YAML node tree raising on the first merge or duplicate key.

    The walk is bounded so a cyclic alias or a pathologically deep/huge
    document can never exhaust the stack silently:

    * a *path-based* visited set (``on_path``) rejects a cyclic alias the moment
      it revisits a node already on the current descent;
    * a *global* visited set skips a subtree that was already validated, so a
      shared alias cannot re-walk (or exponentially expand) the same subtree;
    * a depth limit (``_MAX_DEPTH``) bounds deeply nested documents;
    * a total-node budget (``_MAX_NODES``) bounds huge documents.

    Any escaping :class:`RecursionError` is captured and converted to a
    sanitized :class:`ProfileFileValidationError`, raised *after* the
    ``except`` so no raw traceback, ``__cause__``, or ``__context__`` is
    retained.
    """
    budget = _NodeBudget()
    on_path: set[int] = set()
    visited: set[int] = set()
    too_complex: ProfileFileValidationError | None = None
    try:
        _walk_for_duplicate_and_merge_keys(
            node, loader, 0, budget, "", on_path, visited
        )
    except RecursionError:
        # Capture-only: raise after the ``except`` so __cause__/__context__ are
        # None and the traceback carries no raw recursion frames.
        too_complex = ProfileFileValidationError(
            "source.profile.too_complex", _ROOT_PATH
        )
    if too_complex is not None:
        raise too_complex


def _walk_for_duplicate_and_merge_keys(
    node: yaml.Node,
    loader: yaml.SafeLoader,
    depth: int,
    budget: _NodeBudget,
    path: str,
    on_path: set[int],
    visited: set[int],
) -> None:
    budget.tick()
    if depth > _MAX_DEPTH:
        raise ProfileFileValidationError("source.profile.too_complex", _ROOT_PATH)
    if isinstance(node, yaml.MappingNode):
        nid = id(node)
        if nid in on_path:
            # A cyclic alias points back into the current descent.
            raise ProfileFileValidationError("source.profile.too_complex", _ROOT_PATH)
        if nid in visited:
            # This exact subtree was already validated; do not re-walk it.
            return
        visited.add(nid)
        on_path.add(nid)
        try:
            seen: set[object] = set()
            for key_node, value_node in node.value:
                if isinstance(key_node, yaml.ScalarNode):
                    key = key_node.value
                    # A merge key is an unsupported construct anywhere in the
                    # document; reject it before construction can flatten it.
                    if isinstance(key, str) and key == "<<":
                        raise ProfileFileValidationError(
                            "source.profile.merge_key", path or _ROOT_PATH
                        )
                    # Compare the *constructed semantic* key value (what PyYAML
                    # would build and use as a Python mapping key), not the raw
                    # spelling, so YAML spellings that collapse to one key --
                    # ``true`` and ``True`` (both ``bool True``), or ``1``,
                    # ``1.0`` and ``true`` (all equal as mapping keys) -- are
                    # rejected before construction silently overwrites one with
                    # another.  A hostile key is still redacted in the path.
                    semantic_key = loader.construct_object(key_node)
                    token = (
                        _safe_key_token(key) if isinstance(key, str) else _REDACTED_KEY
                    )
                    child_path = f"{path}.{token}" if path else token
                    if semantic_key in seen:
                        raise ProfileFileValidationError(
                            "source.profile.duplicate_key", child_path
                        )
                    seen.add(semantic_key)
                    _walk_for_duplicate_and_merge_keys(
                        value_node,
                        loader,
                        depth + 1,
                        budget,
                        child_path,
                        on_path,
                        visited,
                    )
                    continue
                # A non-scalar mapping key (a nested mapping/sequence) is not
                # tracked for duplication; only descend into its value.
                _walk_for_duplicate_and_merge_keys(
                    value_node, loader, depth + 1, budget, path, on_path, visited
                )
        finally:
            on_path.discard(nid)
    elif isinstance(node, yaml.SequenceNode):
        nid = id(node)
        if nid in on_path:
            raise ProfileFileValidationError("source.profile.too_complex", _ROOT_PATH)
        if nid in visited:
            return
        visited.add(nid)
        on_path.add(nid)
        try:
            for index, item in enumerate(node.value):
                _walk_for_duplicate_and_merge_keys(
                    item,
                    loader,
                    depth + 1,
                    budget,
                    f"{path}[{index}]",
                    on_path,
                    visited,
                )
        finally:
            on_path.discard(nid)
    # ScalarNode (and any resolved alias leaf): nothing to validate or descend.


def _validate_document_shape(
    document: object,
) -> dict[str, Mapping[str, object]]:
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
        raise ProfileFileValidationError("source.profile.profiles_empty", "profiles")
    profiles: dict[str, Mapping[str, object]] = {}
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
        for entry_key in entries:
            # A non-string entry key (a YAML int/float/bool/null) is rejected
            # *before* resolution rather than str-coerced.  Coercing it with
            # ``str(key)`` would let e.g. the int key ``1`` and the string key
            # ``"1"`` both collapse to ``"1"`` in the resolved profile (a
            # silent runtime collision).  The non-conformant key is never
            # rendered: the structural path uses the fixed ``<key>`` token.
            if type(entry_key) is not str:
                raise ProfileFileValidationError(
                    "source.profile.invalid_entry_key",
                    f"profiles.{name}.{_REDACTED_KEY}",
                )
        profiles[name] = entries
    return profiles


def _resolve_profiles(
    profiles: Mapping[str, Mapping[str, object]],
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
    key: str,
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
    # The entry key is validated as an exact builtin ``str`` during structural
    # validation, so it is stored verbatim (never str-coerced); only the *error
    # path* is sanitized via ``_safe_key_token``.
    key_name = key
    entry_path = f"profiles.{name}.{_safe_key_token(key)}"
    if not isinstance(source, Mapping):
        raise ProfileFileValidationError("source.profile.entry_not_mapping", entry_path)
    # An entry may declare only ``env`` and/or ``literal``; any other field is
    # rejected (merge keys are already rejected during composition).
    if not set(source) <= _SOURCE_FIELDS:
        raise ProfileFileValidationError(
            "source.profile.unknown_source_field", entry_path
        )
    has_env = "env" in source
    has_literal = "literal" in source
    if has_env and has_literal:
        raise ProfileFileValidationError("source.profile.ambiguous_source", entry_path)
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


def _normalize_path(path: object) -> str:
    """Return *path* if it matches the structural-path grammar, else redact it.

    The public :class:`ProfileFileValidationError` constructor passes the
    caller-supplied path through this so an arbitrary/secret-bearing string
    (or non-string) can never be stored or rendered: only an *exact* builtin
    ``str`` that fully matches ``_PATH_RE`` is retained; anything else is
    replaced by the fixed ``_REDACTED_PATH`` token.
    """
    return path if type(path) is str and _PATH_RE.match(path) else _REDACTED_PATH


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
