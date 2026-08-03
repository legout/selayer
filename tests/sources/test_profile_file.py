"""Tests for the runtime profile-file parser and its secrecy contract.

``load_profile_file`` reads a version-1 profile document and resolves each value
from ``env`` (read from an injectable environment) or ``literal`` (an exact
builtin ``bool``) into a :class:`MappingProfileResolver`.  Every malformed
document, disallowed literal, or unresolvable value surfaces as an immutable
:class:`ProfileFileValidationError` carrying only a stable ``code``, a
structural ``path`` (built only from validated identifier tokens and fixed
redaction tokens), and a constant message — never a resolved value, an
environment value, or a raw YAML key/value.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

import pytest

from selayer.sources.errors import SourceProfileError
from selayer.sources.profile_file import (
    ProfileFileValidationError,
    load_profile_file,
)
from selayer.sources.profiles import MappingProfileResolver, RuntimeProfile

# A sentinel that must never surface in any diagnostic surface.
_SECRET = "SENTINEL_SECRET_VALUE"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _assert_secret_absent(
    error: BaseException,
    secret: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Assert a sentinel secret never reaches any error or captured surface."""

    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    captured = capsys.readouterr()
    for surface in (
        repr(error),
        str(error),
        repr(error.args),
        formatted,
        captured.out,
        captured.err,
    ):
        assert secret not in surface
    assert error.__cause__ is None
    assert error.__context__ is None


# ---------------------------------------------------------------------------
# Valid profiles
# ---------------------------------------------------------------------------


def test_profile_file_resolves_environment_and_boolean(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    dsn:\n      env: WAREHOUSE_DSN\n"
        "    allow_extension_install:\n      literal: false\n",
    )
    resolver = load_profile_file(path, environ={"WAREHOUSE_DSN": "secret-dsn"})
    profile = resolver.resolve("warehouse", source_id="orders")
    assert profile.value("dsn") == "secret-dsn"
    assert profile.value("allow_extension_install") is False
    assert "secret-dsn" not in repr(profile)


def test_profile_file_accepts_boolean_literals(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    enabled:\n      literal: true\n"
        "    disabled:\n      literal: false\n",
    )
    resolver = load_profile_file(path, environ={})
    profile = resolver.resolve("warehouse", source_id="orders")
    assert profile.value("enabled") is True
    assert profile.value("disabled") is False


def test_profile_file_supports_multiple_profiles(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\n"
        "profiles:\n"
        "  warehouse:\n    dsn:\n      env: WAREHOUSE_DSN\n"
        "  cache:\n    url:\n      literal: false\n",
    )
    resolver = load_profile_file(path, environ={"WAREHOUSE_DSN": "secret-dsn"})
    assert isinstance(resolver, MappingProfileResolver)
    assert resolver.resolve("cache", source_id="orders").value("url") is False
    assert (
        resolver.resolve("warehouse", source_id="orders").value("dsn") == "secret-dsn"
    )


def test_profile_file_resolver_returns_runtime_profile(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    flag:\n      literal: true\n",
    )
    resolver = load_profile_file(path, environ={})
    profile = resolver.resolve("warehouse", source_id="orders")
    assert isinstance(profile, RuntimeProfile)
    assert profile.value("flag") is True


