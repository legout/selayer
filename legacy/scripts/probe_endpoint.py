#!/usr/bin/env python3
"""Throwaway capability probe for the OpenAI-compatible LLM endpoint.

Purpose
-------
selayer_chat is heading toward *agentic* NL→SQL (dynamic schema exploration
via tool calls, multi-turn clarification, self-debugging). PydanticAI is the
planned agent engine, BUT it leans on features that custom OpenAI-compatible
gateways often omit. This probe checks the four capabilities that actually
matter before committing to PydanticAI:

    1. baseline chat completion        (endpoint + model are alive)
    2. function/tool calling           (required for any agent loop)
    3. response_format json_schema     (PydanticAI's preferred structured mode)
    4. response_format json_object     (the structured-output fallback)

Verdict rules
-------------
    * viable for PydanticAI  -> (2 passes) AND ((3 OR 4) passes)
    * viable for structured parsing only -> (3 OR 4) passes, (2) fails
      (use PydanticAI with no tools, or `instructor`, or raw-SDK JSON mode)
    * not viable             -> (2) fails AND (3,4) fail
      (stay on raw openai SDK; prompt-and-parse)

Run
---
    uv run scripts/probe_endpoint.py      # reads .env + LLMConfig defaults

Delete this file once you've made the decision.

Exit code: 0 = PydanticAI-viable, 1 = not viable, 2 = config problem.
"""

from __future__ import annotations

import json
import sys
from json import JSONDecodeError
from typing import Any, cast

from openai import OpenAI

# Resolve env + config exactly the way the app does, so the probe hits the
# real endpoint/model rather than a hardcoded guess.
from selayer_chat import LLMConfig, load_env

load_env()

# qwen-3.6-27b is a REASONING model: it emits <think>…</think> before the
# real answer. With a small max_tokens the visible answer comes back EMPTY
# (the first run saw exactly this — empty content even on plain chat). So:
#   (a) disable thinking via the vLLM chat-template kwarg (extra_body),
#   (b) append the qwen-native "/no_think" cue as a portable fallback, and
#   (c) budget enough tokens for the answer to survive.
# If a probe STILL returns empty, finish_reason tells us why
# ("length" = truncated, "stop" = finished-but-empty, etc.).
THINK_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
NO_THINK = " /no_think"


def _bail(msg: str) -> None:
    print(f"\n❌ {msg}")
    sys.exit(2)


