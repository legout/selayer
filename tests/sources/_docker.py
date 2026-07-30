"""Shared Docker gate for PostgreSQL and MinIO integration tests."""

from __future__ import annotations

import os

import pytest


def require_docker() -> None:
    """Fail in CI when Docker is unavailable; skip that setup locally."""

    try:
        import docker

        available = bool(docker.from_env().ping())
    except Exception:  # noqa: BLE001
        available = False
    if available:
        return
    if os.environ.get("CI") == "true":
        raise RuntimeError("Docker is unavailable in CI")
    pytest.skip("Docker daemon is not available")