# ---------------------------------------------------------------------------
# Literal restriction: only an exact builtin bool is accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: hello\n",
        "version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: 42\n",
        "version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: 3.14\n",
        "version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: ~\n",
        "version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: [a, b]\n",
        "version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: {nested: 1}\n",
        'version: 1\nprofiles:\n  warehouse:\n    a:\n      literal: "true"\n',
    ],
    ids=["string", "int", "float", "null", "list", "mapping", "quoted_string"],
)
def test_profile_file_rejects_non_boolean_literals(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_literal"
    assert error.path == "profiles.warehouse.a"
    assert error.__cause__ is None
    assert error.__context__ is None


# ---------------------------------------------------------------------------
# Version must be the integer 1 exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version_literal",
    ["2", "1.0", "true", '"1"', "~"],
    ids=["wrong_int", "float", "bool_true", "quoted_string", "null"],
)
def test_profile_file_version_must_be_integer_one(
    tmp_path: Path, version_literal: str
) -> None:
    path = _write(
        tmp_path, f"version: {version_literal}\nprofiles:\n  warehouse: {{}}\n"
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.wrong_version"
    assert error.path == "version"
    assert error.__cause__ is None
    assert error.__context__ is None


# ---------------------------------------------------------------------------
# Structural validation (each case raises ProfileFileValidationError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, code, path",
    [
        (
            (
                "version: 1\nprofiles:\n  warehouse:\n"
                "    dsn:\n      env: A\n    dsn:\n      env: B\n"
            ),
            "source.profile.duplicate_key",
            "profiles.warehouse.dsn",
        ),
        ("version: 2\nprofiles: {}\n", "source.profile.wrong_version", "version"),
        (
            "version: 1\nextra: 1\nprofiles: {}\n",
            "source.profile.unknown_key",
            "top_level",
        ),
        (
            "version: 1\nprofiles:\n  Bad-Name:\n    a:\n      literal: true\n",
            "source.profile.invalid_profile_name",
            "profiles",
        ),
        (
            "version: 1\nprofiles: not-a-mapping\n",
            "source.profile.profiles_not_mapping",
            "profiles",
        ),
        ("- just\n- a\n- list\n", "source.profile.not_mapping", "profiles"),
    ],
    ids=[
        "duplicate_key",
        "wrong_version",
        "unknown_key",
        "invalid_profile_name",
        "profiles_not_mapping",
        "root_not_mapping",
    ],
)
def test_profile_file_rejects_malformed_structure(
    tmp_path: Path, body: str, code: str, path: str
) -> None:
    path_file = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path_file, environ={})
    error = caught.value
    assert error.code == code
    assert error.path == path
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_empty_profiles_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nprofiles: {}\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.profiles_empty"
    assert error.path == "profiles"


def test_profile_file_empty_profile_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nprofiles:\n  warehouse: {}\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.profile_empty"
    assert error.path == "profiles.warehouse"


def test_profile_file_invalid_profile_name_never_renders_the_name(
    tmp_path: Path,
) -> None:
    hostile = f"Evil_{_SECRET}"
    path = _write(
        tmp_path,
        f"version: 1\nprofiles:\n  {hostile}:\n    a:\n      literal: true\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_profile_name"
    assert hostile not in repr(error)
    assert hostile not in str(error)
    assert hostile not in error.path


# ---------------------------------------------------------------------------
# Entry / source-field validation
# ---------------------------------------------------------------------------


def test_profile_file_entry_not_a_mapping_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn: just-a-string\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.entry_not_mapping"
    assert error.path == "profiles.warehouse.dsn"


# ---------------------------------------------------------------------------
# Non-string entry keys are rejected before resolution (never str-coerced)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key_literal",
    ["1", "1.0", "true", "yes", "~", "null"],
    ids=["int", "float", "bool_true", "yaml_yes", "null_tilde", "null_word"],
)
def test_profile_file_rejects_non_string_entry_key(
    tmp_path: Path, key_literal: str
) -> None:
    # A YAML entry key that constructs to a non-string (an int, float, bool, or
    # null) must be rejected *before* resolution, never str-coerced.  The old
    # code coerced it with ``str(key)``, which let a non-string key reach the
    # resolved profile under its stringified spelling.
    body = f"version: 1\nprofiles:\n  warehouse:\n    {key_literal}:\n      env: A\n"
    path = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"A": "v"})
    error = caught.value
    assert error.code == "source.profile.invalid_entry_key"
    # The raw (non-string) key spelling is never rendered: it is redacted to
    # the fixed ``<key>`` token.
    assert error.path == "profiles.warehouse.<key>"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_int_and_string_keys_do_not_collide(tmp_path: Path) -> None:
    # YAML ``1`` (int) and ``"1"`` (string) are distinct mapping keys
    # (``1 == "1"`` is False), so the duplicate walker does not flag them.
    # Without rejecting the non-string key, both would be str-coerced to
    # ``"1"`` and the second would silently overwrite the first in the
    # resolved profile (a runtime collision).  The int key must instead be
    # rejected before resolution.
    body = (
        "version: 1\nprofiles:\n  warehouse:\n"
        "    1:\n      env: A\n"
        '    "1":\n      env: B\n'
    )
    path = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"A": "a", "B": "b"})
    error = caught.value
    assert error.code == "source.profile.invalid_entry_key"
    assert error.path == "profiles.warehouse.<key>"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_single_string_numeric_key_still_loads(tmp_path: Path) -> None:
    # A *string* key that merely looks numeric (``"1"``) is valid and must
    # still load after the non-string-key rejection is added.
    path = _write(
        tmp_path,
        'version: 1\nprofiles:\n  warehouse:\n    "1":\n      env: A\n',
    )
    resolver = load_profile_file(path, environ={"A": "v"})
    profile = resolver.resolve("warehouse", source_id="orders")
    assert profile.value("1") == "v"