def main() -> None:
    cfg = LLMConfig()
    if not cfg.api_key or cfg.api_key == "sk-replace-me":
        _bail(
            "OPENAI_API_KEY is not set (or still the placeholder). "
            "Put a real key in .env, then rerun."
        )

    print("=" * 64)
    print("LLM endpoint capability probe")
    print("=" * 64)
    print(f"  base_url : {cfg.base_url}")
    print(f"  model    : {cfg.model}")
    print(f"  api_key  : {cfg.api_key[:8]}…{cfg.api_key[-4:]}")
    print("=" * 64)

    client = OpenAI(
        base_url=cfg.base_url or None,
        api_key=cfg.api_key,
        timeout=cfg.timeout_s,
    )

    results: dict[str, tuple[bool, str]] = {}
    results["1. chat completion"] = probe_chat(client, cfg.model)
    results["2. tool calling"] = probe_tools(client, cfg.model)
    results["3. json_schema"] = probe_json_schema(client, cfg.model)
    results["4. json_object"] = probe_json_object(client, cfg.model)

    # ---- report ----------------------------------------------------------
    print("\n" + "-" * 64)
    print("Results")
    print("-" * 64)
    for name, (ok, detail) in results.items():
        mark = "✅ PASS" if ok else "❌ FAIL"
        print(f"{mark}  {name}")
        # indent the detail line(s)
        for line in detail.rstrip().splitlines() or ["(no detail)"]:
            print(f"        {line}")
    print("-" * 64)

    chat_ok = results["1. chat completion"][0]
    tools_ok = results["2. tool calling"][0]
    structured_ok = results["3. json_schema"][0] or results["4. json_object"][0]

    print()
    if not chat_ok:
        print("🛑  Endpoint does not answer basic chat. Fix connectivity/auth first.")
        sys.exit(1)
    if tools_ok and structured_ok:
        print("✅  PydanticAI is VIABLE on this endpoint (tools + structured output).")
        print("    Next: `uv add pydantic-ai` and build the agent behind the port.")
        sys.exit(0)
    if tools_ok and not structured_ok:
        print("⚠️   Tools work but structured output does not.")
        print("    PydanticAI can still drive an agent loop, but parsing SQL will need")
        print(
            "    prompt-and-parse (your existing extraction.py) rather than result_type."
        )
        print("    Consider mirascope, or PydanticAI without result_type.")
        sys.exit(0)
    if not tools_ok and structured_ok:
        print("⚠️   Structured output works, but tool calling does NOT.")
        print("    No agentic loop possible. Options: PydanticAI without tools,")
        print("    instructor, or raw openai SDK with response_format=json_object.")
        sys.exit(1)
    print("🛑  Neither tool calling nor structured output is supported.")
    print("    PydanticAI is the wrong bet here. Stay on raw openai SDK;")
    print(
        "    improve parsing via prompt format (JSON / fenced code), not a framework."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _err_detail(e: Exception) -> str:
    """Compact, useful error string for any openai exception."""
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    body = getattr(e, "body", None)
    msg = getattr(e, "message", None) or str(e)
    first = msg.splitlines()[0][:200] if msg else ""
    if status:
        return f"HTTP {status}: {first}" if first else f"HTTP {status}"
    if body:
        return f"{first or body}"
    return first or repr(e)[:200]


def _create(
    client: OpenAI,
    model: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> Any:
    """chat.completions.create with thinking disabled. Returns the choice."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "extra_body": THINK_OFF,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if tools is not None:
        kwargs["tools"] = cast(Any, tools)
        kwargs["tool_choice"] = tool_choice or "auto"
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0]


def _diag(choice: Any) -> str:
    """One-line hint at why content might be empty."""
    msg = choice.message
    rc = getattr(msg, "reasoning_content", None)
    return (
        f"finish_reason={choice.finish_reason!r}, "
        f"content_len={len(msg.content or '')}, "
        f"reasoning={'yes' if rc else 'no'}"
    )


def probe_chat(client: OpenAI, model: str) -> tuple[bool, str]:
    try:
        choice = _create(
            client,
            model,
            [{"role": "user", "content": "Reply with the single word: OK" + NO_THINK}],
            max_tokens=64,
        )
        text = (choice.message.content or "").strip()
        return True, f"answered: {text!r}  [{_diag(choice)}]"
    except Exception as e:  # noqa: BLE001 - probe must not crash
        return False, _err_detail(e)


def probe_tools(client: OpenAI, model: str) -> tuple[bool, str]:
    """Does the model emit a tool_call when given a function spec?"""
    tool = {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "Return column names and types for a table.",
            "parameters": {
                "type": "object",
                "properties": {"table_name": {"type": "string"}},
                "required": ["table_name"],
            },
        },
    }
    try:
        choice = _create(
            client,
            model,
            [
                {
                    "role": "user",
                    "content": (
                        "I need the columns of the 'orders' table. "
                        "Use the describe_table tool to get them." + NO_THINK
                    ),
                }
            ],
            max_tokens=128,
            tools=[tool],
        )
        msg = choice.message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return (
                False,
                "model returned plain text, no tool_calls "
                f"(content={(msg.content or '')[:80]!r})  [{_diag(choice)}]",
            )
        first = calls[0].function
        return True, f"called tool {first.name!r} with args {first.arguments!r}"
    except Exception as e:  # noqa: BLE001
        return False, _err_detail(e)


def probe_json_schema(client: OpenAI, model: str) -> tuple[bool, str]:
    """Does response_format=json_schema (strict) return guaranteed-valid JSON?"""
    schema = {
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
        "additionalProperties": False,
    }
    content = ""
    diag = ""
    try:
        choice = _create(
            client,
            model,
            [
                {
                    "role": "user",
                    "content": (
                        "Return a JSON object with a 'sql' field whose value is "
                        "a SQL query selecting 42 as a single column." + NO_THINK
                    ),
                }
            ],
            max_tokens=512,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "sql_result",
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        content = choice.message.content or ""
        diag = _diag(choice)
        parsed = json.loads(content)
        if "sql" not in parsed:
            return False, f"valid JSON but missing 'sql' key: {content[:80]!r}"
        return True, f"parsed OK: sql={parsed['sql']!r}"
    except JSONDecodeError:
        return (
            False,
            f"accepted json_schema but not JSON: {content[:80]!r}  [{diag}]",
        )
    except Exception as e:  # noqa: BLE001
        return False, _err_detail(e)


def probe_json_object(client: OpenAI, model: str) -> tuple[bool, str]:
    """Does response_format=json_object return valid JSON? (fallback mode.)"""
    content = ""
    diag = ""
    try:
        choice = _create(
            client,
            model,
            [
                {
                    "role": "user",
                    "content": (
                        "Return JSON with a 'sql' field whose value is "
                        "a SQL query selecting 42. Respond with JSON only." + NO_THINK
                    ),
                }
            ],
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        content = choice.message.content or ""
        diag = _diag(choice)
        parsed = json.loads(content)
        if "sql" not in parsed:
            return False, f"valid JSON but missing 'sql' key: {content[:80]!r}"
        return True, f"parsed OK: sql={parsed['sql']!r}"
    except JSONDecodeError:
        return (
            False,
            f"accepted json_object but not JSON: {content[:80]!r}  [{diag}]",
        )
    except Exception as e:  # noqa: BLE001
        return False, _err_detail(e)


if __name__ == "__main__":
    main()
