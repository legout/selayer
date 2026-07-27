# Legacy chat and UI prototype

This directory preserves the former `selayer_chat` backend, four UI adapters, endpoint probes, and example questions as historical reference.

These files are **unsupported legacy artifacts**:

- they are not included in the `selayer` wheel;
- their dependencies are not installed by this project's `pyproject.toml`;
- they are excluded from normal linting, type checking, and tests;
- their original relative paths and run commands no longer apply;
- they may be extracted into a separate project later.

The archive reflects the implementation and platform design previously found at:

```text
src/selayer_chat/    -> legacy/selayer_chat/
apps/                -> legacy/apps/
scripts/             -> legacy/scripts/
example_questions.md -> legacy/example_questions.md
.env.example         -> legacy/.env.example
SemaLoom design      -> legacy/docs/
```

Use Git history if an exact runnable snapshot with the original layout is needed.