def test_profile_file_unknown_source_field_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn:\n      env: X\n      extra: y\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"X": "v"})
    error = caught.value
    assert error.code == "source.profile.unknown_source_field"
    assert error.path == "profiles.warehouse.dsn"


def test_profile_file_unknown_only_field_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn:\n      other: x\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.unknown_source_field"
    assert error.path == "profiles.warehouse.dsn"


def test_profile_file_merge_key_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    base: &base\n      env: X\n"
        "    dsn:\n      <<: *base\n      literal: true\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"X": "v"})
    error = caught.value
    assert error.code == "source.profile.merge_key"
    assert error.path == "profiles.warehouse.dsn"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_both_env_and_literal_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    dsn:\n      env: X\n      literal: true\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"X": "v"})
    error = caught.value
    assert error.code == "source.profile.ambiguous_source"
    assert error.path == "profiles.warehouse.dsn"


def test_profile_file_neither_source_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn: {}\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.missing_source"
    assert error.path == "profiles.warehouse.dsn"


@pytest.mark.parametrize(
    "env_value",
    ["bad-name", "", "123"],
    ids=["hyphen", "empty", "non_string"],
)
def test_profile_file_invalid_environment_name_rejected(
    tmp_path: Path, env_value: str
) -> None:
    body = f"version: 1\nprofiles:\n  warehouse:\n    dsn:\n      env: {env_value}\n"
    path = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_environment_name"
    assert error.path == "profiles.warehouse.dsn.env"
    # A malformed env name is never rendered (the empty case is trivially a
    # substring of any text, so only assert non-empty values do not leak).
    if env_value:
        assert env_value not in repr(error)
        assert env_value not in error.path


def test_profile_file_missing_environment_variable_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn:\n      env: MISSING_DSN\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.missing_environment"
    assert error.path == "profiles.warehouse.dsn.env"


