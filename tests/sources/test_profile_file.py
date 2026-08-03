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
def test_profile_file_rejects_non_boolean_literals(
    tmp_path: Path, body: str
) -> None:
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


def test_profile_file_unknown_source_field_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nprofiles:\n  warehouse:\n"
        "    dsn:\n      env: X\n      extra: y\n",
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
    body = (
        "version: 1\nprofiles:\n  warehouse:\n"
        f"    dsn:\n      env: {env_value}\n"
    )
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
