"""Shared pytest fixtures for the ``selayer-discovery`` test suite.

Fixtures here are deliberately small and reusable across the discovery test
modules. They construct sessions under :func:`pytest`'s ``tmp_path`` so no
fixture session ever touches the real repository tree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from selayer_discovery.session import SessionCharter

#: A canonical SHA-256-shaped content hash used by fixtures/tests.
SAMPLE_HASH: str = "0" * 64


def _hash(index: int) -> str:
    """Return a distinct 64-hex string derived from ``index``."""

    return f"{index:064x}"


@pytest.fixture
def actor() -> str:
    """A deliberately whitespace-heavy approver/actor name (normalization target)."""

    return "Dr.  Alice   Okonkwo"


@pytest.fixture
def make_charter() -> Callable[..., SessionCharter]:
    """Factory for :class:`SessionCharter` values with overridable fields."""

    def _factory(
        *,
        session_id: str = "session-shopfloor-001",
        business_question: str = (
            "Is the order_facts grain one row per confirmed order?"
        ),
        catalog_fingerprint: str = SAMPLE_HASH,
        approver: str = "Dr.  Alice   Okonkwo",
        inclusions: tuple[str, ...] = ("source.shopfloor.orders",),
        exclusions: tuple[str, ...] = ("domain.finance",),
        acceptance_questions: tuple[str, ...] = (
            "Does the corrected grain pass the uniqueness audit?",
        ),
    ) -> SessionCharter:
        return SessionCharter(
            session_id=session_id,
            business_question=business_question,
            catalog_fingerprint=catalog_fingerprint,
            approver=approver,
            inclusions=inclusions,
            exclusions=exclusions,
            acceptance_questions=acceptance_questions,
        )

    return _factory


@pytest.fixture
def charter(make_charter: Callable[..., SessionCharter]) -> SessionCharter:
    """A default charter for most session tests."""

    return make_charter()


@pytest.fixture
def session_root(tmp_path: Path) -> Path:
    """An isolated session directory under the pytest temporary tree."""

    return tmp_path / "discovery-session"


@pytest.fixture
def hash_factory() -> Callable[[int], str]:
    """Factory returning distinct, well-formed 64-hex content hashes."""

    return _hash