def test_profile_file_malformed_yaml_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nprofiles: {unterminated\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.malformed"
    # The raw PyYAML exception must be fully discarded: neither cause nor
    # context may carry it.  (Document-text secrecy is asserted separately by
    # ``test_profile_file_malformed_yaml_does_not_leak_document_text``.)
    assert error.__cause__ is None
    assert error.__context__ is None


# ---------------------------------------------------------------------------
# Semantic duplicate-key detection and recursion bounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("true", "True"),
        ("1", "1.0"),
        ("True", "1"),
        ("yes", "True"),
    ],
    ids=["bool_true_True", "int_float", "bool_int", "yaml_yes_true"],
)
def test_profile_file_rejects_semantic_duplicate_keys(
    tmp_path: Path, first: str, second: str
) -> None:
    # Two entry keys whose raw spellings differ but which collapse to a single
    # Python mapping key after YAML construction -- ``true``/``True`` (both
    # ``bool True``), ``1``/``1.0`` (int/float, equal as keys), ``1``/``true``
    # (int/bool, equal as keys), ``yes``/``True`` (YAML's bool spellings) --
    # must be rejected as duplicates before construction silently overwrites
    # one with the other.  The duplicate is compared by *constructed value*,
    # not raw spelling.
    body = (
        "version: 1\nprofiles:\n  warehouse:\n"
        f"    {first}:\n      env: A\n"
        f"    {second}:\n      env: B\n"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"A": "a", "B": "b"})
    error = caught.value
    assert error.code == "source.profile.duplicate_key"
    # The duplicate's raw spelling is never rendered: a numeric/bool/uppercase
    # key redacts to the fixed ``<key>`` token; the path never carries a value.
    assert error.path.startswith("profiles.warehouse.")
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_cyclic_sequence_alias_rejected(tmp_path: Path) -> None:
    # ``&a [*a]`` composes to a sequence that contains itself.  Without a
    # path-based cycle guard the duplicate/merge walker would recurse forever
    # and a raw ``RecursionError`` (with its traceback) would escape.
    path = _write(tmp_path, "&a [*a]\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.too_complex"
    assert error.path == "<document>"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_cyclic_mapping_alias_rejected(tmp_path: Path) -> None:
    # ``&a`` anchors the root mapping and ``*a`` aliases it from within, so the
    # composed mapping points back to itself.
    path = _write(tmp_path, "&a\nb: *a\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.too_complex"
    assert error.path == "<document>"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_cyclic_alias_does_not_leak_document_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A recursive document is reported with a fixed safe path token; neither
    # the document text nor a raw recursion traceback may reach any surface.
    body = f"&a\n# {_SECRET}\nb: *a\n"
    path = _write(tmp_path, body)
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.too_complex"
    assert error.path == "<document>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_deeply_nested_document_rejected(tmp_path: Path) -> None:
    # A pathologically deep document exceeds the composition walk's depth/node
    # budget (or, past PyYAML's own tolerance, Python's recursion limit during
    # compose/construct).  Either way it must surface as the sanitized
    # ``too_complex`` error, never a raw ``RecursionError`` traceback.
    depth = 300
    body = "[" * depth + "1" + "]" * depth + "\n"
    path = tmp_path / "profiles.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.too_complex"
    assert error.path == "<document>"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_profile_file_non_cyclic_shared_alias_loads(tmp_path: Path) -> None:
    # A non-cyclic alias shared between two entries is valid and must not be
    # mis-flagged as a cycle by the walker's visited-set bounding.
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    one: &src\n      env: SHARED\n"
        "    two: *src\n",
    )
    resolver = load_profile_file(path, environ={"SHARED": "v"})
    profile = resolver.resolve("warehouse", source_id="orders")
    assert profile.value("one") == "v"
    assert profile.value("two") == "v"


# ---------------------------------------------------------------------------
# Error attribute contract
# ---------------------------------------------------------------------------


def test_profile_file_error_message_is_constant_for_code(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn:\n      env: MISSING\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.message == "a required profile environment variable is missing"
    assert error.message == str(error)
    assert error.message in repr(error.args)


def test_profile_file_error_is_immutable(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn:\n      env: MISSING\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    # code/path/message cannot be mutated or deleted after construction.
    for attr in ("code", "path", "message"):
        with pytest.raises(AttributeError):
            setattr(error, attr, "mutated")
        with pytest.raises(AttributeError):
            delattr(error, attr)
    # The repr exposes only the safe code/path/message (all constant/structural).
    text = repr(error)
    assert "code=" in text
    assert "path=" in text
    assert "message=" in text


# ---------------------------------------------------------------------------
# Constructor hardening: arbitrary caller code/path never leaks
# ---------------------------------------------------------------------------


def test_profile_file_validation_error_arbitrary_code_is_normalized() -> None:
    # The public constructor must never retain an arbitrary caller-supplied
    # code: an unknown code is normalized to the fixed fallback code, and the
    # supplied string never reaches code/message/repr/str/args.
    secret = f"code_{_SECRET}"
    error = ProfileFileValidationError(secret, "profiles")
    assert error.code == "source.profile.error"
    assert error.message == "a profile document could not be loaded"
    for surface in (
        repr(error),
        str(error),
        repr(error.args),
        error.code,
        error.message,
    ):
        assert secret not in surface


def test_profile_file_validation_error_arbitrary_path_is_normalized() -> None:
    # The public constructor must never retain an arbitrary caller-supplied
    # path: a path not matching the structural-path grammar is normalized to
    # the fixed ``<path>`` token, and the supplied string never reaches
    # path/repr/str/args.  A known code is preserved.
    secret = f"path_{_SECRET}"
    error = ProfileFileValidationError("source.profile.malformed", secret)
    assert error.code == "source.profile.malformed"
    assert error.path == "<path>"
    for surface in (
        repr(error),
        str(error),
        repr(error.args),
        error.path,
        error.message,
    ):
        assert secret not in surface


def test_profile_file_validation_error_known_code_and_path_preserved() -> None:
    # Internal known codes and grammar-valid structural paths are preserved
    # verbatim: the hardening must not change internal behavior.
    error = ProfileFileValidationError(
        "source.profile.missing_environment", "profiles.warehouse.dsn.env"
    )
    assert error.code == "source.profile.missing_environment"
    assert error.path == "profiles.warehouse.dsn.env"
    assert error.message == "a required profile environment variable is missing"


def test_profile_file_validation_error_non_string_args_normalized() -> None:
    # Non-string code/path arguments must not crash and must not leak.
    error = ProfileFileValidationError(12345, 67890)
    assert error.code == "source.profile.error"
    assert error.path == "<path>"
    assert error.message == "a profile document could not be loaded"


def test_profile_file_validation_error_arbitrary_args_do_not_leak_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A secret-bearing arbitrary code and path supplied to the public
    # constructor must never reach any rendered surface.
    error = ProfileFileValidationError(_SECRET, _SECRET)
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    captured = capsys.readouterr()
    for surface in (
        repr(error),
        str(error),
        repr(error.args),
        error.code,
        error.path,
        error.message,
        formatted,
        captured.out,
        captured.err,
    ):
        assert _SECRET not in surface


# ---------------------------------------------------------------------------
# Secrecy contract: resolved / environment values never leak
# ---------------------------------------------------------------------------


def test_profile_file_resolved_value_does_not_leak_when_a_later_entry_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The first entry resolves the sentinel secret; the second references a
    # missing environment variable.  The already-resolved value must never
    # surface in the resulting error or captured output.
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    dsn:\n      env: SECRET_DSN\n"
        "    broken:\n      env: MISSING_DSN\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"SECRET_DSN": _SECRET})
    assert caught.value.code == "source.profile.missing_environment"
    _assert_secret_absent(caught.value, _SECRET, capsys)


class _LeakyEnvValue(str):
    """A ``str`` subclass whose repr leaks a secret — must never be rendered."""

    def __repr__(self) -> str:  # pragma: no cover - repr must never run
        return f"_LeakyEnvValue({_SECRET!r})"


def test_profile_file_hostile_environ_value_subclass_rejected_and_does_not_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    dsn:\n      env: SECRET_DSN\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"SECRET_DSN": _LeakyEnvValue(_SECRET)})
    error = caught.value
    assert error.code == "source.profile.invalid_environment_value"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_hostile_top_level_key_does_not_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = f"Evil_{_SECRET}"
    path = _write(
        tmp_path,
        f"version: 1\n{hostile}: 1\nprofiles:\n  warehouse: {{}}\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.unknown_key"
    assert error.path == "top_level"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_hostile_entry_key_does_not_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = f"Evil_{_SECRET}"
    # The hostile entry key references a missing environment variable, so the
    # error path is built; the key must be redacted, never rendered.
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        f"    {hostile}:\n      env: MISSING_DSN\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.missing_environment"
    assert error.path == "profiles.warehouse.<key>.env"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_hostile_duplicate_key_does_not_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = f"Evil_{_SECRET}"
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        f"    {hostile}:\n      env: A\n"
        f"    {hostile}:\n      env: B\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={"A": "v", "B": "v"})
    error = caught.value
    assert error.code == "source.profile.duplicate_key"
    assert error.path == "profiles.warehouse.<key>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_malformed_yaml_does_not_leak_document_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sentinel is embedded in malformed document text; the PyYAML
    # diagnostic (which quotes document text) must be discarded entirely.
    path = _write(
        tmp_path,
        f"version: 1\nprofiles: {{unterminated: {_SECRET}\n",
    )
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.malformed"
    _assert_secret_absent(error, _SECRET, capsys)


# ---------------------------------------------------------------------------
# File I/O failures and secret-bearing filesystem paths
# ---------------------------------------------------------------------------


def test_profile_file_malformed_yaml_filename_does_not_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The secret is embedded in the *filename* (not the document text).  The
    # malformed error must use a fixed safe path token, never the caller-
    # supplied filesystem path, so the secret cannot reach ``error.path`` or
    # any other error surface.
    path = tmp_path / f"{_SECRET}.yaml"
    path.write_text("version: 1\nprofiles: {unterminated\n", encoding="utf-8")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.malformed"
    assert error.path == "<document>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_missing_file_raises_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A secret-bearing filename must never surface: the raw
    # ``FileNotFoundError`` (whose message carries the path) is captured and
    # discarded, and the sanitized error uses a fixed safe path token.
    path = tmp_path / f"{_SECRET}.yaml"
    assert not path.exists()
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.file_missing"
    assert error.path == "<file>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_unreadable_directory_raises_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading a directory raises ``IsADirectoryError`` (an ``OSError``): it is
    # captured and reported with a fixed safe path token, never the filesystem
    # path (which here carries the secret).
    path = tmp_path / _SECRET
    path.mkdir()
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.file_unreadable"
    assert error.path == "<file>"
    _assert_secret_absent(error, _SECRET, capsys)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod 000 cannot deny reads when running as root",
)
def test_profile_file_unreadable_permission_denied_raises_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / f"{_SECRET}.yaml"
    path.write_text("version: 1\nprofiles: {}\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(ProfileFileValidationError) as caught:
            load_profile_file(path, environ={})
    finally:
        # Restore writability so the tmp_path teardown can remove the file.
        path.chmod(0o600)
    error = caught.value
    assert error.code == "source.profile.file_unreadable"
    assert error.path == "<file>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_invalid_utf8_does_not_leak_raw_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sentinel is encoded into the file as valid UTF-8, then followed by
    # undecodable bytes.  A raw ``UnicodeDecodeError`` (whose ``args``/``object``
    # carry the raw bytes) is a ``ValueError``, not an ``OSError``, so without
    # an explicit handler it would escape sanitization and leak the bytes.  It
    # must be captured and discarded, and the sanitized error must use a fixed
    # safe path token.
    path = tmp_path / "profiles.yaml"
    path.write_bytes(b"version: 1\n" + _SECRET.encode("utf-8") + b"\xff\xfe\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_utf8"
    assert error.path == "<file>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_invalid_utf8_filename_does_not_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A secret-bearing filename combined with invalid UTF-8 bytes: the raw
    # ``UnicodeDecodeError`` is captured and the sanitized error uses the fixed
    # safe path token, never the caller-supplied filesystem path.
    path = tmp_path / f"{_SECRET}.yaml"
    path.write_bytes(b"version: 1\n\xff\xfe\n")
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_utf8"
    assert error.path == "<file>"
    _assert_secret_absent(error, _SECRET, capsys)


def test_profile_file_unencodable_surrogate_path_raises_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A lone high surrogate (``\ud800``) in the caller-supplied filesystem path
    # cannot be encoded to the filesystem encoding: ``read_text`` -> ``open`` ->
    # ``os.fsencode`` raises ``UnicodeEncodeError`` (a ``ValueError``, *not* an
    # ``OSError``), so without an explicit handler it escapes sanitization and
    # leaks the path (here carrying the sentinel) via ``args``/``object``/the
    # traceback.  It must be captured and discarded; the sanitized error uses a
    # fixed safe path token and neither the secret-bearing path nor the
    # surrogate ever reaches any rendered surface.
    surrogate = "\ud800"
    path = tmp_path / f"{_SECRET}{surrogate}.yaml"
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_path"
    assert error.path == "<file>"
    assert error.__cause__ is None
    assert error.__context__ is None
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    captured = capsys.readouterr()
    for surface in (
        repr(error),
        str(error),
        repr(error.args),
        error.path,
        error.message,
        formatted,
        captured.out,
        captured.err,
    ):
        assert _SECRET not in surface
        assert surrogate not in surface


def test_profile_file_embedded_nul_path_raises_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An embedded NUL (``\x00``) in the caller-supplied filesystem path makes
    # ``open``/``os.stat`` raise ``ValueError("embedded null byte")`` -- a
    # ``ValueError``, *not* an ``OSError`` (nor a Unicode error), so without an
    # explicit handler it escapes sanitization.  It must be captured and
    # reported with the fixed safe path token, never the caller-supplied path
    # (which here carries the sentinel) or the NUL.
    path = tmp_path / f"{_SECRET}\x00.yaml"
    with pytest.raises(ProfileFileValidationError) as caught:
        load_profile_file(path, environ={})
    error = caught.value
    assert error.code == "source.profile.invalid_path"
    assert error.path == "<file>"
    _assert_secret_absent(error, _SECRET, capsys)
    assert "\x00" not in repr(error)
    assert "\x00" not in str(error)


# ---------------------------------------------------------------------------
# Runtime resolver failures remain SourceProfileError (unchanged)
# ---------------------------------------------------------------------------


def test_profile_file_resolver_missing_name_still_raises_source_profile_error(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n    flag:\n      literal: true\n",
    )
    resolver = load_profile_file(path, environ={})
    with pytest.raises(SourceProfileError) as caught:
        resolver.resolve("absent", source_id="orders")
    assert caught.value.code == "missing_profile"
