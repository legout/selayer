"""Tests for opaque runtime profiles and provider resolution contracts."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pyarrow as pa
import pytest

from selayer.sources.errors import SourceProfileError
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)

# ---------------------------------------------------------------------------
# RuntimeProfile opacity and defensive copy
# ---------------------------------------------------------------------------


def test_runtime_profile_never_reprs_secret_values() -> None:
    values = {"access_key": "AKIA_SECRET", "secret_key": "SUPER_SECRET"}
    profile = RuntimeProfile("analytics_s3", values)
    values["secret_key"] = "changed"

    assert "AKIA_SECRET" not in repr(profile)
    assert "SUPER_SECRET" not in repr(profile)
    assert profile.value("secret_key") == "SUPER_SECRET"


def test_runtime_profile_defensive_copy_isolates_original() -> None:
    original: dict[str, object] = {"region": "us-east-1", "token": "abc"}
    profile = RuntimeProfile("p", original)
    original["token"] = "MUTATED"
    original["extra"] = "new"

    assert profile.value("token") == "abc"
    with pytest.raises(KeyError):
        profile.value("extra")


def test_runtime_profile_repr_exposes_only_name() -> None:
    profile = RuntimeProfile("analytics_s3", {"secret_key": "shh"})
    rendered = repr(profile)
    assert "analytics_s3" in rendered
    # Neither the secret value nor its key surfaces in the repr.
    assert "shh" not in rendered
    assert "secret_key" not in rendered


def test_runtime_profile_is_immutable() -> None:
    profile = RuntimeProfile("p", {"a": 1})
    # ``__setattr__`` (rather than direct assignment) exercises the frozen
    # dataclass guard without tripping Pyright's read-only attribute check
    # or Ruff's B009 constant-setattr simplification.
    with pytest.raises(AttributeError):
        profile.__setattr__("name", "other")


def test_runtime_profile_value_missing_raises_keyerror() -> None:
    profile = RuntimeProfile("p", {"a": 1})
    with pytest.raises(KeyError):
        profile.value("missing")


# ---------------------------------------------------------------------------
# MappingProfileResolver
# ---------------------------------------------------------------------------


def test_missing_profile_has_safe_domain_error() -> None:
    resolver = MappingProfileResolver({})

    with pytest.raises(SourceProfileError) as caught:
        resolver.resolve("missing", source_id="orders")

    assert caught.value.code == "missing_profile"
    assert caught.value.source_id == "orders"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_resolver_returns_profile_with_original_values() -> None:
    resolver = MappingProfileResolver(
        {"analytics_s3": {"bucket": "data", "region": "us-east-1"}}
    )
    profile = resolver.resolve("analytics_s3", source_id="orders")
    assert isinstance(profile, RuntimeProfile)
    assert profile.value("bucket") == "data"
    assert profile.value("region") == "us-east-1"


def test_resolver_defensively_copies_profile_map() -> None:
    source_map: dict[str, dict[str, object]] = {"p": {"token": "orig"}}
    resolver = MappingProfileResolver(source_map)
    # Mutating the caller's map after construction must not affect resolution.
    source_map["p"]["token"] = "MUTATED"
    source_map["p2"] = {"x": 1}

    profile = resolver.resolve("p", source_id="orders")
    assert profile.value("token") == "orig"
    with pytest.raises(SourceProfileError):
        resolver.resolve("p2", source_id="orders")


def test_resolver_missing_error_has_uuidv4_operation_id() -> None:
    resolver = MappingProfileResolver({})
    with pytest.raises(SourceProfileError) as caught:
        resolver.resolve("missing", source_id="orders")
    parsed = uuid.UUID(caught.value.operation_id)
    assert parsed.version == 4
    assert str(parsed) == caught.value.operation_id


def test_resolver_error_repr_is_sanitized() -> None:
    resolver = MappingProfileResolver({})
    with pytest.raises(SourceProfileError) as caught:
        resolver.resolve("missing", source_id="orders")
    text = repr(caught.value)
    assert "orders" in text
    assert "missing_profile" in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# RuntimeProfileResolver protocol acceptance
# ---------------------------------------------------------------------------


class _FakeProfileResolver:
    def resolve(self, name: str, *, source_id: str) -> RuntimeProfile:
        return RuntimeProfile(name, {"resolved": True})


def test_runtime_profile_resolver_protocol_accepts_fake() -> None:
    resolver: RuntimeProfileResolver = _FakeProfileResolver()
    assert isinstance(resolver, RuntimeProfileResolver)
    profile = resolver.resolve("any", source_id="orders")
    assert profile.value("resolved") is True


# ---------------------------------------------------------------------------
# ArrowProviderResolver protocol acceptance
# ---------------------------------------------------------------------------


class _FakeArrowResolver:
    def resolve(self, handle: str, *, source_id: str) -> Callable[[], ArrowObject]:
        table = pa.table({"id": [1, 2]})

        def provider() -> pa.Table:
            return table

        return provider


def test_arrow_provider_resolver_protocol_accepts_fake() -> None:
    resolver: ArrowProviderResolver = _FakeArrowResolver()
    assert isinstance(resolver, ArrowProviderResolver)
    provider = resolver.resolve("h", source_id="orders")
    obj = provider()
    assert isinstance(obj, pa.Table)
