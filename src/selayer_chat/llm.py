"""LLM port + OpenAI-compatible adapter.

The backend does not know which provider is in use; it only knows about
the `LLMClient` protocol. Production wires an `OpenAIClient`, tests wire a
`MockLLMClient` that returns canned SQL.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Protocol

from .types import LLMConfig, StreamEvent


class LLMClient(Protocol):
    """Anything that can answer a single NL→SQL question."""

    def complete(self, system: str, user: str) -> tuple[str, int | None]:
        """Return (text, tokens_used_or_None)."""

    def stream(self, system: str, user: str) -> Iterator[StreamEvent]:
        """Yield token events, then a single 'sql' event with the final SQL,
        then a 'done' event. The yielded `df` is left None for LLMs; SQL
        execution happens in the backend after this returns."""


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------


class OpenAIClient:
    """Adapter for any endpoint that follows OpenAI's chat-completions API."""

    def __init__(self, config: LLMConfig) -> None:
        try:
            from openai import OpenAI  # lazy import keeps the package optional
        except ImportError as e:
            raise ImportError(
                "openai package required for OpenAIClient. "
                "Install with `pip install openai`."
            ) from e

        self._cfg = config
        self._client = OpenAI(
            base_url=config.base_url or None,
            api_key=config.api_key or "no-key",
            timeout=config.timeout_s,
        )

    def complete(self, system: str, user: str) -> tuple[str, int | None]:
        resp = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._cfg.temperature,
            max_tokens=self._cfg.max_tokens,
        )
        text = resp.choices[0].message.content or ""
        used = getattr(resp.usage, "total_tokens", None)
        return text, used

    def stream(self, system: str, user: str) -> Iterator[StreamEvent]:
        """Stream tokens from OpenAI. The final SQL is the concatenated text."""
        stream = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._cfg.temperature,
            max_tokens=self._cfg.max_tokens,
            stream=True,
        )

        chunks: list[str] = []
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                chunks.append(delta)
                yield StreamEvent(kind="token", content=delta)

        yield StreamEvent(kind="sql", content="".join(chunks))
        yield StreamEvent(kind="done")


# ---------------------------------------------------------------------------
# A tiny mock for tests
# ---------------------------------------------------------------------------


class MockLLMClient:
    """Returns a configured response regardless of input.

    >>> MockLLMClient(response="SELECT 1").complete("", "")
    ('SELECT 1', None)
    """

    def __init__(
        self, response: str = "SELECT 1 AS one", tokens: int | None = None
    ) -> None:
        self._response = response
        self._tokens = tokens
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> tuple[str, int | None]:
        self.calls.append((system, user))
        return self._response, self._tokens

    def stream(self, system: str, user: str) -> Iterator[StreamEvent]:
        self.calls.append((system, user))
        chunks = [self._response[i : i + 8] for i in range(0, len(self._response), 8)]
        for c in chunks:
            time.sleep(0)  # yield to event loop
            yield StreamEvent(kind="token", content=c)
        yield StreamEvent(kind="sql", content=self._response)
        yield StreamEvent(kind="done")
